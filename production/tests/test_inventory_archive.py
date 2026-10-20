# production/tests/test_inventory_archive.py
"""
Archiving, restoring and deleting an inventory item.

Mirrors test_house_archive.py: owner/manager can archive and restore
(workers never); an archived item drops out of the default item list (the
usage and feed-delivery pickers both read that same list) but its stock-ins,
usage logs and feed deliveries stay exactly where they are. DELETE is a
separate, stricter gate — it only succeeds with zero history anywhere, and
otherwise points the caller at archiving instead.

The offline case is the one with teeth: a worker's usage log can be queued
on a device before an item is archived and only reach the server afterwards.
recorded_at (the client's clock) is what tells that backlog entry apart from
a new one — see InventoryUsageLogSerializer.validate().
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from production.models import (
    FeedDelivery,
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


def archive_url(farm, item):
    return reverse(
        "production:inventory-item-archive", kwargs={"farm_pk": farm.pk, "pk": item.pk}
    )


def restore_url(farm, item):
    return reverse(
        "production:inventory-item-restore", kwargs={"farm_pk": farm.pk, "pk": item.pk}
    )


def detail_url(farm, item):
    return reverse(
        "production:inventory-item-detail", kwargs={"farm_pk": farm.pk, "pk": item.pk}
    )


def items_url(farm):
    return reverse("production:inventory-item-list", kwargs={"farm_pk": farm.pk})


def usage_url(farm):
    return f"/api/farms/{farm.id}/inventory/usage/"


def stock_ins_url(farm, item):
    return f"/api/farms/{farm.id}/inventory/items/{item.id}/stock-ins/"


def _usage_payload(item, usage_date="2026-02-10", **overrides):
    body = {
        "item": item.id,
        "quantity_used": "3.00",
        "usage_date": usage_date,
        "notes": "",
    }
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────
# Archive / restore
# ─────────────────────────────────────────────────────────────


class TestOwnerAndManagerCanArchiveAndRestore:
    def test_owner_can_archive(self, auth, owner, staffed_farm, item):
        resp = auth(owner).post(archive_url(staffed_farm, item))
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.archived_at is not None
        assert resp.data["archived_at"] is not None

    def test_manager_can_archive(self, auth, manager, staffed_farm, item):
        resp = auth(manager).post(archive_url(staffed_farm, item))
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.archived_at is not None

    def test_owner_can_restore(self, auth, owner, staffed_farm, item):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).post(restore_url(staffed_farm, item))
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.archived_at is None

    def test_archiving_twice_is_refused(self, auth, owner, staffed_farm, item):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).post(archive_url(staffed_farm, item))
        assert resp.status_code == 400

    def test_restoring_an_unarchived_item_is_refused(self, auth, owner, staffed_farm, item):
        resp = auth(owner).post(restore_url(staffed_farm, item))
        assert resp.status_code == 400


class TestWorkerIsRefused:
    def test_worker_gets_403_on_archive(self, auth, worker, staffed_farm, item):
        resp = auth(worker).post(archive_url(staffed_farm, item))
        assert resp.status_code == 403

    def test_worker_gets_403_on_restore(self, auth, worker, staffed_farm, item):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])
        resp = auth(worker).post(restore_url(staffed_farm, item))
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────
# Hidden from pickers, visible in reports/history
# ─────────────────────────────────────────────────────────────


class TestArchivedItemHiddenFromPickersButPresentInReports:
    def test_archived_item_is_left_out_of_the_default_item_list(
        self, auth, owner, staffed_farm, item
    ):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).get(items_url(staffed_farm))
        assert resp.status_code == 200
        assert item.pk not in [row["id"] for row in resp.data]

    def test_include_archived_param_brings_it_back(self, auth, owner, staffed_farm, item):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).get(items_url(staffed_farm) + "?include_archived=true")
        assert resp.status_code == 200
        assert item.pk in [row["id"] for row in resp.data]

    def test_stock_in_history_still_lists_the_archived_items_record(
        self, auth, owner, staffed_farm, item
    ):
        stock_in = InventoryStockIn.objects.create(
            item=item, quantity=10, stock_in_date=timezone.localdate(), recorded_by=owner,
        )
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).get(stock_ins_url(staffed_farm, item))
        assert resp.status_code == 200
        assert stock_in.id in [row["id"] for row in resp.data]

    def test_detail_view_still_returns_the_archived_item(self, auth, owner, staffed_farm, item):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).get(detail_url(staffed_farm, item))
        assert resp.status_code == 200
        assert resp.data["id"] == item.id
        assert resp.data["archived_at"] is not None


# ─────────────────────────────────────────────────────────────
# New stock-ins / feed deliveries against an archived item
# ─────────────────────────────────────────────────────────────


class TestArchivedItemBlocksNewStockMovement:
    def test_new_stock_in_against_an_archived_item_is_rejected(
        self, auth, owner, staffed_farm, item
    ):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).post(
            stock_ins_url(staffed_farm, item),
            {"quantity": "5.00", "stock_in_date": str(timezone.localdate())},
            format="json",
        )
        assert resp.status_code == 400
        assert "archived" in str(resp.data).lower()
        assert "restore" in str(resp.data).lower()

    def test_feed_delivery_against_an_archived_feed_item_is_rejected(
        self, auth, owner, staffed_farm
    ):
        feed_item = InventoryItem.objects.create(
            farm=staffed_farm,
            name="Starter feed",
            unit="sack",
            kg_per_unit="50.00",
            is_feed=True,
            feed_type=InventoryItem.FeedType.STARTER,
            created_by=owner,
            archived_at=timezone.now(),
        )
        resp = auth(owner).post(
            f"/api/farms/{staffed_farm.id}/feed-deliveries/",
            {
                "delivery_date": str(timezone.localdate()),
                "feed_type": "STARTER",
                "quantity_kg": "250.00",
                "inventory_item": feed_item.id,
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "archived" in str(resp.data).lower()
        assert FeedDelivery.objects.count() == 0


# ─────────────────────────────────────────────────────────────
# Offline usage logs queued before archiving
# ─────────────────────────────────────────────────────────────


class TestOfflineUsageLogAcrossArchiving:
    def test_new_usage_against_an_archived_item_is_rejected(
        self, auth, worker, staffed_farm, item
    ):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(worker).post(
            usage_url(staffed_farm),
            _usage_payload(item, recorded_at=timezone.now().isoformat()),
            format="json",
        )
        assert resp.status_code == 400
        assert "archived" in str(resp.data).lower()
        assert InventoryUsageLog.objects.filter(item=item).count() == 0

    def test_offline_usage_recorded_before_archiving_is_accepted_on_late_sync(
        self, auth, worker, staffed_farm, item
    ):
        recorded_at = timezone.now() - timedelta(hours=6)
        item.archived_at = timezone.now() - timedelta(hours=1)
        item.save(update_fields=["archived_at"])

        resp = auth(worker).post(
            usage_url(staffed_farm),
            _usage_payload(item, recorded_at=recorded_at.isoformat()),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert InventoryUsageLog.objects.filter(item=item).count() == 1

    def test_offline_bulk_sync_recorded_before_archiving_is_accepted(
        self, auth, worker, staffed_farm, item
    ):
        recorded_at = timezone.now() - timedelta(hours=6)
        item.archived_at = timezone.now() - timedelta(hours=1)
        item.save(update_fields=["archived_at"])

        resp = auth(worker).post(
            f"/api/farms/{staffed_farm.id}/inventory/usage/bulk-sync/",
            {"records": [_usage_payload(item, recorded_at=recorded_at.isoformat())]},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["failed"] == []
        assert len(resp.data["created"]) == 1


# ─────────────────────────────────────────────────────────────
# DELETE — only with no history anywhere
# ─────────────────────────────────────────────────────────────


class TestDelete:
    def test_item_with_no_history_can_be_deleted(self, auth, owner, staffed_farm, item):
        resp = auth(owner).delete(detail_url(staffed_farm, item))
        assert resp.status_code == 204
        assert not InventoryItem.objects.filter(pk=item.pk).exists()

    def test_item_with_a_stock_in_cannot_be_deleted(
        self, auth, owner, staffed_farm, item
    ):
        InventoryStockIn.objects.create(
            item=item, quantity=10, stock_in_date=timezone.localdate(), recorded_by=owner,
        )
        resp = auth(owner).delete(detail_url(staffed_farm, item))
        assert resp.status_code == 400
        assert "history" in str(resp.data).lower()
        assert "archive" in str(resp.data).lower()
        assert InventoryItem.objects.filter(pk=item.pk).exists()

    def test_item_with_a_usage_log_cannot_be_deleted(
        self, auth, owner, worker, staffed_farm, item
    ):
        InventoryUsageLog.objects.create(
            item=item, quantity_used=3, usage_date=timezone.localdate(), recorded_by=worker,
        )
        resp = auth(owner).delete(detail_url(staffed_farm, item))
        assert resp.status_code == 400
        assert "history" in str(resp.data).lower()
        assert InventoryItem.objects.filter(pk=item.pk).exists()

    def test_archived_item_with_no_history_can_still_be_deleted(
        self, auth, owner, staffed_farm, item
    ):
        item.archived_at = timezone.now()
        item.save(update_fields=["archived_at"])

        resp = auth(owner).delete(detail_url(staffed_farm, item))
        assert resp.status_code == 204

    def test_worker_cannot_delete_an_item(self, auth, worker, staffed_farm, item):
        resp = auth(worker).delete(detail_url(staffed_farm, item))
        assert resp.status_code == 403
        assert InventoryItem.objects.filter(pk=item.pk).exists()

    def test_can_delete_field_reflects_history(
        self, auth, owner, worker, staffed_farm, item
    ):
        resp = auth(owner).get(detail_url(staffed_farm, item))
        assert resp.data["can_delete"] is True

        InventoryUsageLog.objects.create(
            item=item, quantity_used=3, usage_date=timezone.localdate(), recorded_by=worker,
        )
        resp = auth(owner).get(detail_url(staffed_farm, item))
        assert resp.data["can_delete"] is False
