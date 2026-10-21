# checkngo/platform_stats.py
"""
Platform-wide counts for the admin dashboard.

COUNTS ONLY. Never per-farm, never a name, a phone number, or money — the
technical administrator who views this maintains accounts and takes no
part in farm operations, so nothing here can be traced back to a specific
farm's business or a specific person's identity. If you are tempted to
add a breakdown "by farm", stop — that belongs in the farm-facing app,
not here.

Cached for 5 minutes (platform_stats() itself, not individual callers) —
cheap enough to not need a cron job, expensive enough (several COUNT
queries) that every admin-index load recomputing it would be wasteful.
"""
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

CACHE_KEY = "checkngo:platform_stats:v1"
CACHE_TTL_SECONDS = 300


def _invitation_counts():
    from accounts.models import Invitation

    now = timezone.now()
    overdue_pending = Q(status=Invitation.Status.PENDING, expires_at__lte=now)
    return {
        "pending": Invitation.objects.filter(
            status=Invitation.Status.PENDING, expires_at__gt=now
        ).count(),
        "accepted": Invitation.objects.filter(
            status=Invitation.Status.ACCEPTED
        ).count(),
        "expired": Invitation.objects.filter(
            Q(status=Invitation.Status.EXPIRED) | overdue_pending
        ).count(),
        "revoked": Invitation.objects.filter(
            status=Invitation.Status.REVOKED
        ).count(),
    }


def _user_counts():
    from accounts.models import User

    owners = User.objects.filter(role=User.Role.OWNER)
    return {
        "owners": owners.count(),
        "owners_in_setup": owners.exclude(farm_memberships__is_active=True)
        .distinct()
        .count(),
        "managers": User.objects.filter(role=User.Role.MANAGER).count(),
        "workers": User.objects.filter(role=User.Role.WORKER).count(),
        "buyers": User.objects.filter(role=User.Role.CONSUMER).count(),
    }


def _farm_counts():
    from farms.models import Farm

    return {
        "active": Farm.objects.filter(is_active=True).count(),
        "archived": Farm.objects.filter(is_active=False).count(),
    }


def _house_counts():
    from production.models import House

    return {"active": House.objects.filter(archived_at__isnull=True).count()}


def _batch_counts():
    from production.models import Batch

    return {
        "active": Batch.objects.filter(status=Batch.Status.ACTIVE).count(),
        "closed": Batch.objects.filter(status=Batch.Status.HARVESTED).count(),
        "ended_early": Batch.objects.filter(status=Batch.Status.TERMINATED).count(),
    }


def _recent_activity_counts():
    from production.models import DailyRecord, SaleEvent

    now = timezone.now()
    last_7_days = now - timezone.timedelta(days=7)
    last_30_days = now - timezone.timedelta(days=30)
    return {
        "daily_records_last_7_days": DailyRecord.objects.filter(
            created_at__gte=last_7_days
        ).count(),
        "daily_records_last_30_days": DailyRecord.objects.filter(
            created_at__gte=last_30_days
        ).count(),
        "sales_last_30_days": SaleEvent.objects.filter(
            created_at__gte=last_30_days
        ).count(),
    }


def _sms_counts():
    from django.db.models import Count

    from notifications.models import SmsLog

    last_30_days = timezone.now() - timezone.timedelta(days=30)

    # Nested dict, zero-filled for every (purpose, status) pair so a chart
    # never has to guess whether a missing key means zero.
    by_purpose = {
        purpose: {status: 0 for status, _ in SmsLog.Status.choices}
        for purpose, _ in SmsLog.Purpose.choices
    }
    counted = (
        SmsLog.objects.filter(sent_at__gte=last_30_days)
        .values("purpose", "status")
        .annotate(count=Count("id"))
    )
    for row in counted:
        by_purpose.setdefault(row["purpose"], {})[row["status"]] = row["count"]
    return by_purpose


def _compute_platform_stats():
    return {
        "generated_at": timezone.now().isoformat(),
        "farms": _farm_counts(),
        "users": _user_counts(),
        "houses": _house_counts(),
        "batches": _batch_counts(),
        "invitations": _invitation_counts(),
        **_recent_activity_counts(),
        "sms_last_30_days": _sms_counts(),
    }


def platform_stats():
    """Platform-wide counts, cached for CACHE_TTL_SECONDS."""
    stats = cache.get(CACHE_KEY)
    if stats is None:
        stats = _compute_platform_stats()
        cache.set(CACHE_KEY, stats, CACHE_TTL_SECONDS)
    return stats
