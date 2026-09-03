# analytics/services.py
"""
Aggregation logic, deliberately separated from the views.

Views handle HTTP; these functions handle arithmetic. The split means the
same computation can back an API response, a management command, or a PDF
export without duplication — and it means the formulas live in one file a
panelist can be walked through.
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    SaleEvent,
    WeightSample,
)


def _batch_sale_totals(batch):
    """
    (birds, weight, revenue) a closed batch produced, summed from its sale
    events. Falls back to the Harvest's own figures for a batch closed
    before sale events existed, so historical FCR and feed margin are
    unchanged. `revenue` is None when no sale carried a value.
    """
    agg = SaleEvent.objects.filter(batch=batch).aggregate(
        birds=Sum("bird_count"),
        weight=Sum("total_weight_kg"),
        revenue=Sum("revenue"),
    )
    if agg["birds"]:
        return agg["birds"], agg["weight"], agg["revenue"]

    harvest = getattr(batch, "harvest", None)
    if harvest is None:
        return None, None, None
    return harvest.birds_harvested, harvest.total_weight_kg, harvest.revenue


def _q(value, places="0.01"):
    """Quantize safely — None in, None out."""
    if value is None:
        return None
    try:
        return Decimal(value).quantize(Decimal(places))
    except (InvalidOperation, TypeError):
        return None


def _pct(numerator, denominator):
    if not denominator:
        return Decimal("0.00")
    return _q((Decimal(numerator) / Decimal(denominator)) * 100)


# ─────────────────────────────────────────────────────────────
# 1. Mortality over time
# ─────────────────────────────────────────────────────────────


def mortality_timeseries(batch):
    """
    Daily deaths for one batch, split by cause, with a running cumulative
    rate. Gaps are real (no record filed) and are not interpolated —
    inventing data for a chart is how you lose an analytics defense.

    Days that were corrected after the fact are flagged, so the chart can
    mark them and the audit trail reaches all the way to the UI.
    """
    from production.models import RecordCorrection

    corrected_dates = set(
        RecordCorrection.objects.filter(batch=batch).values_list(
            "record_date", flat=True
        )
    )

    records = (
        DailyRecord.objects.filter(batch=batch)
        .order_by("record_date")
        .values(
            "record_date",
            "mortality_disease",
            "mortality_heat",
            "mortality_culled",
            "mortality_unknown",
            "feed_kg",
        )
    )

    points = []
    cumulative = 0
    placed = batch.initial_bird_count

    for row in records:
        daily = (
            row["mortality_disease"]
            + row["mortality_heat"]
            + row["mortality_culled"]
            + row["mortality_unknown"]
        )
        cumulative += daily
        age = (row["record_date"] - batch.start_date).days

        points.append(
            {
                "date": row["record_date"].isoformat(),
                "age_days": age,
                "disease": row["mortality_disease"],
                "heat": row["mortality_heat"],
                "culled": row["mortality_culled"],
                "unknown": row["mortality_unknown"],
                "daily_total": daily,
                "cumulative_total": cumulative,
                "cumulative_rate_pct": str(_pct(cumulative, placed)),
                "birds_alive": placed - cumulative,
                "feed_kg": str(row["feed_kg"]),
                "was_corrected": row["record_date"] in corrected_dates,
            }
        )

    by_cause = DailyRecord.objects.filter(batch=batch).aggregate(
        disease=Coalesce(Sum("mortality_disease"), 0),
        heat=Coalesce(Sum("mortality_heat"), 0),
        culled=Coalesce(Sum("mortality_culled"), 0),
        unknown=Coalesce(Sum("mortality_unknown"), 0),
    )

    return {
        "batch_id": str(batch.id),
        "batch_code": batch.batch_code,
        "house_name": batch.house.name,
        "initial_bird_count": placed,
        "start_date": batch.start_date.isoformat(),
        "status": batch.status,
        "points": points,
        "summary": {
            "total_mortality": cumulative,
            "mortality_rate_pct": str(_pct(cumulative, placed)),
            "birds_alive": placed - cumulative,
            "days_recorded": len(points),
            "days_corrected": len(corrected_dates),
            "by_cause": by_cause,
            "worst_day": max(points, key=lambda p: p["daily_total"]) if points else None,
        },
    }


def farm_mortality_comparison(farm, status=None):
    """Cross-batch mortality rates — which houses are losing birds."""
    qs = Batch.objects.filter(house__farm=farm).select_related("house")
    if status:
        qs = qs.filter(status=status)

    rows = []
    for batch in qs:
        rows.append(
            {
                "batch_id": str(batch.id),
                "batch_code": batch.batch_code,
                "house_name": batch.house.name,
                "status": batch.status,
                "start_date": batch.start_date.isoformat(),
                "initial_bird_count": batch.initial_bird_count,
                "total_mortality": batch.total_mortality,
                "mortality_rate_pct": str(
                    _pct(batch.total_mortality, batch.initial_bird_count)
                ),
            }
        )
    rows.sort(key=lambda r: Decimal(r["mortality_rate_pct"]), reverse=True)
    return rows


# ─────────────────────────────────────────────────────────────
# 2. Feed conversion ratio
# ─────────────────────────────────────────────────────────────


def fcr_by_batch(farm):
    """
    FCR = feed consumed (kg) / live weight produced (kg). Lower is better.

    HARVESTED batches only. Active batches have no weight denominator, and
    TERMINATED batches never reached a production outcome — including
    either would drag the average toward a number that means nothing.
    """
    harvested = (
        Batch.objects.filter(
            house__farm=farm, status=Batch.Status.HARVESTED, harvest__isnull=False
        )
        .select_related("house", "harvest")
        .order_by("start_date")
    )

    rows = []
    for batch in harvested:
        harvest = batch.harvest
        # Live weight produced is now the SUM of the batch's sale events,
        # carried onto the Harvest at closing time (fallback for history).
        birds_sold, weight_sold, _ = _batch_sale_totals(batch)
        if not weight_sold:
            continue

        fcr = _q(batch.total_feed_kg / weight_sold, "0.001")
        survival = _pct(birds_sold, batch.initial_bird_count)
        avg_weight = (
            _q(weight_sold / birds_sold, "0.001") if birds_sold else None
        )
        cycle_days = (harvest.harvest_date - batch.start_date).days

        rows.append(
            {
                "batch_id": str(batch.id),
                "batch_code": batch.batch_code,
                "house_name": batch.house.name,
                "harvest_date": harvest.harvest_date.isoformat(),
                "cycle_days": cycle_days,
                "birds_placed": batch.initial_bird_count,
                "birds_harvested": birds_sold,
                "survival_rate_pct": str(survival),
                "total_feed_kg": str(batch.total_feed_kg),
                "total_weight_kg": str(weight_sold),
                "average_bird_weight_kg": str(avg_weight) if avg_weight else None,
                "fcr": str(fcr),
            }
        )

    if rows:
        fcr_values = [Decimal(r["fcr"]) for r in rows]
        best = min(rows, key=lambda r: Decimal(r["fcr"]))
        worst = max(rows, key=lambda r: Decimal(r["fcr"]))
        summary = {
            "batches_analysed": len(rows),
            "average_fcr": str(_q(sum(fcr_values) / len(fcr_values), "0.001")),
            "best_fcr": best["fcr"],
            "best_batch": best["batch_code"],
            "worst_fcr": worst["fcr"],
            "worst_batch": worst["batch_code"],
        }
    else:
        summary = {
            "batches_analysed": 0,
            "note": "FCR requires at least one harvested batch.",
        }

    excluded = Batch.objects.filter(house__farm=farm).aggregate(
        active=Count("id", filter=Q(status=Batch.Status.ACTIVE)),
        terminated=Count("id", filter=Q(status=Batch.Status.TERMINATED)),
    )

    return {
        "rows": rows,
        "summary": summary,
        # Stated explicitly so the chart can show "3 batches excluded"
        # rather than silently under-reporting.
        "excluded": {
            "active_batches": excluded["active"],
            "terminated_batches": excluded["terminated"],
            "reason": "FCR requires a harvest weight.",
        },
    }


def growth_curve(batch):
    """Weight samples over time — the input side of the FCR story."""
    samples = WeightSample.objects.filter(batch=batch).order_by("sample_date")
    return [
        {
            "date": s.sample_date.isoformat(),
            "age_days": (s.sample_date - batch.start_date).days,
            "average_grams": str(s.average_grams),
            "birds_weighed": s.birds_weighed,
        }
        for s in samples
    ]


# ─────────────────────────────────────────────────────────────
# 3. Profitability
# ─────────────────────────────────────────────────────────────


def farm_feed_cost_per_kg(farm):
    """
    Average feed cost across the farm.

    Deliveries are farm-level and consumption is batch-level, so per-batch
    feed cost is an allocation, not a trace. This is the ruling from the
    schema design: average cost over the period, stated as such.
    """
    agg = FeedDelivery.objects.filter(farm=farm).aggregate(
        kg=Coalesce(Sum("quantity_kg"), Value(Decimal("0")), output_field=DecimalField()),
        cost=Coalesce(Sum("total_cost"), Value(Decimal("0")), output_field=DecimalField()),
    )
    if not agg["kg"]:
        return None
    return _q(agg["cost"] / agg["kg"])


def profitability_by_batch(farm):
    """
    Revenue against allocated feed cost, per harvested batch.

    Feed cost is APPORTIONED using the farm's average cost per kg — it is
    not a traced expense. Margin here excludes chicks, labour, utilities,
    and medication, so it is a feed margin, not a profit figure. The
    response says so explicitly rather than letting the label mislead.
    """
    avg_cost_per_kg = farm_feed_cost_per_kg(farm)

    harvested = (
        Batch.objects.filter(
            house__farm=farm, status=Batch.Status.HARVESTED, harvest__isnull=False
        )
        .select_related("house", "harvest", "harvest__buyer_link__partner")
        .order_by("harvest__harvest_date")
    )

    rows = []
    total_revenue = Decimal("0")
    total_feed_cost = Decimal("0")

    for batch in harvested:
        harvest = batch.harvest
        # Revenue and live weight are SUMMED from the batch's sale events,
        # not read from a single harvest figure (fallback for history).
        _, weight_sold, revenue = _batch_sale_totals(batch)

        feed_cost = (
            _q(batch.total_feed_kg * avg_cost_per_kg)
            if avg_cost_per_kg is not None
            else None
        )

        margin = None
        margin_per_kg = None
        if revenue is not None and feed_cost is not None:
            margin = _q(revenue - feed_cost)
            if weight_sold:
                margin_per_kg = _q(margin / weight_sold)

        if revenue is not None:
            total_revenue += revenue
        if feed_cost is not None:
            total_feed_cost += feed_cost

        buyer = harvest.buyer_link
        rows.append(
            {
                "batch_id": str(batch.id),
                "batch_code": batch.batch_code,
                "house_name": batch.house.name,
                "harvest_date": harvest.harvest_date.isoformat(),
                "total_weight_kg": str(weight_sold) if weight_sold is not None else None,
                "revenue": str(revenue) if revenue is not None else None,
                "revenue_per_kg": str(_q(revenue / weight_sold))
                if revenue is not None and weight_sold
                else None,
                "allocated_feed_cost": str(feed_cost) if feed_cost is not None else None,
                "feed_margin": str(margin) if margin is not None else None,
                "feed_margin_per_kg": str(margin_per_kg) if margin_per_kg is not None else None,
                "buyer": (buyer.business_name or buyer.partner.full_name) if buyer else None,
            }
        )

    return {
        "rows": rows,
        "summary": {
            "batches_analysed": len(rows),
            "total_revenue": str(_q(total_revenue)),
            "total_allocated_feed_cost": str(_q(total_feed_cost)),
            "total_feed_margin": str(_q(total_revenue - total_feed_cost)),
            "average_feed_cost_per_kg": str(avg_cost_per_kg) if avg_cost_per_kg else None,
        },
        "methodology": {
            "feed_cost_basis": "Farm-wide average cost per kg, apportioned by batch consumption.",
            "excluded_costs": [
                "day-old chicks",
                "labour",
                "utilities",
                "medication and vaccines",
                "housing depreciation",
            ],
            "note": "Feed margin, not net profit. Feed is typically 60-70% of broiler cost.",
        },
    }


# ─────────────────────────────────────────────────────────────
# Dashboard rollup
# ─────────────────────────────────────────────────────────────


def farm_dashboard(farm):
    """Single call for the landing screen — avoids four round trips on mobile."""
    active = Batch.objects.filter(
        house__farm=farm, status=Batch.Status.ACTIVE
    ).select_related("house")

    birds_alive = sum(b.current_bird_count for b in active)

    feed = FeedDelivery.objects.filter(farm=farm).aggregate(
        delivered=Coalesce(Sum("quantity_kg"), Value(Decimal("0")), output_field=DecimalField())
    )
    consumed = DailyRecord.objects.filter(batch__house__farm=farm).aggregate(
        used=Coalesce(Sum("feed_kg"), Value(Decimal("0")), output_field=DecimalField())
    )

    fcr = fcr_by_batch(farm)

    return {
        "farm_id": farm.pk,
        "farm_name": farm.name,
        "is_active": farm.is_active,
        "houses": {
            "total": farm.houses.filter(is_active=True).count(),
            "occupied": active.count(),
        },
        "active_batches": [
            {
                "batch_id": str(b.id),
                "batch_code": b.batch_code,
                "house_name": b.house.name,
                "age_days": b.age_days,
                "birds_alive": b.current_bird_count,
                "mortality_rate_pct": str(_pct(b.total_mortality, b.initial_bird_count)),
            }
            for b in active
        ],
        "totals": {
            "birds_alive": birds_alive,
            "feed_delivered_kg": str(feed["delivered"]),
            "feed_consumed_kg": str(consumed["used"]),
            "feed_balance_kg": str(feed["delivered"] - consumed["used"]),
        },
        "lifetime": {
            "batches_completed": fcr["summary"].get("batches_analysed", 0),
            "average_fcr": fcr["summary"].get("average_fcr"),
        },
    }