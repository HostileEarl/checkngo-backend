# notifications/management/commands/send_stock_alerts.py
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from farms.models import Farm
from notifications.messages import render_low_stock_message
from notifications.models import SmsLog
from notifications.sms import send_sms
from production.models import InventoryItem

DEDUPE_WINDOW = timedelta(hours=24)


class Command(BaseCommand):
    """
    Text each farm owner one low-stock summary, at most once a day per
    distinct set of items.

    This is a scheduled job, not something triggered from a request — the
    alerts endpoint (analytics/alerts.py) computes the same low-stock
    condition on every poll from the frontend, and firing an SMS on a read
    request would drain prepaid credits and spam the owner every few
    minutes. In deployment this would run as a cron job (or a Render cron
    job); during development it is run by hand:

        python manage.py send_stock_alerts
    """

    help = "Send one low-stock SMS per farm to its owner, deduped within 24 hours."

    def handle(self, *args, **options):
        cutoff = timezone.now() - DEDUPE_WINDOW
        sent_count = 0

        for farm in Farm.objects.filter(is_active=True).select_related("owner"):
            items = InventoryItem.objects.filter(farm=farm, is_active=True).with_levels()

            low_items = [
                item
                for item in items
                if not (item.stock_in_count == 0 and item.usage_count == 0)
                and item.qty_current <= item.low_stock_threshold
            ]
            if not low_items:
                continue

            message, context_key = render_low_stock_message(farm.name, low_items)

            # Dedupe on the item-id set, not the rendered text: a renamed
            # farm/item, or two different item sets that happen to render
            # the same "and N more" tail, must not affect this decision.
            # Only a prior SENT counts as "already notified" — a FAILED or
            # SKIPPED attempt should be retried on the next run.
            already_sent = SmsLog.objects.filter(
                purpose=SmsLog.Purpose.LOW_STOCK,
                related_farm=farm,
                context_key=context_key,
                status=SmsLog.Status.SENT,
                sent_at__gte=cutoff,
            ).exists()
            if already_sent:
                continue

            send_sms(
                farm.owner.phone_number,
                message,
                purpose=SmsLog.Purpose.LOW_STOCK,
                log_message=message,  # nothing secret in a low-stock summary
                related_farm=farm,
                context_key=context_key,
            )
            sent_count += 1

        self.stdout.write(self.style.SUCCESS(f"Sent {sent_count} low-stock alert(s)."))
