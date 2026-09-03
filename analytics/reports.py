# analytics/reports.py
"""
Row generators for the CSV report exports.

Same split as services.py: the views own HTTP (auth, query parsing, the
streaming response, the filename), and this module owns *what the rows
are*. Every function here is a generator that yields lists — the header
row first, then data, then any disclosure rows — and never materialises
the whole file in memory, because a farm with several finished cycles has
thousands of daily records and building the file before sending it is what
falls over on a small server.

Money and weight columns are written as plain decimal strings. They are
Decimals in the database; routing them through float on the way out would
undo the reason they are stored as Decimals.
"""
import uuid
from datetime import timedelta

from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from production.models import (
    Batch,
    DailyRecord,
    House,
    InventoryUsageLog,
    RecordCorrection,
    TaskCompletion,
    TaskTemplate,
)

from .services import _pct, fcr_by_batch, profitability_by_batch


def _s(value):
    """Decimal / None → plain string. None becomes an empty cell, never '0'."""
    return "" if value is None else str(value)


def _name(user):
    """recorded_by is SET_NULL — a deleted account leaves the cell blank."""
    return user.full_name if user else ""


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────
# 1. Daily records
# ─────────────────────────────────────────────────────────────


def daily_records_rows(farm, date_from=None, date_to=None, batch=None):
    """
    One row per filed daily record: the four mortality causes, their total,
    feed, who recorded it, and whether a manager later corrected that day.
    """
    yield [
        "record_date",
        "batch_code",
        "house",
        "mortality_disease",
        "mortality_heat",
        "mortality_culled",
        "mortality_unknown",
        "mortality_total",
        "feed_kg",
        "recorded_by",
        "was_corrected",
    ]

    records = (
        DailyRecord.objects.filter(batch__house__farm=farm)
        .select_related("batch", "batch__house", "recorded_by")
        .order_by("record_date", "batch__batch_code")
    )
    if date_from:
        records = records.filter(record_date__gte=date_from)
    if date_to:
        records = records.filter(record_date__lte=date_to)

    batch_uuid = _as_uuid(batch)
    if batch_uuid is not None:
        records = records.filter(batch_id=batch_uuid)

    # One query for the whole page instead of a `was_corrected` property
    # lookup per row — the N+1 that turns a thousand-row export into a
    # thousand extra queries.
    corrected = set(
        RecordCorrection.objects.filter(batch__house__farm=farm).values_list(
            "batch_id", "record_date"
        )
    )

    for r in records.iterator():
        total = (
            r.mortality_disease
            + r.mortality_heat
            + r.mortality_culled
            + r.mortality_unknown
        )
        yield [
            r.record_date.isoformat(),
            r.batch.batch_code,
            r.batch.house.name,
            r.mortality_disease,
            r.mortality_heat,
            r.mortality_culled,
            r.mortality_unknown,
            total,
            _s(r.feed_kg),
            _name(r.recorded_by),
            "yes" if (r.batch_id, r.record_date) in corrected else "no",
        ]


# ─────────────────────────────────────────────────────────────
# 2. Mortality summary by batch
# ─────────────────────────────────────────────────────────────


def mortality_summary_rows(farm, date_from=None, date_to=None):
    """
    One row per batch: birds placed, birds lost in the window, the rate,
    and the split by cause. With a date filter the loss figure and the
    rate are for the window, not the batch's lifetime.
    """
    yield [
        "batch_code",
        "house",
        "status",
        "birds_placed",
        "birds_lost",
        "mortality_rate_pct",
        "lost_disease",
        "lost_heat",
        "lost_culled",
        "lost_unknown",
    ]

    batches = (
        Batch.objects.filter(house__farm=farm)
        .select_related("house")
        .order_by("start_date", "batch_code")
    )

    for batch in batches:
        records = batch.daily_records.all()
        if date_from:
            records = records.filter(record_date__gte=date_from)
        if date_to:
            records = records.filter(record_date__lte=date_to)

        agg = records.aggregate(
            disease=Coalesce(Sum("mortality_disease"), 0),
            heat=Coalesce(Sum("mortality_heat"), 0),
            culled=Coalesce(Sum("mortality_culled"), 0),
            unknown=Coalesce(Sum("mortality_unknown"), 0),
        )
        lost = agg["disease"] + agg["heat"] + agg["culled"] + agg["unknown"]

        yield [
            batch.batch_code,
            batch.house.name,
            batch.status,
            batch.initial_bird_count,
            lost,
            str(_pct(lost, batch.initial_bird_count)),
            agg["disease"],
            agg["heat"],
            agg["culled"],
            agg["unknown"],
        ]


# ─────────────────────────────────────────────────────────────
# 3. Feed conversion ratio
# ─────────────────────────────────────────────────────────────


def fcr_rows(farm):
    """
    Harvested batches only, straight from fcr_by_batch. After the data a
    blank row then a note stating how many batches were left out and why —
    the same disclosure the on-screen FCR chart carries, because the file
    outlives the tooltip.
    """
    data = fcr_by_batch(farm)

    yield [
        "batch_code",
        "house",
        "harvest_date",
        "cycle_days",
        "birds_placed",
        "birds_harvested",
        "survival_rate_pct",
        "total_feed_kg",
        "total_weight_kg",
        "average_bird_weight_kg",
        "fcr",
    ]

    for r in data["rows"]:
        yield [
            r["batch_code"],
            r["house_name"],
            r["harvest_date"],
            r["cycle_days"],
            r["birds_placed"],
            r["birds_harvested"],
            r["survival_rate_pct"],
            r["total_feed_kg"],
            r["total_weight_kg"],
            _s(r["average_bird_weight_kg"]),
            r["fcr"],
        ]

    excluded = data["excluded"]
    yield []
    yield [
        "NOTE",
        (
            f'{excluded["active_batches"]} active batch(es) and '
            f'{excluded["terminated_batches"]} terminated batch(es) were '
            f'excluded from this report. {excluded["reason"]}'
        ),
    ]


# ─────────────────────────────────────────────────────────────
# 4. Feed margin
# ─────────────────────────────────────────────────────────────


def feed_margin_rows(farm):
    """
    Revenue less apportioned feed cost, from profitability_by_batch. Never
    called profit. After the data a blank row then the methodology block:
    the feed-cost basis, the excluded costs, and the "feed margin, not net
    profit" line — the caveats the on-screen chart shows, carried into the
    file so it cannot be read out of context.
    """
    data = profitability_by_batch(farm)
    m = data["methodology"]
    s = data["summary"]

    yield [
        "batch_code",
        "house",
        "harvest_date",
        "total_weight_kg",
        "revenue",
        "revenue_per_kg",
        "allocated_feed_cost",
        "feed_margin",
        "feed_margin_per_kg",
        "buyer",
    ]

    for r in data["rows"]:
        yield [
            r["batch_code"],
            r["house_name"],
            r["harvest_date"],
            r["total_weight_kg"],
            _s(r["revenue"]),
            _s(r["revenue_per_kg"]),
            _s(r["allocated_feed_cost"]),
            _s(r["feed_margin"]),
            _s(r["feed_margin_per_kg"]),
            r["buyer"] or "",
        ]

    yield []
    yield ["METHODOLOGY", "Feed cost basis", m["feed_cost_basis"]]
    yield ["METHODOLOGY", "Excluded costs", "; ".join(m["excluded_costs"])]
    yield ["METHODOLOGY", "Basis of figure", m["note"]]

    yield []
    yield ["SUMMARY", "Batches analysed", s["batches_analysed"]]
    yield ["SUMMARY", "Total revenue", _s(s["total_revenue"])]
    yield ["SUMMARY", "Total allocated feed cost", _s(s["total_allocated_feed_cost"])]
    yield ["SUMMARY", "Total feed margin", _s(s["total_feed_margin"])]
    yield [
        "SUMMARY",
        "Average feed cost per kg",
        _s(s["average_feed_cost_per_kg"]),
    ]


# ─────────────────────────────────────────────────────────────
# 5. Inventory usage
# ─────────────────────────────────────────────────────────────


def inventory_usage_rows(farm, date_from=None, date_to=None, item=None):
    """One row per drawdown a worker logged: date, item, quantity, unit, who."""
    yield ["usage_date", "item", "quantity_used", "unit", "recorded_by"]

    logs = (
        InventoryUsageLog.objects.filter(item__farm=farm)
        .select_related("item", "recorded_by")
        .order_by("usage_date", "item__name")
    )
    if date_from:
        logs = logs.filter(usage_date__gte=date_from)
    if date_to:
        logs = logs.filter(usage_date__lte=date_to)

    item_id = _as_int(item)
    if item_id is not None:
        logs = logs.filter(item_id=item_id)

    for log in logs.iterator():
        yield [
            log.usage_date.isoformat(),
            log.item.name,
            _s(log.quantity_used),
            log.item.unit,
            _name(log.recorded_by),
        ]


# ─────────────────────────────────────────────────────────────
# 6. Routine completion
# ─────────────────────────────────────────────────────────────


def routine_completion_rows(farm, date_from=None, date_to=None, house=None):
    """
    A completed/not grid: every active routine task, for every active
    house, for every day in the window.

    TaskCompletion only records the *done* side, so the "not done" rows are
    synthesised here. The grid is built from the routine as it stands now —
    a task added later shows "no" for earlier days; a retired task drops
    out. When `from` is not given it starts at the farm's earliest recorded
    completion (or today, if there are none); `to` defaults to today.
    """
    yield ["date", "task", "house", "completed", "completed_by"]

    templates = list(
        TaskTemplate.objects.filter(farm=farm, is_active=True).order_by(
            "order", "name"
        )
    )
    houses = House.objects.filter(farm=farm, is_active=True).order_by("name")
    house_id = _as_int(house)
    if house_id is not None:
        houses = houses.filter(pk=house_id)
    houses = list(houses)

    if not templates or not houses:
        return

    start = date_from
    if start is None:
        start = (
            TaskCompletion.objects.filter(template__farm=farm)
            .order_by("completion_date")
            .values_list("completion_date", flat=True)
            .first()
        ) or timezone.localdate()
    end = date_to or timezone.localdate()
    if start > end:
        return

    completions = TaskCompletion.objects.filter(
        template__farm=farm,
        completion_date__gte=start,
        completion_date__lte=end,
    ).select_related("recorded_by")
    if house_id is not None:
        completions = completions.filter(house_id=house_id)

    done = {
        (c.template_id, c.house_id, c.completion_date): c
        for c in completions.iterator()
    }

    day = start
    step = timedelta(days=1)
    while day <= end:
        for template in templates:
            for house_obj in houses:
                c = done.get((template.id, house_obj.id, day))
                yield [
                    day.isoformat(),
                    template.name,
                    house_obj.name,
                    "yes" if c else "no",
                    _name(c.recorded_by) if c else "",
                ]
        day += step
