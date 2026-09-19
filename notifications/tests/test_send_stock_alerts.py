# notifications/tests/test_send_stock_alerts.py
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from notifications.models import SmsLog
from production.models import InventoryItem, InventoryStockIn, InventoryUsageLog

pytestmark = pytest.mark.django_db


def _make_low_item(farm, name, worker, threshold=Decimal("10")):
    item = InventoryItem.objects.create(
        farm=farm, name=name, unit="sack", low_stock_threshold=threshold
    )
    InventoryStockIn.objects.create(
        item=item, quantity=Decimal("10"), stock_in_date=timezone.localdate(), recorded_by=worker
    )
    InventoryUsageLog.objects.create(
        item=item, quantity_used=Decimal("5"), usage_date=timezone.localdate(), recorded_by=worker
    )
    return item


class TestSendStockAlerts:
    def test_one_message_per_farm_not_per_item(self, fake_semaphore, farm, worker):
        _make_low_item(farm, "Feed Sacks", worker)
        _make_low_item(farm, "Disinfectant", worker)
        _make_low_item(farm, "Vaccine Vials", worker)

        call_command("send_stock_alerts")

        assert fake_semaphore["data"] is not None
        log = SmsLog.objects.get(purpose=SmsLog.Purpose.LOW_STOCK)
        assert "Feed Sacks" in log.message
        assert "Disinfectant" in log.message
        assert "Vaccine Vials" in log.message
        assert log.status == SmsLog.Status.SENT

    def test_farm_with_nothing_low_sends_nothing(self, settings, farm, worker):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"

        InventoryItem.objects.create(
            farm=farm, name="Plenty", unit="sack", low_stock_threshold=Decimal("1")
        )

        call_command("send_stock_alerts")  # no fake_semaphore: nothing should be posted

        assert not SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).exists()

    def test_never_stocked_item_is_not_low(self, settings, farm, worker):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"

        InventoryItem.objects.create(
            farm=farm, name="Untouched", unit="sack", low_stock_threshold=Decimal("100")
        )

        call_command("send_stock_alerts")

        assert not SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).exists()

    def test_does_not_resend_within_24_hours_for_same_items(self, fake_semaphore, farm, worker):
        _make_low_item(farm, "Feed Sacks", worker)

        call_command("send_stock_alerts")
        call_command("send_stock_alerts")

        assert SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).count() == 1

    def test_resends_after_24_hours(self, fake_semaphore, farm, worker):
        _make_low_item(farm, "Feed Sacks", worker)

        call_command("send_stock_alerts")

        old_log = SmsLog.objects.get(purpose=SmsLog.Purpose.LOW_STOCK)
        old_log.sent_at = timezone.now() - timedelta(hours=25)
        old_log.save(update_fields=["sent_at"])

        call_command("send_stock_alerts")

        assert SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).count() == 2

    def test_renaming_the_farm_does_not_resend(self, fake_semaphore, farm, worker):
        """
        Dedupe matches on the item-id set (context_key), not the rendered
        text — a farm rename must not defeat the 24h window.
        """
        _make_low_item(farm, "Feed Sacks", worker)
        call_command("send_stock_alerts")
        assert SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).count() == 1

        farm.name = "Renamed Farm"
        farm.save(update_fields=["name"])

        call_command("send_stock_alerts")
        assert SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).count() == 1

    def test_different_item_sets_that_render_identically_do_not_suppress_each_other(
        self, fake_semaphore, farm, worker
    ):
        """
        Two distinct item sets can, in principle, render to the same
        "and N more" text. context_key (the item-id set) is what dedupe
        actually matches on, so this must not wrongly suppress the second.
        """
        first_item = _make_low_item(farm, "Feed Sacks", worker)
        call_command("send_stock_alerts")
        first_log = SmsLog.objects.get(purpose=SmsLog.Purpose.LOW_STOCK)
        assert first_log.context_key == str(first_item.id)

        first_item.is_active = False
        first_item.save(update_fields=["is_active"])
        second_item = _make_low_item(farm, "Vitamins", worker)

        call_command("send_stock_alerts")

        logs = SmsLog.objects.filter(purpose=SmsLog.Purpose.LOW_STOCK).order_by("id")
        assert logs.count() == 2
        assert logs[1].context_key == str(second_item.id)
        assert logs[0].context_key != logs[1].context_key
