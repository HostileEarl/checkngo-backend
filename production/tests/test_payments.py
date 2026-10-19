# production/tests/test_payments.py
"""
Payments against a sale.

An append-only log, not a field on SaleEvent — "nagbayad 2k" against a
larger sale is one of several instalments, and a single amount_paid column
would overwrite earlier ones and lose them. Outstanding balance is always
derived (revenue minus the sum of payments), never stored. A mistake is
corrected with a reversing entry, never an edit or a delete.
"""
from datetime import date
from decimal import Decimal

import pytest

from analytics.services import fcr_by_batch, profitability_by_batch
from partners.models import FarmPartnerLink
from production.models import Batch, DailyRecord, Harvest, House, Payment, SaleEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed P", capacity=5000)


@pytest.fixture
def batch(house, owner):
    b = Batch.objects.create(
        house=house,
        batch_code="PAY-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    for day in range(1, 5):
        DailyRecord.objects.create(
            batch=b,
            record_date=date(2026, 1, day),
            mortality_disease=10,
            feed_kg=Decimal("100.00"),
            recorded_by=owner,
        )
    b.refresh_from_db()
    return b


@pytest.fixture
def sale(batch):
    return SaleEvent.objects.create(
        batch=batch,
        sale_date=date(2026, 1, 10),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=500,
        total_weight_kg=Decimal("1000.00"),
        price_per_kg=Decimal("70.00"),  # revenue 70,000.00
    )


def _payments_url(farm, batch, sale):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/{sale.id}/payments/"


class TestWhoCanRecordAPayment:
    def test_owner_can_record_a_payment(self, auth, owner, staffed_farm, batch, sale):
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_manager_can_record_a_payment(
        self, auth, manager, staffed_farm, batch, sale
    ):
        resp = auth(manager).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_worker_cannot_record_a_payment(
        self, auth, worker, staffed_farm, batch, sale
    ):
        resp = auth(worker).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code == 403
        assert not Payment.objects.filter(sale=sale).exists()

    def test_buyer_cannot_record_a_payment(
        self, auth, staffed_farm, batch, sale
    ):
        from accounts.models import User

        buyer = User.objects.create_user(
            "+639170000099",
            "999999",
            full_name="A Buyer",
            role=User.Role.CONSUMER,
            must_change_credential=False,
        )
        resp = auth(buyer).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code in (403, 404)
        assert not Payment.objects.filter(sale=sale).exists()


class TestFarmIsolation:
    def test_a_member_of_a_different_farm_is_denied(
        self, auth, rival_owner, staffed_farm, batch, sale
    ):
        resp = auth(rival_owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code in (403, 404)
        assert not Payment.objects.filter(sale=sale).exists()

    def test_a_different_farms_owner_cannot_list_payments(
        self, auth, owner, rival_owner, staffed_farm, batch, sale
    ):
        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        resp = auth(rival_owner).get(_payments_url(staffed_farm, batch, sale))
        assert resp.status_code in (403, 404)


class TestOutstandingBalance:
    def test_two_partial_payments_leave_the_correct_outstanding(
        self, auth, owner, staffed_farm, batch, sale
    ):
        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "3000.00", "received_date": "2026-01-15"},
            format="json",
        )

        resp = auth(owner).get(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/sales/"
        )
        row = next(r for r in resp.data if r["id"] == str(sale.id))
        assert row["total_paid"] == "5000.00"
        assert row["outstanding"] == "65000.00"
        assert row["is_fully_paid"] is False

    def test_outstanding_at_zero_reads_as_fully_paid(
        self, auth, owner, staffed_farm, batch, sale
    ):
        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "70000.00", "received_date": "2026-01-11"},
            format="json",
        )

        sale.refresh_from_db()
        assert sale.outstanding == Decimal("0.00")
        assert sale.is_fully_paid is True

    def test_a_reversal_makes_a_fully_paid_sale_unpaid_again(
        self, auth, owner, staffed_farm, batch, sale
    ):
        full = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "70000.00", "received_date": "2026-01-11"},
            format="json",
        )
        sale.refresh_from_db()
        assert sale.is_fully_paid is True

        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {
                "amount": "-70000.00",
                "received_date": "2026-01-12",
                "reverses": full.data["id"],
                "note": "Recorded against the wrong sale.",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data

        sale.refresh_from_db()
        assert sale.total_paid == Decimal("0.00")
        assert sale.is_fully_paid is False

    def test_a_reversal_must_be_the_exact_negative(
        self, auth, owner, staffed_farm, batch, sale
    ):
        full = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "70000.00", "received_date": "2026-01-11"},
            format="json",
        )

        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {
                "amount": "-50000.00",
                "received_date": "2026-01-12",
                "reverses": full.data["id"],
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "amount" in resp.data


@pytest.fixture
def consumer_b(db):
    from accounts.models import User

    return User.objects.create_user(
        "+639170000088",
        "888888",
        full_name="Another Buyer",
        role=User.Role.CONSUMER,
        must_change_credential=False,
    )


@pytest.fixture
def link(db, staffed_farm, consumer, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


@pytest.fixture
def link_other_farm(db, rival_farm, consumer, rival_owner):
    """Same partner, but linked through a different farm's row."""
    return FarmPartnerLink.objects.create(
        farm=rival_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=rival_owner,
    )


@pytest.fixture
def link_b(db, staffed_farm, consumer_b, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer_b,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


class TestPaymentBuyerLinkAgainstSaleAndHarvest:
    """
    Payment.buyer_link is attributed independently of SaleEvent.buyer_link
    and Harvest.buyer_link (see production/models.py) — nothing ties them
    together at the model layer. The two can disagree today; this is the
    guard that stops a payment contradicting a buyer already on record for
    the sale it is against, without ever inventing one that wasn't given.
    """

    def test_payment_naming_the_same_buyer_as_the_sale_is_accepted(
        self, auth, owner, staffed_farm, batch, link
    ):
        sale = SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=500,
            total_weight_kg=Decimal("1000.00"), price_per_kg=Decimal("70.00"),
            buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11", "buyer_link": link.pk},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_payment_naming_a_different_buyer_than_the_sale_is_rejected(
        self, auth, owner, staffed_farm, batch, link, link_b
    ):
        sale = SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=500,
            total_weight_kg=Decimal("1000.00"), price_per_kg=Decimal("70.00"),
            buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11", "buyer_link": link_b.pk},
            format="json",
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data
        assert not Payment.objects.filter(sale=sale).exists()

    def test_payment_naming_the_same_partner_via_a_different_farm_link_is_rejected(
        self, auth, owner, staffed_farm, batch, link, link_other_farm
    ):
        """
        link and link_other_farm are two different FarmPartnerLink rows
        for the SAME partner. The check is "same farm, same partner", so
        a payment pointing at the other farm's row for that partner still
        does not satisfy it (and would fail its own farm check first) —
        this exercises that the match is not done by raw FK equality
        alone when the rows otherwise look interchangeable.
        """
        sale = SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=500,
            total_weight_kg=Decimal("1000.00"), price_per_kg=Decimal("70.00"),
            buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {
                "amount": "2000.00",
                "received_date": "2026-01-11",
                "buyer_link": link_other_farm.pk,
            },
            format="json",
        )
        # Rejected either way: link_other_farm fails its own
        # "linked to this farm" check before the mismatch check even runs.
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_payment_naming_the_harvests_buyer_when_the_sale_has_none_is_accepted(
        self, auth, owner, staffed_farm, batch, house, link, sale
    ):
        # `sale` (the module fixture) carries no buyer_link. Close the
        # batch so its Harvest picks one up, then pay against that sale.
        Harvest.objects.create(
            batch=batch, harvest_date=date(2026, 1, 15),
            birds_harvested=500, total_weight_kg=Decimal("1000.00"),
            revenue=Decimal("70000.00"), buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-16", "buyer_link": link.pk},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_payment_contradicting_the_harvests_buyer_is_rejected(
        self, auth, owner, staffed_farm, batch, house, link, link_b, sale
    ):
        Harvest.objects.create(
            batch=batch, harvest_date=date(2026, 1, 15),
            birds_harvested=500, total_weight_kg=Decimal("1000.00"),
            revenue=Decimal("70000.00"), buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-16", "buyer_link": link_b.pk},
            format="json",
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_payment_with_no_buyer_link_is_accepted_even_when_the_sale_has_one(
        self, auth, owner, staffed_farm, batch, link
    ):
        # No invention: omitting buyer_link is never treated as a mismatch.
        sale = SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=500,
            total_weight_kg=Decimal("1000.00"), price_per_kg=Decimal("70.00"),
            buyer_link=link,
        )
        resp = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_link"] is None


class TestAppendOnly:
    def test_a_payment_cannot_be_patched(self, auth, owner, staffed_farm, batch, sale):
        created = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        resp = auth(owner).patch(
            f"{_payments_url(staffed_farm, batch, sale)}{created.data['id']}/",
            {"amount": "9999.00"},
            format="json",
        )
        assert resp.status_code in (404, 405)

    def test_a_payment_cannot_be_deleted(self, auth, owner, staffed_farm, batch, sale):
        created = auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        resp = auth(owner).delete(
            f"{_payments_url(staffed_farm, batch, sale)}{created.data['id']}/"
        )
        assert resp.status_code in (404, 405)
        assert Payment.objects.filter(pk=created.data["id"]).exists()


class TestAnalyticsUntouchedByPayments:
    def test_fcr_and_feed_margin_are_unchanged_by_recording_a_payment(
        self, auth, owner, staffed_farm, batch, house, sale
    ):
        # Sell the rest and close, so Harvest/FCR/profitability have data.
        auth(owner).post(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/sales/",
            {
                "sale_date": "2026-01-11",
                "sale_type": SaleEvent.SaleType.LIVE,
                "bird_count": 460,
                "total_weight_kg": "900.00",
                "price_per_kg": "60.00",
            },
            format="json",
        )
        auth(owner).post(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/close/", {}, format="json"
        )

        before_fcr = fcr_by_batch(staffed_farm)
        before_profit = profitability_by_batch(staffed_farm)

        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-20"},
            format="json",
        )

        after_fcr = fcr_by_batch(staffed_farm)
        after_profit = profitability_by_batch(staffed_farm)

        assert before_fcr == after_fcr
        assert before_profit == after_profit
