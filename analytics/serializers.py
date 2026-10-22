# analytics/serializers.py
from rest_framework import serializers

from .models import AlertLog


class AlertLogSerializer(serializers.ModelSerializer):
    """One row of the alert history. The audience and farm stay server-side."""

    class Meta:
        model = AlertLog
        fields = [
            "id",
            "alert_key",
            "severity",
            "title",
            "detail",
            "link",
            "first_seen_at",
            "resolved_at",
        ]
        read_only_fields = fields
