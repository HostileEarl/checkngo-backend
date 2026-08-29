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
from production.models import Batch, Harvest, House

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
            "harvest_date",
            "farm",
            "farm_name",
            "batch_code",
            "birds_harvested",
            "total_weight_kg",
            "average_weight_kg",
        }

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
