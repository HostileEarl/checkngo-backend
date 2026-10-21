"""
GET /api/partners/my-payments/

A buyer sees payments they made against sales — never another buyer's,
never a different farm's operational data, and never the sale's own
revenue or outstanding figure (that's the farm's side of the ledger).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import User
from partners.models import FarmPartnerLink
from production.models import Batch, House, Payment, SaleEvent

pytestmark = pytest.mark.django_db

URL = reverse("partners:my-payments")


@pytest.fixture
def buyer(db):
    return User.objects.create_user(
        "+639175557777",
        "555777",
        full_name="FreshMart Buyer",
        role=User.Role.CONSUMER,
        must_change_credential=False,
    )


@pytest.fixture
def buyer_b(db):
    return User.objects.create_user(
        "+639176668888",
        "666888",
        full_name="Palengke Buyer",
        role=User.Role.CONSUMER,
        must_change_credential=False,
    )


@pytest.fixture
def link_buyer(db, farm, buyer, owner):
    return FarmPartnerLink.objects.create(
        farm=farm,
        partner=buyer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


@pytest.fixture
def link_buyer_b(db, farm, buyer_b, owner):
    return FarmPartnerLink.objects.create(
        farm=farm,
        partner=buyer_b,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


_seq = 0


def _sale(target_farm, *, owner, **kw):
    global _seq
    _seq += 1
    house = House.objects.create(
        farm=target_farm, name=f"House {_seq}", capacity=10000
    )
    batch = Batch.objects.create(
        house=house,
        batch_code=f"B-{_seq:03d}",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    return SaleEvent.objects.create(
        batch=batch,
        sale_date=kw.get("sale_date", date(2026, 1, 10)),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=kw.get("bird_count", 500),
        total_weight_kg=kw.get("total_weight_kg", Decimal("1000.00")),
        price_per_kg=kw.get("price_per_kg", Decimal("70.00")),
    )


def _payment(sale, link, *, amount=Decimal("2000.00"), received_date=date(2026, 1, 11)):
    return Payment.objects.create(
        sale=sale,
        amount=amount,
        received_date=received_date,
        buyer_link=link,
    )


class TestMyPayments:
    def test_buyer_sees_only_their_own_payments(
        self, auth, buyer, owner, farm, link_buyer, link_buyer_b
    ):
        sale_mine = _sale(farm, owner=owner)
        sale_theirs = _sale(farm, owner=owner)
        _payment(sale_mine, link_buyer, amount=Decimal("1000.00"))
        _payment(sale_mine, link_buyer, amount=Decimal("1500.00"))
        _payment(sale_theirs, link_buyer_b, amount=Decimal("500.00"))

        resp = auth(buyer).get(URL)

        assert resp.status_code == 200
        amounts = {row["amount"] for row in resp.data}
        assert amounts == {"1000.00", "1500.00"}

    def test_a_second_buyer_is_denied_the_first_buyers_payments(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        sale = _sale(farm, owner=owner)
        _payment(sale, link_buyer, amount=Decimal("1000.00"))

        resp = auth(buyer_b).get(URL)

        assert resp.status_code == 200
        assert resp.data == []

    def test_no_revenue_or_outstanding_in_the_buyer_view(
        self, auth, buyer, owner, farm, link_buyer
    ):
        sale = _sale(farm, owner=owner)
        _payment(sale, link_buyer)

        row = auth(buyer).get(URL).data[0]

        assert "revenue" not in row
        assert "outstanding" not in row
        assert "notes" not in row
        assert set(row) == {
            "id",
            "farm",
            "farm_name",
            "batch_code",
            "sale_date",
            "amount",
            "received_date",
            "is_reversal",
        }

    def test_unattributed_payments_are_not_shown_to_any_buyer(
        self, auth, buyer, owner, farm, link_buyer
    ):
        """A cash-at-the-gate payment with no buyer_link, on a sale with no
        buyer either, belongs to no one."""
        sale = _sale(farm, owner=owner)
        _payment(sale, link=None)

        resp = auth(buyer).get(URL)

        assert resp.status_code == 200
        assert resp.data == []

    def test_internal_user_gets_empty_list_not_403(self, auth, worker, farm, link_buyer):
        sale = _sale(farm, owner=worker)
        _payment(sale, link_buyer)

        resp = auth(worker).get(URL)

        assert resp.status_code == 200
        assert resp.data == []

    def test_unauthenticated_request_gets_401(self, api):
        assert api.get(URL).status_code == 401


def _buyers_sale(farm, owner, link):
    sale = _sale(farm, owner=owner)
    sale.buyer_link = link
    sale.save(update_fields=["buyer_link"])
    return sale


class TestPaymentsOnTheBuyersSales:
    """
    Payments recorded before the app sent buyer_link have none of their
    own; the sale's buyer_link says whose they are.
    """

    def test_a_payment_without_buyer_link_on_the_buyers_sale_is_listed(
        self, auth, buyer, owner, farm, link_buyer
    ):
        sale = _buyers_sale(farm, owner, link_buyer)
        _payment(sale, link=None, amount=Decimal("1200.00"))

        resp = auth(buyer).get(URL)

        assert resp.status_code == 200
        assert [row["amount"] for row in resp.data] == ["1200.00"]
        assert resp.data[0]["batch_code"] == sale.batch.batch_code

    def test_a_payment_on_another_buyers_sale_is_not_listed(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        theirs = _buyers_sale(farm, owner, link_buyer_b)
        _payment(theirs, link=None)

        assert auth(buyer).get(URL).data == []
        assert len(auth(buyer_b).get(URL).data) == 1

    def test_a_payment_attributed_to_someone_else_stays_theirs_even_on_my_sale(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        """An explicit buyer_link wins over the sale's buyer."""
        mine = _buyers_sale(farm, owner, link_buyer)
        _payment(mine, link_buyer_b, amount=Decimal("300.00"))

        assert auth(buyer).get(URL).data == []
        assert [row["amount"] for row in auth(buyer_b).get(URL).data] == ["300.00"]

    def test_reversals_follow_the_same_rule(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        mine = _buyers_sale(farm, owner, link_buyer)
        original = _payment(mine, link=None, amount=Decimal("800.00"))
        Payment.objects.create(
            sale=mine,
            amount=Decimal("-800.00"),
            received_date=date(2026, 1, 12),
            reverses=original,
        )
        theirs = _buyers_sale(farm, owner, link_buyer_b)
        other = _payment(theirs, link=None, amount=Decimal("400.00"))
        Payment.objects.create(
            sale=theirs,
            amount=Decimal("-400.00"),
            received_date=date(2026, 1, 12),
            reverses=other,
        )

        rows = auth(buyer).get(URL).data

        assert sorted((row["amount"], row["is_reversal"]) for row in rows) == [
            ("-800.00", True),
            ("800.00", False),
        ]

    def test_the_farm_filter_still_applies(
        self, auth, buyer, owner, farm, rival_farm, rival_owner, link_buyer
    ):
        rival_link = FarmPartnerLink.objects.create(
            farm=rival_farm,
            partner=buyer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=rival_owner,
        )
        _payment(_buyers_sale(farm, owner, link_buyer), link=None)
        _payment(_buyers_sale(rival_farm, rival_owner, rival_link), link=None)

        all_rows = auth(buyer).get(URL).data
        one_farm = auth(buyer).get(URL, {"farm": farm.id}).data

        assert len(all_rows) == 2
        assert [row["farm"] for row in one_farm] == [farm.id]

    def test_internal_user_still_gets_an_empty_list(
        self, auth, worker, owner, farm, link_buyer
    ):
        _payment(_buyers_sale(farm, owner, link_buyer), link=None)

        assert auth(worker).get(URL).data == []
