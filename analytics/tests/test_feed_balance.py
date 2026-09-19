# analytics/tests/test_feed_balance.py
"""
The Owner/Manager dashboard's "Feed remaining" card reads
totals.feed_balance_kg from /analytics/dashboard/ (farm_dashboard()); the
Feed page it links to reads balance_kg from /feed-stock/ (FeedStockView).
Both now call analytics.services.feed_balance() — these tests prove the two
routes can never disagree, in both the ordinary case and the "Feed records
do not balance" case where consumption outpaces delivery.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from production.models import Batch, DailyRecord, FeedDelivery, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Feed Test House", capacity=5000)


@pytest.fixture
def active_batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="FEED-001",
        initial_bird_count=500,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


class TestFeedBalanceParity:
    def test_dashboard_and_feed_stock_agree(self, auth, owner, farm, active_batch):
        FeedDelivery.objects.create(
            farm=farm,
            delivery_date=date(2026, 1, 2),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("500.00"),
            unit_cost=Decimal("45.00"),
        )
        DailyRecord.objects.create(
            batch=active_batch,
            record_date=date(2026, 1, 3),
            feed_kg=Decimal("120.50"),
            recorded_by=owner,
        )

        client = auth(owner)
        dashboard = client.get(
            reverse("analytics:dashboard", kwargs={"farm_pk": farm.pk})
        ).json()
        feed_stock = client.get(
            reverse("production:feed-stock", kwargs={"farm_pk": farm.pk})
        ).json()

        assert dashboard["totals"]["feed_balance_kg"] == feed_stock["balance_kg"]
        assert feed_stock["balance_kg"] == "379.50"

    def test_agree_when_consumed_exceeds_delivered(
        self, auth, owner, farm, active_batch
    ):
        """The "Feed records do not balance" state — balance goes negative."""
        FeedDelivery.objects.create(
            farm=farm,
            delivery_date=date(2026, 1, 2),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("200.00"),
            unit_cost=Decimal("45.00"),
        )
        DailyRecord.objects.create(
            batch=active_batch,
            record_date=date(2026, 1, 3),
            feed_kg=Decimal("350.00"),
            recorded_by=owner,
        )

        client = auth(owner)
        dashboard = client.get(
            reverse("analytics:dashboard", kwargs={"farm_pk": farm.pk})
        ).json()
        feed_stock = client.get(
            reverse("production:feed-stock", kwargs={"farm_pk": farm.pk})
        ).json()

        assert dashboard["totals"]["feed_balance_kg"] == feed_stock["balance_kg"]
        assert feed_stock["balance_kg"] == "-150.00"

    def test_agree_with_no_deliveries_or_consumption(self, auth, owner, farm):
        """Both sides coalesce an empty aggregate to zero, quantized like every other case."""
        client = auth(owner)
        dashboard = client.get(
            reverse("analytics:dashboard", kwargs={"farm_pk": farm.pk})
        ).json()
        feed_stock = client.get(
            reverse("production:feed-stock", kwargs={"farm_pk": farm.pk})
        ).json()

        assert dashboard["totals"]["feed_balance_kg"] == feed_stock["balance_kg"]
        assert feed_stock["balance_kg"] == "0.00"
