# production/tests/test_feed_inventory_consistency.py
"""
Feed is recorded once — arriving as a delivery, used in a daily record —
and every other feed figure is derived from those two. This pins the
invariant end to end through the real API:

    level(item) = kg delivered to that item - kg recorded against it in
                  daily records

and Σ level(item) == feed_balance(farm), whenever every delivery and daily
record names its item.

Also covers the gaps that used to let the two ledgers drift: manual
stock-in/usage-log against a feed item, a delivery or feed-bearing daily
record with no item named, and a correction to feed_sacks/feed_item.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from analytics.services import feed_balance
from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    RecordCorrection,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def feed_item(staffed_farm, owner):
    return InventoryItem.objects.create(
        farm=staffed_farm,
        name="Broiler feed",
        unit="sack",
        kg_per_unit=Decimal("50"),
        is_feed=True,
        low_stock_threshold=Decimal("5"),
        created_by=owner,
    )


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="INV-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


def _delivery_url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/"


def _record_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/"


def _stock_in_url(farm, item):
    return f"/api/farms/{farm.id}/inventory/items/{item.id}/stock-ins/"


def _usage_url(farm):
    return f"/api/farms/{farm.id}/inventory/usage/"


def _correct_url(farm, batch, record):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/{record.id}/correct/"


def _item_level_kg(item):
    item.refresh_from_db()
    return item.current_quantity * item.kg_per_unit


class TestInvariantThroughRealApi:
    def test_delivery_then_daily_records_then_correction_keep_the_ledgers_equal(
        self, auth, owner, worker, staffed_farm, feed_item, batch
    ):
        # 1. Delivery naming the item.
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            {
                "delivery_date": "2026-01-01",
                "feed_type": FeedDelivery.FeedType.STARTER,
                "quantity_kg": "1000.00",
                "unit_cost": "30.00",
                "inventory_item": feed_item.id,
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data

        # 2. Online daily record with feed in sacks.
        resp = auth(worker).post(
            _record_url(staffed_farm, batch),
            {
                "record_date": "2026-01-02",
                "mortality_disease": 0, "mortality_heat": 0,
                "mortality_culled": 0, "mortality_unknown": 0,
                "feed_sacks": "5", "feed_item": feed_item.id, "feed_kg": "250.00",
                "notes": "",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data
        record_id = resp.data["id"]

        # 3. A second day via bulk sync (offline device).
        rid = str(uuid.uuid4())
        resp = auth(worker).post(
            f"{_record_url(staffed_farm, batch)}bulk-sync/",
            {
                "records": [
                    {
                        "id": rid,
                        "record_date": "2026-01-03",
                        "mortality_disease": 0, "mortality_heat": 0,
                        "mortality_culled": 0, "mortality_unknown": 0,
                        "feed_sacks": "4", "feed_item": feed_item.id,
                        "feed_kg": "200.00", "notes": "",
                    }
                ]
            },
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["created"] == [rid]

        self._assert_invariant(staffed_farm, feed_item)

        # 4. A correction to feed_sacks moves both feed_kg and the linked log.
        record = DailyRecord.objects.get(pk=record_id)
        resp = auth(owner).post(
            _correct_url(staffed_farm, batch, record),
            {
                "feed_sacks": "6",
                "feed_item": feed_item.id,
                "reason": "Recount found an extra sack fed that day.",
            },
            format="json",
        )
        assert resp.status_code == 200, resp.data
        record.refresh_from_db()
        assert record.feed_sacks == Decimal("6.00")
        assert record.feed_kg == Decimal("300.00")  # 6 x 50

        log = InventoryUsageLog.objects.get(daily_record=record)
        assert log.quantity_used == Decimal("6.00")

        correction = RecordCorrection.objects.get(object_id=record.id)
        assert set(correction.changed_fields) >= {"feed_sacks", "feed_kg"}
        assert correction.new_values["feed_sacks"] == "6.00"

        self._assert_invariant(staffed_farm, feed_item)

    def test_a_plain_kg_record_differs_from_the_item_level_by_exactly_its_kg(
        self, auth, owner, worker, staffed_farm, feed_item, batch
    ):
        # Baseline: one delivery, one sack-based record naming the item —
        # the two ledgers agree exactly.
        auth(owner).post(
            _delivery_url(staffed_farm),
            {
                "delivery_date": "2026-01-01",
                "feed_type": FeedDelivery.FeedType.STARTER,
                "quantity_kg": "1000.00",
                "unit_cost": "30.00",
                "inventory_item": feed_item.id,
            },
            format="json",
        )
        auth(worker).post(
            _record_url(staffed_farm, batch),
            {
                "record_date": "2026-01-02",
                "mortality_disease": 0, "mortality_heat": 0,
                "mortality_culled": 0, "mortality_unknown": 0,
                "feed_sacks": "5", "feed_item": feed_item.id, "feed_kg": "250.00",
                "notes": "",
            },
            format="json",
        )
        self._assert_invariant(staffed_farm, feed_item)
        level_before = _item_level_kg(feed_item)
        balance_before = feed_balance(staffed_farm)["balance_kg"]

        # A plain-kg record — no feed_item — predates this farm tracking
        # feed as inventory, or was written before this rule existed.
        # DailyRecordSerializer now refuses a NEW one on a farm with feed
        # items, so this simulates the legacy row the way it actually
        # entered the database: straight through the ORM, bypassing the
        # API exactly as an old, already-committed row would have.
        legacy_kg = Decimal("77.30")
        DailyRecord.objects.create(
            batch=batch,
            record_date=date(2026, 1, 3),
            feed_kg=legacy_kg,
            recorded_by=worker,
        )

        # It counts in feed_balance() (consumed_kg sums every DailyRecord,
        # feed_item or not) but was never converted into an
        # InventoryUsageLog — a record with no feed_item never touches the
        # inventory ledger at all. So the item's own level is untouched,
        # while feed_balance() drops by exactly this record's kg: the two
        # numbers now differ by precisely legacy_kg, not by drift.
        level_after = _item_level_kg(feed_item)
        balance_after = feed_balance(staffed_farm)["balance_kg"]

        assert level_after == level_before
        assert balance_after == balance_before - legacy_kg
        assert level_after - balance_after == legacy_kg

    def _assert_invariant(self, farm, item):
        # The real invariant: the INVENTORY ledger's level (Σ InventoryStockIn
        # − Σ InventoryUsageLog, converted to kg) must equal feed_balance(),
        # which is computed straight from the FEED ledger (Σ FeedDelivery.
        # quantity_kg − Σ DailyRecord.feed_kg). These are two independent
        # tables; comparing the feed ledger to itself would prove nothing.
        level = _item_level_kg(item)
        balance = feed_balance(farm)["balance_kg"]
        assert level == balance, f"item level {level} != feed_balance {balance}"


class TestManualEntryRejected:
    def test_manual_stock_in_on_feed_item_is_rejected(
        self, auth, owner, staffed_farm, feed_item
    ):
        resp = auth(owner).post(
            _stock_in_url(staffed_farm, feed_item),
            {"quantity": "10", "stock_in_date": "2026-01-01", "note": ""},
            format="json",
        )
        assert resp.status_code == 400
        assert "feed delivery" in str(resp.data).lower()
        assert InventoryStockIn.objects.count() == 0

    def test_manual_stock_in_on_non_feed_item_still_works(
        self, auth, owner, staffed_farm
    ):
        item = InventoryItem.objects.create(
            farm=staffed_farm, name="Disinfectant", unit="litre", created_by=owner,
        )
        resp = auth(owner).post(
            _stock_in_url(staffed_farm, item),
            {"quantity": "10", "stock_in_date": "2026-01-01", "note": ""},
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert InventoryStockIn.objects.filter(item=item).count() == 1

    def test_manual_usage_log_on_feed_item_is_rejected(
        self, auth, worker, staffed_farm, feed_item
    ):
        resp = auth(worker).post(
            _usage_url(staffed_farm),
            {"item": feed_item.id, "quantity_used": "1", "usage_date": "2026-01-01"},
            format="json",
        )
        assert resp.status_code == 400
        assert "daily record" in str(resp.data).lower()
        assert InventoryUsageLog.objects.count() == 0

    def test_manual_usage_log_on_non_feed_item_still_works(
        self, auth, worker, staffed_farm
    ):
        item = InventoryItem.objects.create(
            farm=staffed_farm, name="Disinfectant", unit="litre", created_by=worker,
        )
        resp = auth(worker).post(
            _usage_url(staffed_farm),
            {"item": item.id, "quantity_used": "1", "usage_date": "2026-01-01"},
            format="json",
        )
        assert resp.status_code == 201, resp.data


class TestNamingRequired:
    def test_delivery_with_no_item_is_rejected_when_farm_has_feed_items(
        self, auth, owner, staffed_farm, feed_item
    ):
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            {
                "delivery_date": "2026-01-01",
                "feed_type": FeedDelivery.FeedType.STARTER,
                "quantity_kg": "1000.00",
                "unit_cost": "30.00",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "inventory_item" in resp.data
        assert FeedDelivery.objects.count() == 0

    def test_delivery_with_no_item_is_allowed_when_farm_has_no_feed_items(
        self, auth, owner, staffed_farm
    ):
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            {
                "delivery_date": "2026-01-01",
                "feed_type": FeedDelivery.FeedType.STARTER,
                "quantity_kg": "1000.00",
                "unit_cost": "30.00",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_daily_record_with_feed_but_no_item_is_rejected(
        self, auth, worker, staffed_farm, feed_item, batch
    ):
        resp = auth(worker).post(
            _record_url(staffed_farm, batch),
            {
                "record_date": "2026-01-02",
                "mortality_disease": 0, "mortality_heat": 0,
                "mortality_culled": 0, "mortality_unknown": 0,
                "feed_kg": "250.00", "notes": "",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "feed_item" in resp.data
        assert DailyRecord.objects.count() == 0

    def test_daily_record_with_zero_feed_and_no_item_is_allowed(
        self, auth, worker, staffed_farm, feed_item, batch
    ):
        resp = auth(worker).post(
            _record_url(staffed_farm, batch),
            {
                "record_date": "2026-01-02",
                "mortality_disease": 0, "mortality_heat": 0,
                "mortality_culled": 0, "mortality_unknown": 0,
                "feed_kg": "0", "notes": "",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data


class TestLowStockUsesCorrectedLevel:
    def test_low_stock_alert_triggers_from_the_corrected_level(
        self, auth, owner, worker, staffed_farm, feed_item, batch
    ):
        from analytics.alerts import compute_alerts

        # Deliver 6 sacks worth, consume 5 — 1 sack left, at/under the
        # item's threshold of 5... use a farm-specific tighter threshold.
        feed_item.low_stock_threshold = Decimal("2")
        feed_item.save(update_fields=["low_stock_threshold"])

        auth(owner).post(
            _delivery_url(staffed_farm),
            {
                "delivery_date": "2026-01-01",
                "feed_type": FeedDelivery.FeedType.STARTER,
                "quantity_kg": "300.00",  # 6 sacks
                "unit_cost": "30.00",
                "inventory_item": feed_item.id,
            },
            format="json",
        )
        auth(worker).post(
            _record_url(staffed_farm, batch),
            {
                "record_date": "2026-01-02",
                "mortality_disease": 0, "mortality_heat": 0,
                "mortality_culled": 0, "mortality_unknown": 0,
                "feed_sacks": "5", "feed_item": feed_item.id, "feed_kg": "250.00",
                "notes": "",
            },
            format="json",
        )

        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("1.00")

        alerts = compute_alerts(staffed_farm)
        low_stock = [a for a in alerts if a["id"] == f"low-stock:{feed_item.id}"]
        assert len(low_stock) == 1


class TestAdminFeedFieldsReadOnly:
    def test_daily_record_admin_feed_fields_are_read_only(self):
        from production.admin import DailyRecordAdmin
        from production.models import DailyRecord as DR

        admin_instance = DailyRecordAdmin(DR, None)
        assert "feed_kg" in admin_instance.readonly_fields
        assert "feed_sacks" in admin_instance.readonly_fields
        assert "feed_item" in admin_instance.readonly_fields


class TestSeedInvariant:
    def test_seeded_demo_farm_has_equal_feed_item_level_and_balance(self, settings, db):
        settings.DEBUG = True
        call_command("seed_demo_data", "--reset")

        from farms.models import Farm

        farm = Farm.objects.get(name="Santos Broiler Farm")
        item = InventoryItem.objects.get(farm=farm, name="Broiler feed")

        level = _item_level_kg(item)
        balance = feed_balance(farm)["balance_kg"]

        # Feed is converted once now — feed_kg is derived from feed_sacks
        # (and a delivery's quantity_kg from the sack count it implies),
        # so the two ledgers land on the same number to the cent, not
        # just within a rounding residual.
        assert level == balance, (level, balance)

    def test_reset_leaves_no_orphaned_usage_logs_and_reseeding_reproduces_the_same_numbers(
        self, settings, db, owner
    ):
        settings.DEBUG = True
        from farms.models import Farm

        # An unrelated farm's usage log — must survive the demo farm's
        # reset untouched, proving the cleanup is scoped to that farm.
        other_farm = Farm.objects.create(name="Some Other Farm", owner=owner)
        other_item = InventoryItem.objects.create(
            farm=other_farm, name="Their feed", unit="sack",
            kg_per_unit=Decimal("50"), is_feed=True, created_by=owner,
        )
        InventoryStockIn.objects.create(
            item=other_item, quantity=Decimal("10"), stock_in_date=date(2026, 1, 1),
        )
        other_log = InventoryUsageLog.objects.create(
            item=other_item, quantity_used=Decimal("2"), usage_date=date(2026, 1, 1),
        )

        call_command("seed_demo_data", "--reset", "--seed", "7")
        farm = Farm.objects.get(name="Santos Broiler Farm")
        item = InventoryItem.objects.get(farm=farm, name="Broiler feed")
        first_level = _item_level_kg(item)
        first_balance = feed_balance(farm)["balance_kg"]
        first_usage_log_count = InventoryUsageLog.objects.filter(item=item).count()

        # Reset again with the SAME seed. If _reset() left orphaned usage
        # logs behind (InventoryUsageLog.daily_record is SET_NULL, so the
        # cascade-delete of the old batches only unlinks them, it doesn't
        # remove them), the item's used_total would double-count and the
        # second seeding would disagree with the first.
        call_command("seed_demo_data", "--reset", "--seed", "7")

        assert not InventoryUsageLog.objects.filter(
            item__farm=farm, daily_record__isnull=True
        ).exists()

        item.refresh_from_db()
        second_level = _item_level_kg(item)
        second_balance = feed_balance(farm)["balance_kg"]
        second_usage_log_count = InventoryUsageLog.objects.filter(item=item).count()

        assert second_level == first_level
        assert second_balance == first_balance
        assert second_usage_log_count == first_usage_log_count

        # The other farm's usage log is untouched.
        assert InventoryUsageLog.objects.filter(pk=other_log.pk).exists()
