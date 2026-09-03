# production/tests/test_daily_record_inventory_link.py
"""
A daily record's feed line also draws the inventory item down.

Feed consumption used to live in two unconnected places: DailyRecord.feed_kg
(per batch, drives FCR) and InventoryUsageLog (per farm, drives the item
balance). A worker who fed a batch and recorded it in Daily Entry had NOT
reduced the inventory — they had to record the same feeding twice.

Now every path that writes a DailyRecord with feed_item + feed_sacks set
also creates or updates a linked InventoryUsageLog, in the same
transaction. The drawdown is in the item's own unit — sacks, not kg — so
the balance comes out in sacks. This mirrors the feed-delivery-with-stock
endpoint on the consumption side.

The rollback test is the reason it is one transaction and not two calls: a
half-written pair cannot survive.
"""
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest

from production.models import (
    Batch,
    DailyRecord,
    Harvest,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="DR-INV-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


@pytest.fixture
def feed_item(farm, owner):
    item = InventoryItem.objects.create(
        farm=farm,
        name="Broiler Starter",
        unit="sack",
        kg_per_unit=Decimal("50"),
        created_by=owner,
    )
    # A stock-in so the balance is a real number to watch move.
    InventoryStockIn.objects.create(
        item=item, quantity=Decimal("100"), stock_in_date=date(2026, 1, 1)
    )
    return item


@pytest.fixture
def other_feed_item(farm, owner):
    item = InventoryItem.objects.create(
        farm=farm,
        name="Broiler Grower",
        unit="sack",
        kg_per_unit=Decimal("50"),
        created_by=owner,
    )
    InventoryStockIn.objects.create(
        item=item, quantity=Decimal("100"), stock_in_date=date(2026, 1, 1)
    )
    return item


def _url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/"


def _bulk_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/bulk-sync/"


def _payload(record_date, **overrides):
    body = {
        "record_date": record_date,
        "mortality_disease": 0,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": "10.00",
        "notes": "",
    }
    body.update(overrides)
    return body


def _sacks(record_date, item, sacks, kg, **overrides):
    return _payload(
        record_date,
        feed_sacks=str(sacks),
        feed_item=item.id,
        feed_kg=kg,
        **overrides,
    )


class TestCreatesLinkedLog:
    def test_sacks_entry_creates_linked_log_with_sack_count_not_kg(
        self, auth, owner, farm, batch, feed_item
    ):
        response = auth(owner).post(
            _url(farm, batch),
            _sacks("2026-01-05", feed_item, 5, "250.00"),
            format="json",
        )
        assert response.status_code == 201, response.data

        record = DailyRecord.objects.get(pk=response.data["id"])
        log = InventoryUsageLog.objects.get(daily_record=record)
        assert log.item_id == feed_item.id
        # The sack count, not the 250 kg figure.
        assert log.quantity_used == Decimal("5.00")
        assert log.usage_date == date(2026, 1, 5)
        assert log.recorded_by_id == owner.id

    def test_inventory_balance_drops_by_exactly_that_many_sacks(
        self, auth, owner, farm, batch, feed_item
    ):
        before = feed_item.current_quantity
        assert before == Decimal("100")

        auth(owner).post(
            _url(farm, batch),
            _sacks("2026-01-05", feed_item, 5, "250.00"),
            format="json",
        )

        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("95")

    def test_record_without_feed_item_creates_no_log(
        self, auth, owner, farm, batch, feed_item
    ):
        response = auth(owner).post(
            _url(farm, batch),
            _payload("2026-01-05", feed_kg="123.45"),
            format="json",
        )
        assert response.status_code == 201, response.data

        assert InventoryUsageLog.objects.count() == 0
        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("100")


class TestEditWithinWindow:
    def test_editing_sacks_updates_the_linked_log_not_a_second_one(
        self, auth, owner, farm, batch, feed_item
    ):
        create = auth(owner).post(
            _url(farm, batch),
            _sacks("2026-01-05", feed_item, 5, "250.00"),
            format="json",
        )
        pk = create.data["id"]
        log_id = InventoryUsageLog.objects.get(daily_record_id=pk).id

        patch = auth(owner).patch(
            f"{_url(farm, batch)}{pk}/",
            _sacks("2026-01-05", feed_item, 8, "400.00"),
            format="json",
        )
        assert patch.status_code == 200, patch.data

        logs = InventoryUsageLog.objects.filter(daily_record_id=pk)
        assert logs.count() == 1
        assert logs.get().id == log_id  # same row, updated in place
        assert logs.get().quantity_used == Decimal("8.00")

        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("92")  # 100 - 8

    def test_changing_feed_item_reverses_the_old_and_draws_the_new(
        self, auth, owner, farm, batch, feed_item, other_feed_item
    ):
        create = auth(owner).post(
            _url(farm, batch),
            _sacks("2026-01-05", feed_item, 5, "250.00"),
            format="json",
        )
        pk = create.data["id"]

        patch = auth(owner).patch(
            f"{_url(farm, batch)}{pk}/",
            _sacks("2026-01-05", other_feed_item, 5, "250.00"),
            format="json",
        )
        assert patch.status_code == 200, patch.data

        # Exactly one log, now against the new item.
        logs = InventoryUsageLog.objects.filter(daily_record_id=pk)
        assert logs.count() == 1
        assert logs.get().item_id == other_feed_item.id

        feed_item.refresh_from_db()
        other_feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("100")  # reversed
        assert other_feed_item.current_quantity == Decimal("95")  # drawn

    def test_clearing_feed_item_deletes_the_linked_log(
        self, auth, owner, farm, batch, feed_item
    ):
        create = auth(owner).post(
            _url(farm, batch),
            _sacks("2026-01-05", feed_item, 5, "250.00"),
            format="json",
        )
        pk = create.data["id"]
        assert InventoryUsageLog.objects.filter(daily_record_id=pk).exists()

        patch = auth(owner).patch(
            f"{_url(farm, batch)}{pk}/",
            _payload("2026-01-05", feed_kg="250.00", feed_sacks=None, feed_item=None),
            format="json",
        )
        assert patch.status_code == 200, patch.data

        assert not InventoryUsageLog.objects.filter(daily_record_id=pk).exists()
        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("100")


class TestBulkSyncIdempotency:
    def test_resyncing_the_same_record_does_not_double_the_drawdown(
        self, auth, owner, farm, batch, feed_item
    ):
        rid = str(uuid.uuid4())
        record = {"id": rid, **_sacks("2026-01-05", feed_item, 6, "300.00")}

        first = auth(owner).post(
            _bulk_url(farm, batch), {"records": [record]}, format="json"
        )
        assert first.status_code == 200, first.data
        assert first.data["created"] == [rid]

        second = auth(owner).post(
            _bulk_url(farm, batch), {"records": [record]}, format="json"
        )
        assert second.status_code == 200, second.data
        assert second.data["updated"] == [rid]

        assert InventoryUsageLog.objects.filter(daily_record_id=rid).count() == 1
        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("94")  # 100 - 6, once

    def test_bulk_sync_create_links_a_log(
        self, auth, owner, farm, batch, feed_item
    ):
        rid = str(uuid.uuid4())
        auth(owner).post(
            _bulk_url(farm, batch),
            {"records": [{"id": rid, **_sacks("2026-01-05", feed_item, 3, "150.00")}]},
            format="json",
        )
        log = InventoryUsageLog.objects.get(daily_record_id=rid)
        assert log.quantity_used == Decimal("3.00")


class TestTransactionAtomicity:
    def test_usage_log_write_failure_rolls_back_the_daily_record(
        self, auth, owner, farm, batch, feed_item
    ):
        """Force the failure — neither row may exist afterwards."""
        with mock.patch.object(
            InventoryUsageLog.objects,
            "create",
            side_effect=RuntimeError("forced failure after the daily record is written"),
        ):
            with pytest.raises(RuntimeError):
                auth(owner).post(
                    _url(farm, batch),
                    _sacks("2026-01-05", feed_item, 5, "250.00"),
                    format="json",
                )

        assert DailyRecord.objects.count() == 0
        assert InventoryUsageLog.objects.count() == 0
        # The denormalised batch total rolled back with the record.
        batch.refresh_from_db()
        assert batch.total_feed_kg == Decimal("0")


class TestFcrUnaffected:
    def test_fcr_is_unchanged_by_the_inventory_link(
        self, auth, owner, farm, feed_item
    ):
        """
        A batch fed via sacks (which now also writes a usage log) and one
        fed in plain kg must land on the same FCR — the usage log is a
        parallel record, it never touches feed_kg.
        """

        def _harvested(code, house_name, use_sacks):
            house = House.objects.create(
                farm=farm, name=house_name, capacity=5000
            )
            b = Batch.objects.create(
                house=house,
                batch_code=code,
                initial_bird_count=1000,
                start_date=date(2026, 1, 1),
                created_by=owner,
            )
            body = (
                _sacks("2026-01-02", feed_item, 5, "250.00")
                if use_sacks
                else _payload("2026-01-02", feed_kg="250.00")
            )
            resp = auth(owner).post(_url(farm, b), body, format="json")
            assert resp.status_code == 201, resp.data

            Harvest.objects.create(
                batch=b,
                harvest_date=date(2026, 2, 12),
                birds_harvested=950,
                total_weight_kg=Decimal("150.00"),
                recorded_by=owner,
            )
            b.status = Batch.Status.HARVESTED
            b.save(update_fields=["status"])
            b.refresh_from_db()
            return b

        kg_batch = _harvested("KG-1", "House KG", use_sacks=False)
        sack_batch = _harvested("SACK-1", "House SACK", use_sacks=True)

        assert kg_batch.total_feed_kg == Decimal("250.00")
        assert sack_batch.total_feed_kg == Decimal("250.00")
        assert (
            kg_batch.feed_conversion_ratio
            == sack_batch.feed_conversion_ratio
            == Decimal("1.667")
        )
        # And the sack batch really did draw its item down.
        feed_item.refresh_from_db()
        assert feed_item.current_quantity == Decimal("95")
