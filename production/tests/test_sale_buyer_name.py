# production/tests/test_sale_buyer_name.py
"""
SaleEvent.buyer_name — a free-text walk-in buyer name, and buyer_display,
the single read-only field that resolves what to show in the buyer
column: a registered buyer's name, else the walk-in name, else "Walk-in".

Also covers restoring an archived buyer's effect on sale editing: an old
sale that resends its own (now-archived) buyer_link unchanged must still
be editable and payable, even though a NEW assignment of an inactive
buyer stays refused.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from partners.models import FarmPartnerLink
from production.models import Batch, DailyRecord, House, SaleEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    b = Batch.objects.create(
        house=house,
        batch_code="S-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    DailyRecord.objects.create(
        batch=b, record_date=date(2026, 1, 1), feed_kg=Decimal("100.00"), recorded_by=owner
    )
    return b


@pytest.fixture
def link(db, staffed_farm, consumer, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


def _sales_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/"


def _sale_url(farm, batch, sale):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/{sale.id}/"


def _bulk_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/bulk-sync/"


def _payment_url(farm, batch, sale):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/{sale.id}/payments/"


def _payload(**overrides):
    body = {
        "sale_date": date(2026, 1, 10).isoformat(),
        "sale_type": SaleEvent.SaleType.DRESSED,
        "bird_count": 80,
        "total_weight_kg": "160.00",
        "price_per_kg": "75.00",
        "notes": "",
    }
    body.update(overrides)
    return body


class TestBuyerNameStorage:
    def test_buyer_name_is_stored_and_trimmed(self, auth, owner, staffed_farm, batch):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(buyer_name="  Juan Dela Cruz  "),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_name"] == "Juan Dela Cruz"

    def test_empty_buyer_name_is_accepted_as_unnamed_walkin(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_name"] == ""
        assert resp.data["buyer_display"] == "Walk-in"

    def test_buyer_name_together_with_buyer_link_is_rejected(
        self, auth, owner, staffed_farm, batch, link
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(buyer_link=link.pk, buyer_name="Someone"),
            format="json",
        )
        assert resp.status_code == 400
        assert resp.data["buyer_name"] == [
            "Leave the name empty when a linked buyer is chosen."
        ]

    def test_buyer_display_prefers_business_name_over_buyer_name(
        self, auth, owner, staffed_farm, batch, link
    ):
        link.business_name = "FreshMart"
        link.save(update_fields=["business_name"])
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_display"] == "FreshMart"

    def test_buyer_display_falls_back_to_buyer_name(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(buyer_name="Gate Walk-in"),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_display"] == "Gate Walk-in"


class TestBulkSyncBuyerName:
    def test_bulk_sync_accepts_buyer_name(self, auth, owner, staffed_farm, batch):
        rid = str(uuid.uuid4())
        resp = auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **_payload(buyer_name="Market Vendor")}]},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["created"] == [rid]
        assert SaleEvent.objects.get(pk=rid).buyer_name == "Market Vendor"

    def test_bulk_sync_without_buyer_name_field_still_works(
        self, auth, owner, staffed_farm, batch
    ):
        """An old queued offline payload that predates buyer_name."""
        rid = str(uuid.uuid4())
        payload = _payload()
        resp = auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **payload}]},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["created"] == [rid]
        assert SaleEvent.objects.get(pk=rid).buyer_name == ""


class TestArchivedBuyerOldSaleEditing:
    def test_new_sale_cannot_use_an_archived_buyer(
        self, auth, owner, staffed_farm, batch, link
    ):
        link.deactivate()
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_editing_other_fields_with_unchanged_archived_buyer_still_works(
        self, auth, owner, staffed_farm, batch, link
    ):
        create = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert create.status_code == 201, create.data
        sale = SaleEvent.objects.get(pk=create.data["id"])

        link.deactivate()

        resp = auth(owner).patch(
            _sale_url(staffed_farm, batch, sale),
            {"buyer_link": link.pk, "notes": "price corrected"},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["notes"] == "price corrected"
        assert resp.data["buyer_link"] == link.pk

    def test_changing_to_a_different_archived_buyer_is_still_rejected(
        self, auth, owner, staffed_farm, batch, link, consumer
    ):
        from accounts.models import User

        other_buyer = User.objects.create_user(
            "+639170009999",
            "000999",
            full_name="Other Buyer",
            role=User.Role.CONSUMER,
            must_change_credential=False,
        )
        other_link = FarmPartnerLink.objects.create(
            farm=staffed_farm,
            partner=other_buyer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=owner,
        )
        other_link.deactivate()

        create = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        sale = SaleEvent.objects.get(pk=create.data["id"])

        resp = auth(owner).patch(
            _sale_url(staffed_farm, batch, sale),
            {"buyer_link": other_link.pk},
            format="json",
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_payment_on_a_sale_with_an_archived_buyer_still_works(
        self, auth, owner, staffed_farm, batch, link
    ):
        create = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        sale = SaleEvent.objects.get(pk=create.data["id"])
        link.deactivate()

        resp = auth(owner).post(
            _payment_url(staffed_farm, batch, sale),
            {"amount": "1000.00", "received_date": date(2026, 1, 11).isoformat()},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_sale_list_still_shows_buyer_name_for_archived_buyer(
        self, auth, owner, staffed_farm, batch, link
    ):
        link.business_name = "FreshMart"
        link.save(update_fields=["business_name"])
        auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        link.deactivate()

        resp = auth(owner).get(_sales_url(staffed_farm, batch))
        assert resp.status_code == 200
        assert resp.data[0]["buyer_display"] == "FreshMart"
