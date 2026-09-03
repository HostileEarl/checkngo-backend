# production/tests/test_feed_delivery_with_stock.py
"""
Recording a feed delivery that is also an inventory item.

A delivery is two records: FeedDelivery (per farm, feeds cost analysis and
feed margin) and InventoryStockIn (per item, feeds the balance). The
/with-stock/ endpoint writes both in one transaction so a manager records
it once and the two can never drift.

The rollback test is the reason this is one endpoint and not two frontend
calls: if the stock-in fails after the delivery is written, the delivery
must roll back too, or a resubmit double-counts the cost.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest

from production.models import FeedDelivery, InventoryItem, InventoryStockIn

pytestmark = pytest.mark.django_db


@pytest.fixture
def feed_item(staffed_farm, owner):
    return InventoryItem.objects.create(
        farm=staffed_farm,
        name="Broiler feed",
        unit="sack",
        kg_per_unit=Decimal("50"),
        created_by=owner,
    )


@pytest.fixture
def weightless_item(staffed_farm, owner):
    return InventoryItem.objects.create(
        farm=staffed_farm,
        name="Disinfectant",
        unit="litre",
        kg_per_unit=None,
        created_by=owner,
    )


def _url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/with-stock/"


def _payload(**overrides):
    body = {
        "delivery_date": date(2026, 1, 10).isoformat(),
        "feed_type": FeedDelivery.FeedType.STARTER,
        "quantity_kg": "1200.00",
        "unit_cost": "30.00",
    }
    body.update(overrides)
    return body


def test_with_item_creates_both_records_and_converts_units(
    auth, owner, staffed_farm, feed_item
):
    response = auth(owner).post(
        _url(staffed_farm), _payload(inventory_item=feed_item.id), format="json"
    )
    assert response.status_code == 201, response.data

    assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1
    delivery = FeedDelivery.objects.get(farm=staffed_farm)
    assert delivery.quantity_kg == Decimal("1200.00")
    # Model derives total from unit_cost x quantity.
    assert delivery.total_cost == Decimal("36000.00")

    stock_ins = InventoryStockIn.objects.filter(item=feed_item)
    assert stock_ins.count() == 1
    # 1200 kg / 50 kg per sack = 24 sacks.
    assert stock_ins.get().quantity == Decimal("24.00")
    assert stock_ins.get().stock_in_date == date(2026, 1, 10)

    assert response.data["feed_delivery"]["id"] == str(delivery.id)
    assert response.data["inventory_stock_in"]["quantity"] == "24.00"


def test_without_item_creates_only_the_feed_delivery(auth, owner, staffed_farm):
    response = auth(owner).post(_url(staffed_farm), _payload(), format="json")
    assert response.status_code == 201, response.data

    assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1
    assert InventoryStockIn.objects.count() == 0
    assert response.data["inventory_stock_in"] is None


def test_item_without_kg_per_unit_is_rejected(
    auth, owner, staffed_farm, weightless_item
):
    response = auth(owner).post(
        _url(staffed_farm),
        _payload(inventory_item=weightless_item.id),
        format="json",
    )
    assert response.status_code == 400
    assert "kilograms-per-unit" in str(response.data["inventory_item"]).lower()
    assert FeedDelivery.objects.count() == 0


def test_item_from_another_farm_is_rejected(
    auth, owner, staffed_farm, rival_farm, rival_owner
):
    other = InventoryItem.objects.create(
        farm=rival_farm,
        name="Their feed",
        unit="sack",
        kg_per_unit=Decimal("50"),
        created_by=rival_owner,
    )
    response = auth(owner).post(
        _url(staffed_farm), _payload(inventory_item=other.id), format="json"
    )
    assert response.status_code == 400
    assert "no such feed item" in str(response.data["inventory_item"]).lower()
    assert FeedDelivery.objects.count() == 0


def test_stock_in_failure_rolls_back_the_feed_delivery(
    auth, owner, staffed_farm, feed_item
):
    """The whole point of one endpoint: a half-written delivery cannot survive."""
    with mock.patch.object(
        InventoryStockIn.objects,
        "create",
        side_effect=RuntimeError("forced failure after the delivery is written"),
    ):
        with pytest.raises(RuntimeError):
            auth(owner).post(
                _url(staffed_farm),
                _payload(inventory_item=feed_item.id),
                format="json",
            )

    assert FeedDelivery.objects.count() == 0
    assert InventoryStockIn.objects.count() == 0


def test_worker_is_forbidden(auth, worker, staffed_farm, feed_item):
    response = auth(worker).post(
        _url(staffed_farm), _payload(inventory_item=feed_item.id), format="json"
    )
    assert response.status_code == 403
    assert FeedDelivery.objects.count() == 0
