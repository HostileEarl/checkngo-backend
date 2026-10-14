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
from production.models import Batch, DailyRecord, House, Payment, SaleEvent

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
