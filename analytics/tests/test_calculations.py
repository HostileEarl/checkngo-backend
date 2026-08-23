# analytics/tests/test_calculations.py
"""
Arithmetic verification for the analytics engine.

Every fixture below uses round numbers chosen so the expected result can be
computed by hand and stated in the assertion. A permissions bug announces
itself with a 403; a division bug returns a plausible number and gets
presented to a panel. These tests exist for the second case.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analytics.services import (
    farm_feed_cost_per_kg,
    fcr_by_batch,
    mortality_timeseries,
    profitability_by_batch,
)
from production.models import Batch, DailyRecord, FeedDelivery, Harvest, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Test House", capacity=10000)


@pytest.fixture
def clean_batch(house, owner):
    """
    1000 birds placed, 100 lost, 3000kg feed, harvested at 1500kg.

    Hand-computed expectations:
        mortality rate = 100/1000        = 10.00%
        survival       = 900/1000        = 90.00%
        FCR            = 3000/1500       = 2.000
    """
    batch = Batch.objects.create(
        house=house,
        batch_code="MATH-001",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    for day in range(1, 11):
        DailyRecord.objects.create(
            batch=batch,
            record_date=date(2026, 1, 1) + timedelta(days=day),
            mortality_disease=5,
            mortality_heat=3,
            mortality_culled=2,
            mortality_unknown=0,
            feed_kg=Decimal("300.00"),
            recorded_by=owner,
        )
    batch.refresh_from_db()
    return batch


class TestBatchTotals:
    def test_mortality_sums_all_four_causes(self, clean_batch):
        # 10 days x (5+3+2+0) = 100
        assert clean_batch.total_mortality == 100

    def test_feed_total(self, clean_batch):
        assert clean_batch.total_feed_kg == Decimal("3000.00")

    def test_current_bird_count(self, clean_batch):
        assert clean_batch.current_bird_count == 900

    def test_mortality_rate(self, clean_batch):
        assert clean_batch.mortality_rate.quantize(Decimal("0.01")) == Decimal("10.00")

    def test_recalculate_matches_signal(self, clean_batch):
        """The reconciliation path must agree with what the signal maintained."""
        before = (clean_batch.total_mortality, clean_batch.total_feed_kg)
        clean_batch.recalculate_totals()
        clean_batch.refresh_from_db()
        assert (clean_batch.total_mortality, clean_batch.total_feed_kg) == before

    def test_totals_follow_an_edit(self, clean_batch, owner):
        """Editing a record must not double-count — the signal recomputes, not increments."""
        record = clean_batch.daily_records.first()
        record.mortality_disease = 15  # was 5, so +10
        record.save()

        clean_batch.refresh_from_db()
        assert clean_batch.total_mortality == 110

    def test_totals_follow_a_delete(self, clean_batch):
        clean_batch.daily_records.first().delete()
        clean_batch.refresh_from_db()
        assert clean_batch.total_mortality == 90
        assert clean_batch.total_feed_kg == Decimal("2700.00")


class TestMortalityTimeseries:
    def test_point_count_matches_records(self, clean_batch):
        data = mortality_timeseries(clean_batch)
        assert len(data["points"]) == 10

    def test_cumulative_accumulates(self, clean_batch):
        points = mortality_timeseries(clean_batch)["points"]
        assert points[0]["cumulative_total"] == 10
        assert points[4]["cumulative_total"] == 50
        assert points[-1]["cumulative_total"] == 100

    def test_birds_alive_decreases(self, clean_batch):
        points = mortality_timeseries(clean_batch)["points"]
        assert points[0]["birds_alive"] == 990
        assert points[-1]["birds_alive"] == 900

    def test_cause_breakdown_sums_to_total(self, clean_batch):
        summary = mortality_timeseries(clean_batch)["summary"]
        by_cause = summary["by_cause"]
        assert sum(by_cause.values()) == summary["total_mortality"] == 100

    def test_gaps_are_not_interpolated(self, house, owner):
        """A missing day must be absent, not filled with zero."""
        batch = Batch.objects.create(
            house=house, batch_code="GAP-001", initial_bird_count=500,
            start_date=date(2026, 2, 1), created_by=owner,
        )
        for day in (1, 2, 5):  # days 3 and 4 never recorded
            DailyRecord.objects.create(
                batch=batch,
                record_date=date(2026, 2, 1) + timedelta(days=day),
                mortality_disease=1,
                feed_kg=Decimal("10"),
                recorded_by=owner,
            )
        points = mortality_timeseries(batch)["points"]
        assert len(points) == 3
        assert [p["age_days"] for p in points] == [1, 2, 5]


class TestFCR:
    def test_fcr_is_feed_divided_by_weight(self, clean_batch, farm, owner):
        Harvest.objects.create(
            batch=clean_batch,
            harvest_date=date(2026, 2, 15),
            birds_harvested=900,
            total_weight_kg=Decimal("1500.00"),
            recorded_by=owner,
        )
        clean_batch.status = Batch.Status.HARVESTED
        clean_batch.save(update_fields=["status"])

        result = fcr_by_batch(farm)
        row = result["rows"][0]
        # 3000 / 1500 = 2.000
        assert row["fcr"] == "2.000"
        assert row["survival_rate_pct"] == "90.00"

    def test_active_batch_excluded(self, clean_batch, farm):
        """No harvest weight means no denominator. Must be excluded, not zero."""
        result = fcr_by_batch(farm)
        assert result["rows"] == []
        assert result["excluded"]["active_batches"] == 1

    def test_terminated_batch_excluded(self, clean_batch, farm, owner):
        clean_batch.terminate("Disease outbreak", user=owner)
        result = fcr_by_batch(farm)
        assert result["rows"] == []
        assert result["excluded"]["terminated_batches"] == 1

    def test_average_across_batches(self, house, farm, owner):
        """Two batches at FCR 2.0 and 1.0 must average to 1.5, not something else."""
        specs = [
            ("AVG-A", Decimal("2000"), Decimal("1000")),  # FCR 2.000
            ("AVG-B", Decimal("1000"), Decimal("1000")),  # FCR 1.000
        ]
        for i, (code, feed, weight) in enumerate(specs):
            h = House.objects.create(farm=farm, name=f"H-{code}", capacity=5000)
            batch = Batch.objects.create(
                house=h, batch_code=code, initial_bird_count=500,
                start_date=date(2026, 1, 1), created_by=owner,
            )
            DailyRecord.objects.create(
                batch=batch, record_date=date(2026, 1, 2),
                feed_kg=feed, recorded_by=owner,
            )
            Harvest.objects.create(
                batch=batch, harvest_date=date(2026, 2, 12),
                birds_harvested=500, total_weight_kg=weight, recorded_by=owner,
            )
            batch.status = Batch.Status.HARVESTED
            batch.save(update_fields=["status"])

        result = fcr_by_batch(farm)
        assert result["summary"]["average_fcr"] == "1.500"
        assert result["summary"]["best_fcr"] == "1.000"
        assert result["summary"]["worst_fcr"] == "2.000"

    def test_empty_farm_does_not_crash(self, farm):
        result = fcr_by_batch(farm)
        assert result["rows"] == []
        assert result["summary"]["batches_analysed"] == 0


class TestProfitability:
    def test_average_feed_cost(self, farm, owner):
        """2000kg at ₱30 and 1000kg at ₱60 → ₱120000/3000 = ₱40.00/kg."""
        FeedDelivery.objects.create(
            farm=farm, delivery_date=date(2026, 1, 1),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("2000"), unit_cost=Decimal("30.00"),
            recorded_by=owner,
        )
        FeedDelivery.objects.create(
            farm=farm, delivery_date=date(2026, 1, 15),
            feed_type=FeedDelivery.FeedType.GROWER,
            quantity_kg=Decimal("1000"), unit_cost=Decimal("60.00"),
            recorded_by=owner,
        )
        assert farm_feed_cost_per_kg(farm) == Decimal("40.00")

    def test_feed_margin(self, clean_batch, farm, owner):
        """
        3000kg consumed at ₱40/kg = ₱120,000 allocated cost.
        Revenue ₱200,000 → margin ₱80,000.
        """
        FeedDelivery.objects.create(
            farm=farm, delivery_date=date(2026, 1, 1),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("3000"), unit_cost=Decimal("40.00"),
            recorded_by=owner,
        )
        Harvest.objects.create(
            batch=clean_batch, harvest_date=date(2026, 2, 15),
            birds_harvested=900, total_weight_kg=Decimal("1500.00"),
            revenue=Decimal("200000.00"), recorded_by=owner,
        )
        clean_batch.status = Batch.Status.HARVESTED
        clean_batch.save(update_fields=["status"])

        result = profitability_by_batch(farm)
        row = result["rows"][0]
        assert row["allocated_feed_cost"] == "120000.00"
        assert row["feed_margin"] == "80000.00"

    def test_methodology_is_declared(self, farm):
        """The response must state that this is feed margin, not net profit."""
        result = profitability_by_batch(farm)
        assert "methodology" in result
        assert "labour" in result["methodology"]["excluded_costs"]

    def test_missing_revenue_yields_none_not_zero(self, clean_batch, farm, owner):
        """An unrecorded price is unknown, not free."""
        FeedDelivery.objects.create(
            farm=farm, delivery_date=date(2026, 1, 1),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("3000"), unit_cost=Decimal("40.00"),
            recorded_by=owner,
        )
        Harvest.objects.create(
            batch=clean_batch, harvest_date=date(2026, 2, 15),
            birds_harvested=900, total_weight_kg=Decimal("1500.00"),
            revenue=None, recorded_by=owner,
        )
        clean_batch.status = Batch.Status.HARVESTED
        clean_batch.save(update_fields=["status"])

        row = profitability_by_batch(farm)["rows"][0]
        assert row["revenue"] is None
        assert row["feed_margin"] is None