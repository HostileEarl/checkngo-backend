# production/tests/test_inventory.py
"""
Inventory usage logging.

Two guarantees the task calls out explicitly, pinned here:

  1. Idempotency. A usage log's identity is the client-generated UUID
     alone — it is an event, not a one-per-day record. A retried POST (or
     bulk-sync row) carrying an id that already exists updates in place and
     returns a clean 200 / reports it under `updated`, never a raw
     IntegrityError from the primary-key constraint. Two DIFFERENT ids for
     the same item on the same day are both accepted — that is a worker
     drawing the item down twice, not a duplicate.

  2. The 24-hour edit lock, mirroring daily mortality records. Inside the
     window the original worker can PATCH / re-POST. Outside it the record
     is evidence: PATCH is a 400, a retried POST is a clean 400 (not a
     500), and a bulk-sync row lands in `failed[]` with its id.

Plus the low-stock alert's three states: never stocked (suppressed),
running low (warning), and out of stock (danger).
"""
import uuid
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from production.models import (
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def item(staffed_farm, owner):
    return InventoryItem.objects.create(
        farm=staffed_farm,
        name="Disinfectant",
        unit="litre",
        low_stock_threshold=20,
        created_by=owner,
    )


def _usage_url(farm):
    return f"/api/farms/{farm.id}/inventory/usage/"


def _bulk_url(farm):
    return f"/api/farms/{farm.id}/inventory/usage/bulk-sync/"


def _payload(item, usage_date="2026-02-10", **overrides):
    body = {
        "item": item.id,
        "quantity_used": "3.00",
        "usage_date": usage_date,
        "notes": "",
    }
    body.update(overrides)
    return body


def _age_out(usage_log):
    """Push a record's server-clock birth back over the 24-hour line."""
    InventoryUsageLog.objects.filter(pk=usage_log.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    usage_log.refresh_from_db()


# ─────────────────────────────────────────────────────────────
# 1. Idempotency — single POST
# ─────────────────────────────────────────────────────────────


class TestSinglePostIdempotency:
    def test_supplied_id_is_stored_exactly(self, auth, worker, staffed_farm, item):
        rid = str(uuid.uuid4())
        resp = auth(worker).post(
            _usage_url(staffed_farm), _payload(item, id=rid), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["id"] == rid
        assert InventoryUsageLog.objects.filter(pk=rid).exists()

    def test_posting_same_id_twice_updates_not_duplicates(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        first = auth(worker).post(
            _usage_url(staffed_farm), _payload(item, id=rid), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(worker).post(
            _usage_url(staffed_farm), _payload(item, id=rid), format="json"
        )
        # A clean 200, not a 500 from the primary-key unique constraint.
        assert second.status_code == 200, second.data
        assert second.data["id"] == rid
        assert InventoryUsageLog.objects.filter(item=item).count() == 1

    def test_posting_same_id_with_different_values_updates_them(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id=rid, quantity_used="3.00"),
            format="json",
        )
        resp = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id=rid, quantity_used="8.50", notes="topped up"),
            format="json",
        )
        assert resp.status_code == 200, resp.data
        row = InventoryUsageLog.objects.get(pk=rid)
        assert str(row.quantity_used) == "8.50"
        assert row.notes == "topped up"

    def test_posting_without_id_still_works_and_generates_one(
        self, auth, worker, staffed_farm, item
    ):
        resp = auth(worker).post(
            _usage_url(staffed_farm), _payload(item), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["id"]
        assert InventoryUsageLog.objects.filter(item=item).count() == 1

    def test_same_item_twice_in_one_day_is_two_events_not_a_conflict(
        self, auth, worker, staffed_farm, item
    ):
        """
        The key difference from a daily mortality record: no (item, date)
        uniqueness. Two different ids on the same item and day are a worker
        drawing it down twice, and both are accepted.
        """
        first = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id=str(uuid.uuid4()), quantity_used="3.00"),
            format="json",
        )
        second = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id=str(uuid.uuid4()), quantity_used="5.00"),
            format="json",
        )
        assert first.status_code == 201, first.data
        assert second.status_code == 201, second.data
        assert InventoryUsageLog.objects.filter(item=item).count() == 2

    def test_malformed_id_is_a_clean_validation_error_not_a_crash(
        self, auth, worker, staffed_farm, item
    ):
        resp = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id="not-a-uuid"),
            format="json",
        )
        assert resp.status_code == 400
        assert "id" in resp.data


# ─────────────────────────────────────────────────────────────
# 2. Idempotency — bulk sync
# ─────────────────────────────────────────────────────────────


class TestBulkSyncIdempotency:
    def _sync(self, client, farm, records):
        return client.post(_bulk_url(farm), {"records": records}, format="json")

    def test_supplied_id_is_stored_exactly(self, auth, worker, staffed_farm, item):
        rid = str(uuid.uuid4())
        resp = self._sync(auth(worker), staffed_farm, [_payload(item, id=rid)])
        assert resp.status_code == 200, resp.data
        assert resp.data["created"] == [rid]

    def test_syncing_same_id_twice_updates_not_duplicates(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        self._sync(auth(worker), staffed_farm, [_payload(item, id=rid)])
        resp = self._sync(auth(worker), staffed_farm, [_payload(item, id=rid)])

        assert resp.status_code == 200, resp.data
        assert resp.data["failed"] == []
        assert resp.data["updated"] == [rid]
        assert resp.data["created"] == []
        assert InventoryUsageLog.objects.filter(item=item).count() == 1

    def test_duplicate_sync_never_raises_integrity_error(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        payload = [_payload(item, id=rid)]
        self._sync(auth(worker), staffed_farm, payload)
        # Re-send the identical batch three more times.
        for _ in range(3):
            resp = self._sync(auth(worker), staffed_farm, payload)
            assert resp.status_code == 200, resp.data
            assert resp.data["failed"] == []
        assert InventoryUsageLog.objects.filter(item=item).count() == 1

    def test_two_ids_same_item_same_day_both_land(
        self, auth, worker, staffed_farm, item
    ):
        resp = self._sync(
            auth(worker),
            staffed_farm,
            [
                _payload(item, id=str(uuid.uuid4()), quantity_used="2.00"),
                _payload(item, id=str(uuid.uuid4()), quantity_used="4.00"),
            ],
        )
        assert resp.data["failed"] == []
        assert len(resp.data["created"]) == 2

    def test_one_bad_row_does_not_reject_the_good_ones(
        self, auth, worker, staffed_farm, item
    ):
        good = _payload(item, id=str(uuid.uuid4()), usage_date="2026-02-10")
        bad = _payload(item, id=str(uuid.uuid4()), quantity_used="0")
        resp = self._sync(auth(worker), staffed_farm, [good, bad])
        # DRF validates every item in the list eagerly, so a bad row fails
        # the whole request at validation with a per-index error body.
        assert resp.status_code == 400
        assert "records" in resp.data


# ─────────────────────────────────────────────────────────────
# 3. The 24-hour edit lock
# ─────────────────────────────────────────────────────────────


class TestEditLock:
    def _detail_url(self, farm, log):
        return f"/api/farms/{farm.id}/inventory/usage/{log.id}/"

    def test_patch_within_24h_is_allowed(self, auth, worker, staffed_farm, item):
        log = InventoryUsageLog.objects.create(
            item=item, quantity_used="3.00", usage_date=date(2026, 2, 10),
            recorded_by=worker,
        )
        resp = auth(worker).patch(
            self._detail_url(staffed_farm, log),
            {"quantity_used": "4.00"},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        log.refresh_from_db()
        assert str(log.quantity_used) == "4.00"

    def test_patch_after_24h_is_rejected(self, auth, worker, staffed_farm, item):
        log = InventoryUsageLog.objects.create(
            item=item, quantity_used="3.00", usage_date=date(2026, 2, 10),
            recorded_by=worker,
        )
        _age_out(log)

        resp = auth(worker).patch(
            self._detail_url(staffed_farm, log),
            {"quantity_used": "4.00"},
            format="json",
        )
        assert resp.status_code == 400
        assert "locked" in str(resp.data).lower()
        assert "24 hours" in str(resp.data).lower()
        log.refresh_from_db()
        assert str(log.quantity_used) == "3.00"  # untouched

    def test_retried_post_after_24h_is_a_clean_400_not_a_500(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        auth(worker).post(
            _usage_url(staffed_farm), _payload(item, id=rid), format="json"
        )
        _age_out(InventoryUsageLog.objects.get(pk=rid))

        resp = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(item, id=rid, quantity_used="9.00"),
            format="json",
        )
        assert resp.status_code == 400, resp.data
        assert "locked" in str(resp.data).lower()
        assert InventoryUsageLog.objects.get(pk=rid).quantity_used == 3

    def test_bulk_sync_after_24h_reports_failed_with_id_not_500(
        self, auth, worker, staffed_farm, item
    ):
        rid = str(uuid.uuid4())
        client = auth(worker)
        client.post(_bulk_url(staffed_farm), {"records": [_payload(item, id=rid)]}, format="json")
        _age_out(InventoryUsageLog.objects.get(pk=rid))

        resp = client.post(
            _bulk_url(staffed_farm),
            {"records": [_payload(item, id=rid, quantity_used="9.00")]},
            format="json",
        )
        assert resp.status_code == 207
        assert resp.data["created"] == []
        assert resp.data["updated"] == []
        assert len(resp.data["failed"]) == 1
        assert resp.data["failed"][0]["id"] == rid
        assert "locked" in resp.data["failed"][0]["error"].lower()
        assert InventoryUsageLog.objects.get(pk=rid).quantity_used == 3


# ─────────────────────────────────────────────────────────────
# 4. Permissions — item management vs usage logging
# ─────────────────────────────────────────────────────────────


class TestPermissions:
    def _items_url(self, farm):
        return f"/api/farms/{farm.id}/inventory/items/"

    def test_worker_can_log_usage(self, auth, worker, staffed_farm, item):
        resp = auth(worker).post(
            _usage_url(staffed_farm), _payload(item), format="json"
        )
        assert resp.status_code == 201, resp.data

    def test_worker_cannot_create_an_item(self, auth, worker, staffed_farm):
        resp = auth(worker).post(
            self._items_url(staffed_farm),
            {"name": "Vitamins", "unit": "sachet", "low_stock_threshold": "5"},
            format="json",
        )
        assert resp.status_code == 403

    def test_worker_can_read_the_item_list(self, auth, worker, staffed_farm, item):
        resp = auth(worker).get(self._items_url(staffed_farm))
        assert resp.status_code == 200
        assert any(row["id"] == item.id for row in resp.data)

    def test_manager_can_create_an_item_and_set_threshold(
        self, auth, manager, staffed_farm
    ):
        resp = auth(manager).post(
            self._items_url(staffed_farm),
            {"name": "Vitamins", "unit": "sachet", "low_stock_threshold": "5"},
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["low_stock_threshold"] == "5.00"

    def test_worker_cannot_log_against_another_farms_item(
        self, auth, worker, staffed_farm, rival_farm, rival_owner
    ):
        other_item = InventoryItem.objects.create(
            farm=rival_farm, name="Bleach", unit="litre", created_by=rival_owner
        )
        resp = auth(worker).post(
            _usage_url(staffed_farm),
            _payload(other_item),
            format="json",
        )
        assert resp.status_code == 400
        assert "another farm" in str(resp.data).lower()

    def test_duplicate_item_name_on_one_farm_is_rejected(
        self, auth, manager, staffed_farm, item
    ):
        resp = auth(manager).post(
            self._items_url(staffed_farm),
            {"name": "disinfectant", "unit": "litre"},  # case-insensitive clash
            format="json",
        )
        assert resp.status_code == 400
        assert "name" in resp.data


# ─────────────────────────────────────────────────────────────
# 5. Low-stock alert — never stocked / running low / out of stock
# ─────────────────────────────────────────────────────────────


class TestLowStockAlert:
    @pytest.fixture
    def url(self, staffed_farm):
        return reverse("analytics:alerts", kwargs={"farm_pk": staffed_farm.pk})

    def _low_stock_alerts(self, response):
        return [
            a for a in response.data["alerts"] if a["id"].startswith("low-stock:")
        ]

    def test_never_stocked_item_does_not_alert(
        self, auth, owner, staffed_farm, item, url
    ):
        # `item` has a threshold of 20 but zero stock-ins and zero usage.
        alerts = self._low_stock_alerts(auth(owner).get(url))
        assert alerts == []

    def test_item_below_threshold_is_a_warning(
        self, auth, owner, staffed_farm, item, url
    ):
        InventoryStockIn.objects.create(
            item=item, quantity=10, stock_in_date=timezone.localdate(),
            recorded_by=owner,
        )
        alerts = self._low_stock_alerts(auth(owner).get(url))
        assert len(alerts) == 1
        assert alerts[0]["id"] == f"low-stock:{item.id}"
        assert alerts[0]["severity"] == "warning"
        # Trimmed, human quantities in the copy — "10", not "10.00".
        assert "10 litre left" in alerts[0]["detail"]
        assert "reorder level of 20 litre" in alerts[0]["detail"]
        # Structured fields the client uses to build a reorder message.
        assert alerts[0]["item_name"] == item.name
        assert alerts[0]["unit"] == "litre"
        assert alerts[0]["on_hand"] == "10"
        assert alerts[0]["reorder_level"] == "20"

    def test_item_at_zero_is_danger(self, auth, owner, worker, staffed_farm, item, url):
        InventoryStockIn.objects.create(
            item=item, quantity=10, stock_in_date=timezone.localdate(),
            recorded_by=owner,
        )
        InventoryUsageLog.objects.create(
            item=item, quantity_used=10, usage_date=timezone.localdate(),
            recorded_by=worker,
        )
        alerts = self._low_stock_alerts(auth(owner).get(url))
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "danger"
        assert alerts[0]["on_hand"] == "0"
        assert alerts[0]["reorder_level"] == "20"

    def test_item_below_zero_is_danger(
        self, auth, owner, worker, staffed_farm, item, url
    ):
        InventoryStockIn.objects.create(
            item=item, quantity=5, stock_in_date=timezone.localdate(),
            recorded_by=owner,
        )
        InventoryUsageLog.objects.create(
            item=item, quantity_used=8, usage_date=timezone.localdate(),
            recorded_by=worker,
        )
        alerts = self._low_stock_alerts(auth(owner).get(url))
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "danger"
        assert alerts[0]["on_hand"] == "-3"  # negative case, for "beyond stock" wording

    def test_well_stocked_item_does_not_alert(
        self, auth, owner, worker, staffed_farm, item, url
    ):
        InventoryStockIn.objects.create(
            item=item, quantity=100, stock_in_date=timezone.localdate(),
            recorded_by=owner,
        )
        InventoryUsageLog.objects.create(
            item=item, quantity_used=5, usage_date=timezone.localdate(),
            recorded_by=worker,
        )
        assert self._low_stock_alerts(auth(owner).get(url)) == []

    def test_low_stock_is_owner_manager_only_not_workers(
        self, auth, owner, worker, staffed_farm, item, url
    ):
        InventoryStockIn.objects.create(
            item=item, quantity=1, stock_in_date=timezone.localdate(),
            recorded_by=owner,
        )
        assert self._low_stock_alerts(auth(owner).get(url))
        assert self._low_stock_alerts(auth(worker).get(url)) == []
