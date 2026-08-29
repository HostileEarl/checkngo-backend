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
from production.models import Batch, DailyRecord, FeedDelivery, RecordCorrection

from .services import _pct

MORTALITY_ALERT_PCT = Decimal("8")
HARVEST_HORIZON_DAYS = 5
INVITATION_HORIZON_DAYS = 2
CORRECTION_LOOKBACK_DAYS = 7

_OWNER_MANAGER = ["OWNER", "MANAGER"]
_EVERYONE = ["OWNER", "MANAGER", "WORKER"]


def compute_alerts(farm):
    """
    Every alert currently true for this farm, in a fixed order. The view
    filters by the requesting user's role; this function does not.
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
    alerts += _today_not_recorded(active, today)
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
# 3. Today not recorded
# ─────────────────────────────────────────────────────────────


def _today_not_recorded(active_batches, today):
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
