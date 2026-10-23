# production/tests/test_worker_money_reads.py
"""
Workers must not read money.

The manuscript says sales and buyer payments are seen only by owners and
managers, and OwnerManagerAnalyticsPermission already says "money figures
are not worker data" for the analytics side. This pins the same rule on
the production endpoints that still carried price, revenue, buyer
identity, or feed cost in a worker's GET: sales, payments, feed
deliveries, and the harvest record read owner/manager only (403 for a
worker); feed stock stays open to every member but drops total_feed_cost
from a worker's response instead of refusing the whole endpoint.
"""
from datetime import date
from decimal import Decimal

import pytest

from farms.models import FarmMembership
from production.models import Batch, DailyRecord, FeedDelivery, House, SaleEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed M", capacity=5000)


@pytest.fixture
def batch(house, owner):
    b = Batch.objects.create(
        house=house,
        batch_code="M-1",
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


@pytest.fixture
def delivery(staffed_farm):
    return FeedDelivery.objects.create(
        farm=staffed_farm,
        other_source="FeedCo",
        delivery_date=date(2026, 1, 5),
        feed_type=FeedDelivery.FeedType.STARTER,
        quantity_kg=Decimal("500.00"),
        unit_cost=Decimal("30.00"),
    )


def _sales_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/"


def _sale_detail_url(farm, batch, sale):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/{sale.id}/"


def _payments_url(farm, batch, sale):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/{sale.id}/payments/"


def _feed_deliveries_url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/"


def _feed_stock_url(farm):
    return f"/api/farms/{farm.id}/feed-stock/"


def _harvest_detail_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/harvest/detail/"


class TestSalesReadIsOwnerManagerOnly:
    def test_worker_gets_403_listing_sales(self, auth, worker, staffed_farm, batch, sale):
        resp = auth(worker).get(_sales_url(staffed_farm, batch))
        assert resp.status_code == 403

    def test_worker_with_no_house_assignment_is_still_refused(
        self, auth, worker, staffed_farm, batch, sale
    ):
        membership = FarmMembership.objects.get(farm=staffed_farm, user=worker)
        assert membership.houses.count() == 0  # unassigned — reads aren't house-scoped
        resp = auth(worker).get(_sales_url(staffed_farm, batch))
        assert resp.status_code == 403

    def test_worker_gets_403_reading_a_single_sale(
        self, auth, worker, staffed_farm, batch, sale
    ):
        resp = auth(worker).get(_sale_detail_url(staffed_farm, batch, sale))
        assert resp.status_code == 403

    def test_owner_can_list_sales(self, auth, owner, staffed_farm, batch, sale):
        resp = auth(owner).get(_sales_url(staffed_farm, batch))
        assert resp.status_code == 200
        row = next(r for r in resp.data if r["id"] == str(sale.id))
        assert row["revenue"] == "70000.00"

    def test_manager_can_list_sales(self, auth, manager, staffed_farm, batch, sale):
        resp = auth(manager).get(_sales_url(staffed_farm, batch))
        assert resp.status_code == 200
        row = next(r for r in resp.data if r["id"] == str(sale.id))
        assert row["revenue"] == "70000.00"


class TestPaymentsReadIsOwnerManagerOnly:
    def test_worker_gets_403_listing_payments(
        self, auth, worker, staffed_farm, batch, sale
    ):
        resp = auth(worker).get(_payments_url(staffed_farm, batch, sale))
        assert resp.status_code == 403

    def test_owner_can_list_payments(self, auth, owner, staffed_farm, batch, sale):
        auth(owner).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        resp = auth(owner).get(_payments_url(staffed_farm, batch, sale))
        assert resp.status_code == 200
        assert resp.data[0]["amount"] == "2000.00"

    def test_manager_can_list_payments(self, auth, manager, staffed_farm, batch, sale):
        auth(manager).post(
            _payments_url(staffed_farm, batch, sale),
            {"amount": "2000.00", "received_date": "2026-01-11"},
            format="json",
        )
        resp = auth(manager).get(_payments_url(staffed_farm, batch, sale))
        assert resp.status_code == 200


class TestFeedDeliveriesReadIsOwnerManagerOnly:
    def test_worker_gets_403_listing_feed_deliveries(
        self, auth, worker, staffed_farm, delivery
    ):
        resp = auth(worker).get(_feed_deliveries_url(staffed_farm))
        assert resp.status_code == 403

    def test_owner_can_list_feed_deliveries(self, auth, owner, staffed_farm, delivery):
        resp = auth(owner).get(_feed_deliveries_url(staffed_farm))
        assert resp.status_code == 200
        assert resp.data[0]["unit_cost"] == "30.00"

    def test_manager_can_list_feed_deliveries(
        self, auth, manager, staffed_farm, delivery
    ):
        resp = auth(manager).get(_feed_deliveries_url(staffed_farm))
        assert resp.status_code == 200
        assert resp.data[0]["unit_cost"] == "30.00"


class TestFeedStockHidesCostFromWorkers:
    def test_worker_sees_feed_stock_without_cost(
        self, auth, worker, staffed_farm, delivery
    ):
        resp = auth(worker).get(_feed_stock_url(staffed_farm))
        assert resp.status_code == 200
        assert "delivered_kg" in resp.data
        assert "total_feed_cost" not in resp.data

    def test_owner_sees_feed_stock_with_cost(self, auth, owner, staffed_farm, delivery):
        resp = auth(owner).get(_feed_stock_url(staffed_farm))
        assert resp.status_code == 200
        assert resp.data["total_feed_cost"] == "15000.00"

    def test_manager_sees_feed_stock_with_cost(
        self, auth, manager, staffed_farm, delivery
    ):
        resp = auth(manager).get(_feed_stock_url(staffed_farm))
        assert resp.status_code == 200
        assert resp.data["total_feed_cost"] == "15000.00"


class TestHarvestDetailReadIsOwnerManagerOnly:
    def test_worker_gets_403_reading_harvest_detail(
        self, auth, owner, worker, staffed_farm, batch, sale
    ):
        auth(owner).post(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/close/", {}, format="json"
        )
        resp = auth(worker).get(_harvest_detail_url(staffed_farm, batch))
        assert resp.status_code == 403

    def test_owner_can_read_harvest_detail(self, auth, owner, staffed_farm, batch, sale):
        auth(owner).post(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/close/", {}, format="json"
        )
        resp = auth(owner).get(_harvest_detail_url(staffed_farm, batch))
        assert resp.status_code == 200
        assert resp.data["revenue"] == "70000.00"

    def test_manager_can_read_harvest_detail(
        self, auth, owner, manager, staffed_farm, batch, sale
    ):
        auth(owner).post(
            f"/api/farms/{staffed_farm.id}/batches/{batch.id}/close/", {}, format="json"
        )
        resp = auth(manager).get(_harvest_detail_url(staffed_farm, batch))
        assert resp.status_code == 200
        assert resp.data["revenue"] == "70000.00"
