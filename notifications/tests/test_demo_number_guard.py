# notifications/tests/test_demo_number_guard.py
"""
No SMS ever goes to an evaluation-farm demo account.

Their numbers (+63999 NN 0000 R) sit on a live Smart prefix, so a text to
one could reach a stranger. Two layers, each tested on its own:

1. send_stock_alerts skips evaluation farms — which are built with an item
   under its reorder level on purpose — while still syncing their alert
   history.
2. send_sms refuses any reserved demo number outright: SKIPPED with
   "reserved_demo_number", a warning in the log, no network call. That also
   covers an invitation sent to a demo number during a session.
"""
import logging
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import Invitation, User
from analytics.models import AlertLog
from farms.models import Farm
from notifications.models import SmsLog
from notifications.sms import send_sms
from production.evaluation import create_evaluation_farm, demo_phone_number
from production.models import InventoryItem, InventoryStockIn, InventoryUsageLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def semaphore(settings):
    """SMS switched on with a fake key, and requests.post mocked so every
    call is counted. Layered over conftest's network block."""
    settings.SMS_ENABLED = True
    settings.SEMAPHORE_API_KEY = "fake-test-key"
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = [{"message_id": 4242}]
    with patch("notifications.sms.requests.post", return_value=response) as post:
        yield post


def _low_item(farm, name="Feed Sacks"):
    item = InventoryItem.objects.create(
        farm=farm, name=name, unit="sack", low_stock_threshold=Decimal("10")
    )
    InventoryStockIn.objects.create(
        item=item, quantity=Decimal("10"), stock_in_date=timezone.localdate()
    )
    InventoryUsageLog.objects.create(
        item=item, quantity_used=Decimal("5"), usage_date=timezone.localdate()
    )
    return item


def _open_alert_keys(farm):
    return set(
        AlertLog.objects.filter(farm=farm, resolved_at__isnull=True).values_list(
            "alert_key", flat=True
        )
    )


class TestStockAlertsSkipEvaluationFarms:
    def test_texts_only_the_real_farm_and_syncs_both(self, semaphore, farm, capsys):
        evaluation = create_evaluation_farm(3)
        vitamins = InventoryItem.objects.get(farm=evaluation.farm, name="Vitamins")
        real_item = _low_item(farm)
        assert InventoryItem.objects.filter(farm=evaluation.farm).low_stock().exists()

        call_command("send_stock_alerts")

        # One network call, to the real owner.
        assert semaphore.call_count == 1
        assert semaphore.call_args.kwargs["data"]["number"] == "09171112222"

        log = SmsLog.objects.get()
        assert log.related_farm == farm
        assert log.purpose == SmsLog.Purpose.LOW_STOCK
        assert log.status == SmsLog.Status.SENT
        # Skipped by the command itself, not merely refused by send_sms:
        # no attempt is logged for the evaluation farm at all.
        assert not SmsLog.objects.filter(related_farm=evaluation.farm).exists()
        assert not SmsLog.objects.filter(recipient__startswith="0999").exists()

        # Both farms' alert history is current.
        assert f"low-stock:{real_item.pk}" in _open_alert_keys(farm)
        assert f"low-stock:{vitamins.pk}" in _open_alert_keys(evaluation.farm)

        assert "Sent 1 low-stock alert(s)." in capsys.readouterr().out

    def test_a_lookalike_name_on_a_real_farm_is_still_texted(self, semaphore, owner):
        lookalike = Farm.objects.create(name="Demo Farm – Session 41", owner=owner)
        _low_item(lookalike)

        call_command("send_stock_alerts")

        assert semaphore.call_count == 1
        assert SmsLog.objects.get().related_farm == lookalike

    def test_rerunning_still_sends_nothing_for_the_evaluation_farm(self, semaphore):
        evaluation = create_evaluation_farm(4)

        call_command("send_stock_alerts")
        call_command("send_stock_alerts")

        semaphore.assert_not_called()
        assert SmsLog.objects.count() == 0
        assert _open_alert_keys(evaluation.farm)


class TestSendSmsRefusesReservedDemoNumbers:
    @pytest.mark.parametrize("role_digit", [1, 2, 3, 4])
    def test_each_demo_number_is_skipped_with_no_network_call(
        self, semaphore, caplog, role_digit
    ):
        number = demo_phone_number(3, role_digit)

        with caplog.at_level(logging.WARNING, logger="notifications.sms"):
            result = send_sms(
                number,
                "CheckN Go: Low stock at Demo Farm",
                purpose=SmsLog.Purpose.LOW_STOCK,
                log_message="CheckN Go: Low stock at Demo Farm",
            )

        semaphore.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED
        assert result.error == "reserved_demo_number"
        log = SmsLog.objects.get()
        assert log.status == SmsLog.Status.SKIPPED
        assert log.error == "reserved_demo_number"
        assert log.recipient == "0" + number[3:]
        assert any(
            r.levelno == logging.WARNING and "reserved_demo_number" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        "number",
        ["+639990100001", "+639999900004", "09990300002", "639990300003", "+63 999 03 0000 4"],
    )
    def test_every_form_and_session_is_refused(self, semaphore, number):
        result = send_sms(
            number, "CheckN Go: hello", purpose=SmsLog.Purpose.INVITATION,
            log_message="Invitation PIN sent",
        )
        semaphore.assert_not_called()
        assert result.error == "reserved_demo_number"

    @pytest.mark.parametrize(
        "number",
        [
            "+639990000001",  # session 00 does not exist
            "+639990300005",  # role digit 5 does not exist
            "+639990310001",  # not 0000 in the middle
            "+639991234567",  # an ordinary 0999 subscriber
            "+639171112222",
        ],
    )
    def test_ordinary_numbers_are_still_sent(self, semaphore, number):
        result = send_sms(
            number, "CheckN Go: hello", purpose=SmsLog.Purpose.INVITATION,
            log_message="Invitation PIN sent",
        )
        assert semaphore.call_count == 1
        assert result.status == SmsLog.Status.SENT

    def test_refused_before_the_content_checks(self, semaphore):
        result = send_sms(
            demo_phone_number(5, 1), "Ünïcode " * 40, purpose=SmsLog.Purpose.LOW_STOCK,
            log_message="x",
        )
        semaphore.assert_not_called()
        assert result.error == "reserved_demo_number"


class TestInvitingADemoNumber:
    @pytest.fixture
    def demo_owner(self):
        evaluation = create_evaluation_farm(3)
        owner = User.objects.get(phone_number=demo_phone_number(3, 1))
        return evaluation.farm, owner

    def test_staff_invitation_is_saved_but_not_texted(self, semaphore, auth, demo_owner):
        farm, owner = demo_owner
        invitee = demo_phone_number(7, 3)  # nobody holds it yet

        response = auth(owner).post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": farm.pk}),
            {
                "phone_number": invitee,
                "full_name": "Session Tester",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )

        assert response.status_code == 201, response.data
        assert response.data["sms_sent"] is False
        assert len(response.data["pin"]) == 6  # still shown to the inviter
        invitation = Invitation.objects.get(pk=response.data["id"])
        assert invitation.phone_number == invitee
        assert invitation.farm == farm

        semaphore.assert_not_called()
        log = SmsLog.objects.get()
        assert log.purpose == SmsLog.Purpose.INVITATION
        assert log.status == SmsLog.Status.SKIPPED
        assert log.error == "reserved_demo_number"

    def test_buyer_invitation_is_saved_but_not_texted(self, semaphore, auth, demo_owner):
        farm, owner = demo_owner
        invitee = demo_phone_number(7, 4)

        response = auth(owner).post(
            reverse("partners:farm-partners", kwargs={"farm_pk": farm.pk}),
            {
                "phone_number": invitee,
                "full_name": "Session Buyer",
                "business_name": "Tester Trading",
                "account_role": "CONSUMER",
            },
            format="json",
        )

        assert response.status_code == 201, response.data
        assert response.data["sms_sent"] is False
        assert Invitation.objects.filter(phone_number=invitee, farm=farm).exists()
        semaphore.assert_not_called()
        assert SmsLog.objects.get().error == "reserved_demo_number"
