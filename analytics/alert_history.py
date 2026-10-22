# analytics/alert_history.py
"""
Alert history — what the bell said, and when it stopped saying it.

compute_alerts() answers "what is true now". This module turns successive
answers into AlertLog rows: a condition seen for the first time opens a
log, a condition that is gone resolves its log. It is called on every
poll of the alerts endpoint and by the send_stock_alerts job, so history
accrues whether or not anyone has the app open.

Always pass the farm-wide list, compute_alerts(farm) with no membership.
A restricted worker's list leaves out the other houses' "today" alerts,
and syncing it would resolve them every time that worker polled.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import AlertLog

RETENTION_DAYS = 90

# Refreshed on an open log while its condition stands: a mortality rate
# climbs day by day, and a low-stock item can run out (warning -> danger)
# without changing its id. The audience is fixed when the log opens.
_CONTENT_FIELDS = ["severity", "title", "detail", "link"]


def _content(alert):
    title_len = AlertLog._meta.get_field("title").max_length
    link_len = AlertLog._meta.get_field("link").max_length
    return {
        "severity": alert["severity"],
        "title": alert["title"][:title_len],
        "detail": alert.get("detail", ""),
        "link": alert.get("link", "")[:link_len],
    }


def sync_alert_logs(farm, alerts):
    """
    Bring the farm's open AlertLog rows in line with `alerts`, then drop
    its resolved logs older than RETENTION_DAYS. One transaction, a fixed
    number of queries however many alerts there are.

    Concurrent polls of the same farm are safe. The open logs are locked
    up front in pk order, so a second sync waits rather than deadlocks;
    and when there is nothing to lock, two syncs that both see a condition
    as new race on the partial unique index, where ignore_conflicts makes
    the loser skip its insert instead of raising IntegrityError.
    """
    now = timezone.now()
    current = {alert["id"]: alert for alert in alerts}

    with transaction.atomic():
        open_logs = {
            log.alert_key: log
            for log in AlertLog.objects.select_for_update()
            .filter(farm=farm, resolved_at__isnull=True)
            .order_by("pk")
        }

        cleared = [log.pk for key, log in open_logs.items() if key not in current]
        if cleared:
            AlertLog.objects.filter(pk__in=cleared).update(resolved_at=now)

        changed = []
        for key, log in open_logs.items():
            if key not in current:
                continue
            content = _content(current[key])
            if any(getattr(log, field) != value for field, value in content.items()):
                for field, value in content.items():
                    setattr(log, field, value)
                changed.append(log)
        if changed:
            AlertLog.objects.bulk_update(changed, _CONTENT_FIELDS)

        new = [
            AlertLog(
                farm=farm,
                alert_key=key,
                audience=list(alert["audience"]),
                first_seen_at=now,
                **_content(alert),
            )
            for key, alert in sorted(current.items())
            if key not in open_logs
        ]
        if new:
            AlertLog.objects.bulk_create(new, ignore_conflicts=True)

        AlertLog.objects.filter(
            farm=farm, resolved_at__lt=now - timedelta(days=RETENTION_DAYS)
        ).delete()
