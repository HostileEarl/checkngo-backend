# analytics/models.py
from django.db import models
from django.utils import timezone


class AlertLog(models.Model):
    """
    One stretch of time an alert stood on a farm — the bell's history.

    Alerts are still computed live (alerts.py); this table only remembers
    when each condition was first seen and when it cleared, so a manager
    can look back at last week's high-mortality warning after the batch
    has recovered. A condition that clears and later comes back opens a
    new row: the partial unique constraint allows one OPEN log per
    (farm, alert_key) and any number of resolved ones.

    Rows are written only by alert_history.sync_alert_logs(), from the
    farm-wide alert set. `audience` is kept so the history endpoint can
    apply the same role filter the bell does.
    """

    class Severity(models.TextChoices):
        DANGER = "danger", "Danger"
        WARNING = "warning", "Warning"
        INFO = "info", "Info"

    farm = models.ForeignKey(
        "farms.Farm", on_delete=models.CASCADE, related_name="alert_logs"
    )
    alert_key = models.CharField(
        max_length=100, help_text="The alert's `id`, e.g. low-stock:12."
    )
    severity = models.CharField(max_length=10, choices=Severity.choices)
    title = models.CharField(max_length=255)
    detail = models.TextField(blank=True)
    link = models.CharField(max_length=255, blank=True)
    audience = models.JSONField(
        default=list, help_text="Roles the alert is shown to: OWNER, MANAGER, WORKER."
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "analytics_alert_log"
        ordering = ["-first_seen_at", "-id"]
        constraints = [
            # One open log per condition. Partial unique index — resolved
            # logs are excluded, so a condition can recur.
            models.UniqueConstraint(
                fields=["farm", "alert_key"],
                condition=models.Q(resolved_at__isnull=True),
                name="one_open_alert_log_per_key",
            ),
        ]
        indexes = [
            models.Index(fields=["farm", "-first_seen_at"], name="alert_log_farm_seen_idx"),
        ]

    def __str__(self):
        state = "resolved" if self.resolved_at else "open"
        return f"{self.alert_key} ({state})"
