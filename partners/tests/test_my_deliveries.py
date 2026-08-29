"""
GET /api/partners/my-deliveries/

A supplier sees the feed deliveries farms recorded from them — and only
those. No other supplier's rows, nothing operational, and internal staff
get an empty list rather than a 403.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import User
from partners.models import FarmPartnerLink
from production.models import FeedDelivery

pytestmark = pytest.mark.django_db

URL = reverse("partners:my-deliveries")


@pytest.fixture
def supplier_b(db):
    return User.objects.create_user(
        "+639176667777",
        "666666",
        full_name="GrainWorks Supplier",
        role=User.Role.SUPPLIER,
        must_change_credential=False,
    )


@pytest.fixture
def link_a(db, farm, supplier, owner):
    """supplier ↔ Santos Layer Farm."""
    return FarmPartnerLink.objects.create(
        farm=farm,
        partner=supplier,
        link_type=FarmPartnerLink.LinkType.SUPPLIER,
        linked_by=owner,
    )


@pytest.fixture
def link_a_rival(db, rival_farm, supplier, rival_owner):
    """The same supplier, also linked to a second farm."""
    return FarmPartnerLink.objects.create(
        farm=rival_farm,
        partner=supplier,
        link_type=FarmPartnerLink.LinkType.SUPPLIER,
        linked_by=rival_owner,
    )


@pytest.fixture
def link_b(db, farm, supplier_b, owner):
    """A different supplier, linked to the first farm."""
    return FarmPartnerLink.objects.create(
        farm=farm,
        partner=supplier_b,
        link_type=FarmPartnerLink.LinkType.SUPPLIER,
        linked_by=owner,
    )


def _delivery(farm, link, **kw):
    kw.setdefault("delivery_date", date(2026, 1, 10))
    kw.setdefault("feed_type", FeedDelivery.FeedType.STARTER)
    kw.setdefault("quantity_kg", Decimal("1000"))
    kw.setdefault("unit_cost", Decimal("30.00"))
    return FeedDelivery.objects.create(farm=farm, supplier_link=link, **kw)


class TestMyDeliveries:
    def test_supplier_sees_only_their_own_deliveries(
        self, auth, supplier, farm, link_a, link_b
    ):
        _delivery(farm, link_a, invoice_ref="A-1")
        _delivery(farm, link_a, invoice_ref="A-2", delivery_date=date(2026, 2, 1))
        _delivery(farm, link_b, invoice_ref="B-1")  # another supplier's row

        resp = auth(supplier).get(URL)

        assert resp.status_code == 200
        refs = {row["invoice_ref"] for row in resp.data}
        assert refs == {"A-1", "A-2"}

    def test_second_suppliers_deliveries_are_absent(
        self, auth, supplier, supplier_b, farm, link_a, link_b
    ):
        _delivery(farm, link_a, invoice_ref="MINE")
        _delivery(farm, link_b, invoice_ref="THEIRS")

        resp = auth(supplier).get(URL)

        assert resp.status_code == 200
        assert [row["invoice_ref"] for row in resp.data] == ["MINE"]
        assert "THEIRS" not in {row["invoice_ref"] for row in resp.data}

    def test_internal_user_gets_empty_list_not_403(
        self, auth, worker, farm, supplier, link_a
    ):
        _delivery(farm, link_a)

        resp = auth(worker).get(URL)

        assert resp.status_code == 200
        assert resp.data == []

    def test_unauthenticated_request_gets_401(self, api):
        assert api.get(URL).status_code == 401

    def test_farm_filter_narrows(
        self, auth, supplier, farm, rival_farm, link_a, link_a_rival
    ):
        _delivery(farm, link_a, invoice_ref="SANTOS")
        _delivery(rival_farm, link_a_rival, invoice_ref="RIVAL")

        resp = auth(supplier).get(URL, {"farm": farm.pk})

        assert resp.status_code == 200
        assert [row["invoice_ref"] for row in resp.data] == ["SANTOS"]

    def test_ordered_by_delivery_date_descending(self, auth, supplier, farm, link_a):
        _delivery(farm, link_a, invoice_ref="OLD", delivery_date=date(2026, 1, 1))
        _delivery(farm, link_a, invoice_ref="NEW", delivery_date=date(2026, 3, 1))
        _delivery(farm, link_a, invoice_ref="MID", delivery_date=date(2026, 2, 1))

        resp = auth(supplier).get(URL)

        assert [row["invoice_ref"] for row in resp.data] == ["NEW", "MID", "OLD"]

    def test_internal_annotations_not_exposed(self, auth, supplier, farm, link_a):
        _delivery(farm, link_a, notes="short-delivered by 20kg")

        row = auth(supplier).get(URL).data[0]

        assert "notes" not in row
        assert "recorded_by" not in row
        assert set(row) == {
            "id",
            "farm",
            "farm_name",
            "delivery_date",
            "feed_type",
            "feed_type_display",
            "quantity_kg",
            "unit_cost",
            "total_cost",
            "invoice_ref",
        }
