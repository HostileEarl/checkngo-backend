"""
GET /api/partners/my-purchases/

A buyer sees the harvests farms recorded selling them — weight and delivery
only. No revenue, no farm annotations, no production performance, and never
another buyer's rows.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import User
from partners.models import FarmPartnerLink
from production.models import Batch, Harvest, House, SaleEvent

pytestmark = pytest.mark.django_db

URL = reverse("partners:my-purchases")


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
def link_buyer_rival(db, rival_farm, buyer, rival_owner):
    return FarmPartnerLink.objects.create(
        farm=rival_farm,
        partner=buyer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=rival_owner,
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


def _harvest(target_farm, link, *, owner, harvest_date=date(2026, 2, 1), **kw):
    """A farm → house → batch → harvest chain, attributed to `link`."""
    global _seq
    _seq += 1
    house = House.objects.create(
        farm=target_farm, name=f"House {_seq}", capacity=10000
    )
    batch = Batch.objects.create(
        house=house,
        batch_code=kw.get("batch_code", f"B-{_seq:03d}"),
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    return Harvest.objects.create(
        batch=batch,
        harvest_date=harvest_date,
        birds_harvested=kw.get("birds_harvested", 900),
        total_weight_kg=kw.get("total_weight_kg", Decimal("1500.00")),
        revenue=kw.get("revenue", Decimal("200000.00")),
        buyer_link=link,
        notes=kw.get("notes", "paid on collection"),
    )


def _sale_batch(target_farm, link, *, owner, batch_code, bird_count=80):
    """A farm -> house -> active batch -> one buyer-linked SaleEvent."""
    global _seq
    _seq += 1
    house = House.objects.create(
        farm=target_farm, name=f"House {_seq}", capacity=10000
    )
    batch = Batch.objects.create(
        house=house,
        batch_code=batch_code,
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    sale = SaleEvent.objects.create(
        batch=batch,
        sale_date=date(2026, 2, 1),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=bird_count,
        total_weight_kg=Decimal("160.00"),
        price_per_kg=Decimal("75.00"),
        buyer_link=link,
        recorded_by=owner,
    )
    return batch, sale


class TestMyPurchases:
    def test_buyer_sees_only_their_own_purchases(
        self, auth, buyer, owner, farm, link_buyer, link_buyer_b
    ):
        _harvest(farm, link_buyer, owner=owner, batch_code="MINE-1")
        _harvest(farm, link_buyer, owner=owner, batch_code="MINE-2")
        _harvest(farm, link_buyer_b, owner=owner, batch_code="THEIRS")

        resp = auth(buyer).get(URL)

        assert resp.status_code == 200
        assert {row["batch_code"] for row in resp.data} == {"MINE-1", "MINE-2"}

    def test_second_buyers_purchases_are_absent(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        _harvest(farm, link_buyer, owner=owner, batch_code="MINE")
        _harvest(farm, link_buyer_b, owner=owner, batch_code="THEIRS")

        codes = {row["batch_code"] for row in auth(buyer).get(URL).data}

        assert codes == {"MINE"}
        assert "THEIRS" not in codes

    def test_internal_user_gets_empty_list_not_403(
        self, auth, worker, owner, farm, buyer, link_buyer
    ):
        _harvest(farm, link_buyer, owner=owner)

        resp = auth(worker).get(URL)

        assert resp.status_code == 200
        assert resp.data == []

    def test_unauthenticated_request_gets_401(self, api):
        assert api.get(URL).status_code == 401

    def test_farm_filter_narrows(
        self,
        auth,
        buyer,
        owner,
        farm,
        rival_farm,
        link_buyer,
        link_buyer_rival,
    ):
        _harvest(farm, link_buyer, owner=owner, batch_code="HOME")
        _harvest(rival_farm, link_buyer_rival, owner=owner, batch_code="AWAY")

        resp = auth(buyer).get(URL, {"farm": farm.pk})

        assert resp.status_code == 200
        assert [row["batch_code"] for row in resp.data] == ["HOME"]

    def test_ordered_by_harvest_date_descending(
        self, auth, buyer, owner, farm, link_buyer
    ):
        _harvest(farm, link_buyer, owner=owner, batch_code="OLD",
                 harvest_date=date(2026, 1, 5))
        _harvest(farm, link_buyer, owner=owner, batch_code="NEW",
                 harvest_date=date(2026, 3, 5))
        _harvest(farm, link_buyer, owner=owner, batch_code="MID",
                 harvest_date=date(2026, 2, 5))

        codes = [row["batch_code"] for row in auth(buyer).get(URL).data]

        assert codes == ["NEW", "MID", "OLD"]

    def test_revenue_and_annotations_not_exposed(
        self, auth, buyer, owner, farm, link_buyer
    ):
        _harvest(
            farm,
            link_buyer,
            owner=owner,
            revenue=Decimal("200000.00"),
            notes="short weight, buyer disputed",
        )

        row = auth(buyer).get(URL).data[0]

        for hidden in (
            "revenue",
            "revenue_per_kg",
            "notes",
            "recorded_by",
            "feed_conversion_ratio",
            "mortality_rate_pct",
        ):
            assert hidden not in row

        assert set(row) == {
            "id",
            "source",
            "harvest_date",
            "farm",
            "farm_name",
            "batch_code",
            "birds_harvested",
            "total_weight_kg",
            "average_weight_kg",
        }
        assert row["source"] == "harvest"

    def _batch_with_sale(self, target_farm, link, *, owner, batch_code, bird_count=80):
        return _sale_batch(
            target_farm, link, owner=owner, batch_code=batch_code, bird_count=bird_count
        )

    def test_buyer_linked_sale_appears_tagged_as_sale(
        self, auth, buyer, owner, farm, link_buyer
    ):
        self._batch_with_sale(farm, link_buyer, owner=owner, batch_code="SALE-1")

        rows = auth(buyer).get(URL).data

        assert len(rows) == 1
        assert rows[0]["source"] == "sale"
        assert rows[0]["batch_code"] == "SALE-1"
        assert rows[0]["birds_harvested"] == 80

    def test_sale_linked_purchase_hides_revenue_and_internals(
        self, auth, buyer, owner, farm, link_buyer
    ):
        self._batch_with_sale(farm, link_buyer, owner=owner, batch_code="SALE-HIDE")

        row = auth(buyer).get(URL).data[0]

        for hidden in ("revenue", "revenue_per_kg", "notes", "recorded_by", "price_per_kg"):
            assert hidden not in row

    def test_sale_with_no_buyer_is_invisible_to_everyone(
        self, auth, buyer, owner, farm, link_buyer
    ):
        house = House.objects.create(farm=farm, name="No buyer house", capacity=5000)
        batch = Batch.objects.create(
            house=house, batch_code="NOBUYER", initial_bird_count=500,
            start_date=date(2026, 1, 1), created_by=owner,
        )
        SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 2, 1),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=50,
            total_weight_kg=Decimal("90.00"), price_per_kg=Decimal("70.00"),
            buyer_link=None, recorded_by=owner,
        )

        assert auth(buyer).get(URL).data == []

    def test_no_double_counting_when_batch_has_both_harvest_and_sale_buyer(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        """
        A batch with sale events reports through its sales only — even a
        Harvest row with its own buyer_link on the SAME batch is dropped,
        per the "sale events are the source of truth once they exist"
        rule _batch_sale_totals already uses elsewhere.
        """
        batch, _ = self._batch_with_sale(
            farm, link_buyer, owner=owner, batch_code="MIXED"
        )
        Harvest.objects.create(
            batch=batch, harvest_date=date(2026, 3, 1),
            birds_harvested=900, total_weight_kg=Decimal("1500.00"),
            revenue=Decimal("200000.00"), buyer_link=link_buyer_b,
        )

        buyer_rows = auth(buyer).get(URL).data
        buyer_b_rows = auth(buyer_b).get(URL).data

        assert len(buyer_rows) == 1
        assert buyer_rows[0]["source"] == "sale"
        # buyer_b is the Harvest's buyer on this same batch, but the batch
        # has sale events, so that Harvest row never surfaces for anyone.
        assert buyer_b_rows == []

    def test_cross_buyer_isolation_for_sale_linked_purchases(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        self._batch_with_sale(farm, link_buyer, owner=owner, batch_code="MINE-SALE")
        self._batch_with_sale(
            farm, link_buyer_b, owner=owner, batch_code="THEIRS-SALE"
        )

        codes = {row["batch_code"] for row in auth(buyer).get(URL).data}

        assert codes == {"MINE-SALE"}

    def test_no_price_total_or_revenue_key_leaks_to_buyer(
        self, auth, buyer, owner, farm, link_buyer
    ):
        """
        A generic regression guard, not just a check of today's known
        field list: any key whose name contains "price", "revenue", or
        "total" is assumed to be a money figure and must never reach a
        buyer, from either source row. total_weight_kg/average_weight_kg
        are the one allowed exception — they are weight, not money.
        """
        _harvest(farm, link_buyer, owner=owner, batch_code="GUARD-HARVEST")
        self._batch_with_sale(farm, link_buyer, owner=owner, batch_code="GUARD-SALE")

        rows = auth(buyer).get(URL).data
        assert len(rows) == 2

        allowed_weight_keys = {"total_weight_kg", "average_weight_kg"}
        for row in rows:
            for key in row:
                if key in allowed_weight_keys:
                    continue
                lowered = key.lower()
                assert "price" not in lowered, f"{key!r} leaks a price figure"
                assert "revenue" not in lowered, f"{key!r} leaks a revenue figure"
                assert "total" not in lowered, f"{key!r} leaks a total figure"

    def test_payload_shape(self, auth, buyer, owner, farm, link_buyer):
        _harvest(
            farm,
            link_buyer,
            owner=owner,
            birds_harvested=1000,
            total_weight_kg=Decimal("1500.00"),
        )

        row = auth(buyer).get(URL).data[0]

        assert row["farm"] == farm.pk
        assert row["farm_name"] == farm.name
        assert row["birds_harvested"] == 1000
        assert row["total_weight_kg"] == "1500.00"
        assert row["average_weight_kg"] == "1.500"


class TestNewRowsBuyerIsolation:
    """
    Isolation specifically for the per-sale buyer_link rows added in this
    change (BuyerSalePurchaseSerializer), and for the "no buyer_link"
    case's only route to a buyer: the legacy Harvest rule — which is
    itself shut off the moment the batch has any sale events at all (see
    MyPurchasesView.list's no-double-counting note).
    """

    def test_a_buyer_never_sees_another_buyers_linked_sale(
        self, auth, buyer, buyer_b, owner, farm, link_buyer, link_buyer_b
    ):
        _sale_batch(
            farm, link_buyer, owner=owner, batch_code="A-SALE"
        )
        _sale_batch(
            farm, link_buyer_b, owner=owner, batch_code="B-SALE"
        )

        buyer_rows = auth(buyer).get(URL).data
        buyer_b_rows = auth(buyer_b).get(URL).data

        assert {r["batch_code"] for r in buyer_rows} == {"A-SALE"}
        assert {r["batch_code"] for r in buyer_b_rows} == {"B-SALE"}
        assert "B-SALE" not in {r["batch_code"] for r in buyer_rows}
        assert "A-SALE" not in {r["batch_code"] for r in buyer_b_rows}

    def test_a_buyer_linked_sale_is_invisible_to_a_farm_member_with_no_buyer_link(
        self, auth, buyer, manager, owner, farm, link_buyer
    ):
        # manager is internal staff, not a registered buyer at all — the
        # query is scoped by buyer_link__partner, so there is no identity
        # to even match against.
        _sale_batch(
            farm, link_buyer, owner=owner, batch_code="STAFF-INVISIBLE"
        )
        assert auth(manager).get(URL).data == []

    def test_a_sale_with_no_buyer_link_reaches_no_one_even_via_the_harvest_rule(
        self, auth, buyer, owner, farm, link_buyer
    ):
        """
        A SaleEvent cannot name a buyer unless buyer_link is set on it
        directly — there is no fallback from a bare sale row to anyone.
        The ONLY other route to a buyer for that batch is the legacy
        Harvest rule, and that rule itself never fires once the batch has
        any sale events (regardless of whether those carry a buyer_link),
        per the no-double-counting rule. So a batch with an unlinked sale
        AND a buyer-linked Harvest must still be invisible.
        """
        house = House.objects.create(farm=farm, name="Unlinked sale house", capacity=5000)
        batch = Batch.objects.create(
            house=house, batch_code="UNLINKED", initial_bird_count=1000,
            start_date=date(2026, 1, 1), created_by=owner,
        )
        SaleEvent.objects.create(
            batch=batch, sale_date=date(2026, 2, 1),
            sale_type=SaleEvent.SaleType.DRESSED, bird_count=50,
            total_weight_kg=Decimal("90.00"), price_per_kg=Decimal("70.00"),
            buyer_link=None, recorded_by=owner,
        )
        Harvest.objects.create(
            batch=batch, harvest_date=date(2026, 3, 1),
            birds_harvested=900, total_weight_kg=Decimal("1500.00"),
            revenue=Decimal("200000.00"), buyer_link=link_buyer,
        )

        assert auth(buyer).get(URL).data == []

    def test_a_buyer_without_any_sale_events_still_reaches_the_batch_via_harvest(
        self, auth, buyer, owner, farm, link_buyer
    ):
        # Control case: with no sale events on the batch at all, the
        # legacy Harvest rule is the batch's only route to a buyer, and it
        # still works.
        _harvest(farm, link_buyer, owner=owner, batch_code="HARVEST-ONLY")
        codes = {r["batch_code"] for r in auth(buyer).get(URL).data}
        assert codes == {"HARVEST-ONLY"}
