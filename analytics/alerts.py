# analytics/alerts.py
"""
Computed alerts — the notification bell without a notification table.

Almost every alert worth showing is derivable from data the system already
holds: a mortality rate, a missing daily record, an invitation about to
lapse. Persisting a notification model with signals and a job runner would
add moving parts to reproduce facts we can read on demand.

Same split as services.py: the arithmetic lives here, the view does HTTP.
Each alert is a plain dict; `id` is stable for a given condition so the
client can remember dismissals across polls.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Invitation
from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    InventoryItem,
    RecordCorrection,
    TaskCompletion,
    TaskTemplate,
)

from .services import _pct

MORTALITY_ALERT_PCT = Decimal("8")
HARVEST_HORIZON_DAYS = 5
INVITATION_HORIZON_DAYS = 2
CORRECTION_LOOKBACK_DAYS = 7
ROUTINE_ALERT_HOUR = 18  # local hour after which an unfinished routine is flagged

_OWNER_MANAGER = ["OWNER", "MANAGER"]
_EVERYONE = ["OWNER", "MANAGER", "WORKER"]


def compute_alerts(farm, membership=None):
    """
    Every alert currently true for this farm, in a fixed order. The view
    filters by the requesting user's role; this function does not — the one
    exception is `membership`, which house-scopes the "today not recorded"
    alert for a worker restricted to specific houses (owners, managers, and
    unassigned workers are unaffected).
    """
    today = timezone.localdate()
    now = timezone.now()

    active = list(
        Batch.objects.filter(
            house__farm=farm, status=Batch.Status.ACTIVE
        ).select_related("house")
    )

    alerts = []
    alerts += _high_mortality(active)
    alerts += _negative_feed_balance(farm)
    alerts += _low_stock(farm)
    alerts += _today_not_recorded(active, today, membership)
    alerts += _routine_incomplete(farm, now, today)
    alerts += _harvest_approaching(active, today)
    alerts += _invitation_expiring(farm, now)
    alerts += _record_corrected_recently(farm, now)
    return alerts


# ─────────────────────────────────────────────────────────────
# 1. High mortality
# ─────────────────────────────────────────────────────────────


def _high_mortality(active_batches):
    out = []
    for b in active_batches:
        rate = _pct(b.total_mortality, b.initial_bird_count)
        if rate is not None and rate >= MORTALITY_ALERT_PCT:
            out.append(
                {
                    "id": f"mortality:{b.id}",
                    "severity": "danger",
                    "title": f"High mortality in {b.batch_code}",
                    "detail": (
                        f"{rate}% of the flock has been lost. "
                        "Typical is under 5%."
                    ),
                    "link": "/analytics",
                    "audience": list(_EVERYONE),
                }
            )
    return out


# ─────────────────────────────────────────────────────────────
# 2. Negative feed balance
# ─────────────────────────────────────────────────────────────


def _negative_feed_balance(farm):
    delivered = FeedDelivery.objects.filter(farm=farm).aggregate(
        total=Coalesce(
            Sum("quantity_kg"), Value(Decimal("0")), output_field=DecimalField()
        )
    )["total"]
    consumed = DailyRecord.objects.filter(batch__house__farm=farm).aggregate(
        total=Coalesce(
            Sum("feed_kg"), Value(Decimal("0")), output_field=DecimalField()
        )
    )["total"]

    balance = delivered - consumed
    if balance >= 0:
        return []

    short = (consumed - delivered).quantize(Decimal("0.01"))
    return [
        {
            "id": "feed-balance",
            "severity": "danger",
            "title": "Feed records do not balance",
            "detail": (
                f"Consumption exceeds recorded deliveries by {short} kg. "
                "A delivery may not have been recorded."
            ),
            "link": "/records",
            "audience": list(_OWNER_MANAGER),
        }
    ]


# ─────────────────────────────────────────────────────────────
# 2b. Low inventory stock
# ─────────────────────────────────────────────────────────────


def _fmt_qty(value):
    """Trim a quantity to its shortest exact form: 5.00 -> 5, 5.50 -> 5.5."""
    return f"{Decimal(str(value)).normalize():f}"


def _low_stock(farm):
    """
    An inventory item at or below its reorder level.

    Three states, told apart deliberately:

      never stocked  — no stock-ins AND no usage. A brand-new item nobody
                       has touched is not "low"; it has simply never been
                       stocked. Suppressed, or a warning fires the instant
                       a manager adds an item.
      out of stock   — balance <= 0. Surfaced as `danger`: a worker
                       recording usage against an empty item is a data
                       problem, not a "reorder soon" nudge.
      running low    — 0 < balance <= threshold. `warning`.

    Restocking is a purchasing decision, so this goes to owners and
    managers only — the same audience as the feed-balance alert.
    """
    items = InventoryItem.objects.filter(farm=farm, is_active=True).with_levels()

    out = []
    for item in items:
        if item.stock_in_count == 0 and item.usage_count == 0:
            continue  # never stocked

        qty = item.qty_current
        threshold = item.low_stock_threshold

        # Structured quantities alongside the prose `detail`, so the client
        # can offer a "copy reorder message" action straight from the alert
        # without navigating to the inventory screen or parsing the text.
        base = {
            "id": f"low-stock:{item.id}",
            "link": "/inventory",
            "audience": list(_OWNER_MANAGER),
            "item_name": item.name,
            "unit": item.unit,
            "on_hand": _fmt_qty(qty),
            "reorder_level": _fmt_qty(threshold),
        }

        if qty <= 0:
            out.append(
                {
                    **base,
                    "severity": "danger",
                    "title": f"Out of stock: {item.name}",
                    "detail": (
                        f"{item.name} is down to {_fmt_qty(qty)} {item.unit}. "
                        "Usage is still being recorded against it — record a "
                        "delivery or correct the logs."
                    ),
                }
            )
        elif qty <= threshold:
            out.append(
                {
                    **base,
                    "severity": "warning",
                    "title": f"Low stock: {item.name}",
                    "detail": (
                        f"{_fmt_qty(qty)} {item.unit} left, at or below the "
                        f"reorder level of {_fmt_qty(threshold)} {item.unit}."
                    ),
                }
            )
    return out


# ─────────────────────────────────────────────────────────────
# 3. Today not recorded
# ─────────────────────────────────────────────────────────────


def _today_not_recorded(active_batches, today, membership=None):
    if not active_batches:
        return []

    # A worker restricted to specific houses is only nagged about batches in
    # those houses — a shed-A worker should not see "no record yet" for shed
    # B every day. Owners, managers, and unassigned workers see all of them.
    if (
        membership is not None
        and membership.role == "WORKER"
        and membership.houses.exists()
    ):
        assigned = set(membership.houses.values_list("id", flat=True))
        active_batches = [b for b in active_batches if b.house_id in assigned]
        if not active_batches:
            return []

    recorded = set(
        DailyRecord.objects.filter(
            batch__in=active_batches, record_date=today
        ).values_list("batch_id", flat=True)
    )

    out = []
    for b in active_batches:
        if b.id in recorded or b.start_date >= today:
            continue
        out.append(
            {
                "id": f"today:{b.id}",
                "severity": "warning",
                "title": f"No record yet for {b.batch_code}",
                "detail": "Today's mortality and feed have not been entered.",
                "link": "/entry",
                "audience": list(_EVERYONE),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────
# 3b. Daily routine not finished
# ─────────────────────────────────────────────────────────────


def _routine_incomplete(farm, now, today):
    """
    An active routine item with no completion for today, once the day is
    late enough for that to mean something.

    Owner/manager only — workers see their own checklist and do not need
    nagging. Suppressed entirely when the farm has no routine, and when no
    daily record was filed either: a farm with no activity at all is
    already covered by "today not recorded", and two alerts for one
    situation is noise.
    """
    # `now` is UTC; the cutoff is a wall-clock hour on the farm.
    if timezone.localtime(now).hour < ROUTINE_ALERT_HOUR:
        return []

    active_ids = list(
        TaskTemplate.objects.filter(farm=farm, is_active=True).values_list(
            "id", flat=True
        )
    )
    if not active_ids:
        return []

    done_ids = set(
        TaskCompletion.objects.filter(
            template__farm=farm, completion_date=today
        ).values_list("template_id", flat=True)
    )
    open_count = sum(1 for tid in active_ids if tid not in done_ids)
    if open_count == 0:
        return []

    daily_filed = DailyRecord.objects.filter(
        batch__house__farm=farm, record_date=today
    ).exists()
    if not daily_filed:
        return []

    total = len(active_ids)
    return [
        {
            "id": "routine-incomplete",
            "severity": "warning",
            "title": "Daily routine not finished",
            "detail": (
                f"{total - open_count} of {total} routine tasks done. "
                f"{open_count} still open after {ROUTINE_ALERT_HOUR}:00."
            ),
            "link": "/tasks",
            "audience": list(_OWNER_MANAGER),
        }
    ]


# ─────────────────────────────────────────────────────────────
# 4. Harvest approaching
# ─────────────────────────────────────────────────────────────


def _harvest_approaching(active_batches, today):
    horizon = today + timedelta(days=HARVEST_HORIZON_DAYS)
    out = []
    for b in active_batches:
        d = b.expected_harvest_date
        if d is None or not (today <= d <= horizon):
            continue
        out.append(
            {
                "id": f"harvest:{b.id}",
                "severity": "info",
                "title": f"{b.batch_code} is near harvest",
                "detail": (
                    f"Expected on {d.isoformat()}, day {b.age_days} of the cycle."
                ),
                "link": "/batches",
                "audience": list(_OWNER_MANAGER),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────
# 5. Invitation expiring
# ─────────────────────────────────────────────────────────────


def _invitation_expiring(farm, now):
    cutoff = now + timedelta(days=INVITATION_HORIZON_DAYS)
    out = []
    for inv in (
        Invitation.objects.pending()
        .filter(farm=farm, expires_at__lte=cutoff)
        .order_by("expires_at")
    ):
        out.append(
            {
                "id": f"invitation:{inv.id}",
                "severity": "warning",
                "title": f"Invitation for {inv.full_name} expires soon",
                "detail": (
                    f"It expires on {inv.expires_at.date().isoformat()}. "
                    "After that you will need to issue a new one."
                ),
                "link": "/staff",
                "audience": list(_OWNER_MANAGER),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────
# 6. Record corrected recently
# ─────────────────────────────────────────────────────────────


def _record_corrected_recently(farm, now):
    since = now - timedelta(days=CORRECTION_LOOKBACK_DAYS)
    out = []
    for rc in (
        RecordCorrection.objects.filter(
            batch__house__farm=farm, corrected_at__gte=since
        )
        .select_related("batch")
        .order_by("-corrected_at")
    ):
        out.append(
            {
                "id": f"correction:{rc.id}",
                "severity": "info",
                "title": "A record was corrected",
                "detail": (
                    f"{rc.corrected_by_name} amended {rc.record_date.isoformat()} "
                    f"on {rc.batch.batch_code}."
                ),
                "link": "/records",
                "audience": list(_EVERYONE),
            }
        )
    return out
