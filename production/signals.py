# production/signals.py
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Batch, DailyRecord


@receiver(post_save, sender=DailyRecord, dispatch_uid="batch_totals_on_save")
@receiver(post_delete, sender=DailyRecord, dispatch_uid="batch_totals_on_delete")
def refresh_batch_totals(sender, instance, raw=False, **kwargs):
    """
    Keep Batch.total_mortality and total_feed_kg in step with the records.

    A full recalculation rather than an increment: increments drift when a
    record is edited within the 24-hour window, and the aggregate is cheap
    at 45 rows per batch.
    """
    if raw:
        return
    try:
        instance.batch.recalculate_totals()
    except Batch.DoesNotExist:
        # Batch was cascade-deleted; nothing to update.
        pass