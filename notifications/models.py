# notifications/models.py
from django.db import models


class SmsLog(models.Model):
    """
    A record of every SMS attempt — sent, failed, or skipped.

    Without this you cannot answer "did the worker get their PIN?", and an
    SMS that silently failed is worse than one never attempted. It also
    gives a count of messages sent, which matters when credits are prepaid.

    The `message` field never holds a PIN — callers that send a secret pass
    a redacted string for logging instead of the real message text.
    """

    class Purpose(models.TextChoices):
        INVITATION = "INVITATION", "Invitation PIN"
        LOW_STOCK = "LOW_STOCK", "Low stock alert"

    class Status(models.TextChoices):
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    recipient = models.CharField(max_length=16)
    message = models.TextField()
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    provider_message_id = models.CharField(max_length=64, blank=True)
    # Reused for a machine-readable reason on a SKIPPED row (e.g.
    # "invalid_content", "blocked_test_prefix") as well as a provider
    # error message on a FAILED one — both describe why the row isn't a
    # plain SENT.
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    related_farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sms_logs",
    )
    context_key = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Dedupe key: for a low-stock alert, the sorted comma-joined "
            "ids of the items it named. Blank for an invitation, which "
            "has nothing to dedupe against."
        ),
    )

    class Meta:
        db_table = "notifications_sms_log"
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["purpose", "status"], name="sms_purpose_status_idx"),
            models.Index(
                fields=["related_farm", "purpose", "context_key", "sent_at"],
                name="sms_dedupe_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_purpose_display()} to {self.recipient} ({self.status})"
