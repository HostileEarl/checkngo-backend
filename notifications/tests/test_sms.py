# notifications/tests/test_sms.py
from unittest.mock import Mock, patch

import pytest
import requests

from notifications.models import SmsLog
from notifications.sms import (
    SMS_SEGMENT_LIMIT,
    is_gsm7_basic,
    normalize_to_local,
    sanitize_for_sms,
    send_sms,
)

pytestmark = pytest.mark.django_db


class TestNormalizeToLocal:
    def test_converts_e164_to_local(self):
        assert normalize_to_local("+639171234567") == "09171234567"

    def test_leaves_non_e164_untouched(self):
        assert normalize_to_local("09171234567") == "09171234567"


class TestSendSmsNoCredentials:
    def test_skips_with_no_api_key_and_makes_no_network_call(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = ""

        with patch("notifications.sms.requests.post") as mock_post:
            result = send_sms(
                "+639171234567",
                "hello",
                purpose=SmsLog.Purpose.INVITATION,
                log_message="hello",
            )

        mock_post.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED
        log = SmsLog.objects.get()
        assert log.status == SmsLog.Status.SKIPPED
        assert log.recipient == "09171234567"

    def test_skips_when_sms_disabled_even_with_key(self, settings):
        settings.SMS_ENABLED = False
        settings.SEMAPHORE_API_KEY = "some-key"

        with patch("notifications.sms.requests.post") as mock_post:
            result = send_sms(
                "+639171234567",
                "hello",
                purpose=SmsLog.Purpose.INVITATION,
                log_message="hello",
            )

        mock_post.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED


class TestSendSmsSuccess:
    def test_successful_send_creates_sent_log(self, fake_semaphore, settings):
        settings.SEMAPHORE_SENDER_NAME = "CheckNGo"

        result = send_sms(
            "+639171234567",
            "hello",
            purpose=SmsLog.Purpose.INVITATION,
            log_message="hello",
        )

        assert result.status == SmsLog.Status.SENT
        assert result.provider_message_id == "1000001"
        log = SmsLog.objects.get()
        assert log.status == SmsLog.Status.SENT
        assert log.provider_message_id == "1000001"

        assert fake_semaphore["data"]["apikey"] == "fake-test-key"
        assert fake_semaphore["data"]["number"] == "09171234567"
        assert fake_semaphore["data"]["sendername"] == "CheckNGo"
        assert fake_semaphore["timeout"] == 10


class TestSendSmsFailure:
    def test_provider_error_creates_failed_log_and_does_not_raise(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"

        with patch(
            "notifications.sms.requests.post",
            side_effect=requests.ConnectionError("boom"),
        ):
            result = send_sms(
                "+639171234567",
                "hello",
                purpose=SmsLog.Purpose.INVITATION,
                log_message="hello",
            )

        assert result.status == SmsLog.Status.FAILED
        log = SmsLog.objects.get()
        assert log.status == SmsLog.Status.FAILED
        assert "boom" in log.error

    def test_http_error_response_captures_status_code_and_body(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"

        error_response = Mock()
        error_response.status_code = 500
        error_response.text = '{"error": "Internal Server Error", "message": "Something broke"}'
        error_response.raise_for_status = Mock(
            side_effect=requests.HTTPError(response=error_response)
        )

        with patch("notifications.sms.requests.post", return_value=error_response):
            result = send_sms(
                "+639171234567",
                "Your one-time PIN is 483920.",
                purpose=SmsLog.Purpose.INVITATION,
                log_message="Invitation PIN sent",
            )

        assert result.status == SmsLog.Status.FAILED
        log = SmsLog.objects.get()
        assert log.status == SmsLog.Status.FAILED
        assert "500" in log.error
        assert "Something broke" in log.error
        # Never the message text on this path either — log_message keeps
        # being what's stored, regardless of what the provider echoed back.
        assert "483920" not in log.error
        assert log.message == "Invitation PIN sent"


class TestSendSmsRedaction:
    def test_log_message_is_stored_instead_of_real_message(self, settings):
        settings.SMS_ENABLED = False  # skip path exercises the same storage logic
        settings.SEMAPHORE_API_KEY = ""

        send_sms(
            "+639171234567",
            "Your one-time PIN is 483920.",
            purpose=SmsLog.Purpose.INVITATION,
            log_message="Invitation PIN sent",
        )

        log = SmsLog.objects.get()
        assert log.message == "Invitation PIN sent"
        assert "483920" not in log.message

    def test_log_message_is_a_required_argument(self):
        with pytest.raises(TypeError):
            send_sms(  # noqa: missing log_message on purpose
                "+639171234567", "hi", purpose=SmsLog.Purpose.INVITATION
            )


class TestSendSmsContentBackstop:
    """
    send_sms is the last line of defence against a malformed message. These
    never touch the network — invalid content is rejected before the
    SMS_ENABLED/API-key branch is even reached, so this is testable without
    fake_semaphore.
    """

    def test_over_160_characters_is_skipped_not_sent(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"
        long_message = "CheckN Go: " + ("x" * (SMS_SEGMENT_LIMIT))

        with patch("notifications.sms.requests.post") as mock_post:
            result = send_sms(
                "+639171234567",
                long_message,
                purpose=SmsLog.Purpose.INVITATION,
                log_message=long_message,
            )

        mock_post.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED
        assert result.error == "invalid_content"
        assert SmsLog.objects.get().error == "invalid_content"

    def test_non_gsm7_character_is_skipped_not_sent(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"
        message = "CheckN Go: your PIN is here \U0001f600"  # emoji

        with patch("notifications.sms.requests.post") as mock_post:
            result = send_sms(
                "+639171234567",
                message,
                purpose=SmsLog.Purpose.INVITATION,
                log_message=message,
            )

        mock_post.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED
        assert result.error == "invalid_content"

    def test_message_starting_with_test_is_skipped_not_sent(self, settings):
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "test-key"

        with patch("notifications.sms.requests.post") as mock_post:
            result = send_sms(
                "+639171234567",
                "  Test this is not a real CheckN Go message",
                purpose=SmsLog.Purpose.INVITATION,
                log_message="test prefix",
            )

        mock_post.assert_not_called()
        assert result.status == SmsLog.Status.SKIPPED
        assert result.error == "blocked_test_prefix"

    def test_valid_message_is_unaffected_by_the_backstop(self, fake_semaphore):
        result = send_sms(
            "+639171234567",
            "CheckN Go: You have been invited to Santos Farm. Your one-time "
            "PIN is 483920. Change it when you first sign in.",
            purpose=SmsLog.Purpose.INVITATION,
            log_message="Invitation PIN sent",
        )
        assert result.status == SmsLog.Status.SENT
        assert result.error == ""


class TestIsGsm7Basic:
    def test_plain_ascii_is_basic(self):
        assert is_gsm7_basic("CheckN Go: Hello, World! (123) #1 @home 50%")

    def test_n_tilde_is_basic(self):
        assert is_gsm7_basic("Pena and Munoz")

    def test_emoji_is_not_basic(self):
        assert not is_gsm7_basic("Hello \U0001f600")

    def test_curly_apostrophe_is_not_basic(self):
        assert not is_gsm7_basic("Farmer’s Co-op")


class TestSanitizeForSms:
    def test_curly_apostrophe_becomes_straight(self):
        assert sanitize_for_sms("Farmer’s Co-op") == "Farmer's Co-op"

    def test_curly_double_quotes_become_straight(self):
        assert sanitize_for_sms("“Best” Farm") == '"Best" Farm'

    def test_n_tilde_is_preserved_not_decomposed(self):
        assert sanitize_for_sms("Peña Farm") == "Peña Farm"
        assert sanitize_for_sms("Muñoz") == "Muñoz"
        assert sanitize_for_sms("Ñuñez") == "Ñuñez"

    def test_en_and_em_dash_become_hyphen(self):
        assert sanitize_for_sms("2020–2021") == "2020-2021"
        assert sanitize_for_sms("Farm — East") == "Farm - East"

    def test_ellipsis_becomes_three_dots(self):
        assert sanitize_for_sms("Wait…") == "Wait..."

    def test_peso_sign_becomes_php(self):
        assert sanitize_for_sms("₱500") == "PHP500"

    def test_non_breaking_space_becomes_space(self):
        assert sanitize_for_sms("Santos Farm") == "Santos Farm"

    def test_accented_letter_decomposes_to_base(self):
        assert sanitize_for_sms("José") == "Jose"

    def test_unrepresentable_character_is_dropped(self):
        assert sanitize_for_sms("Hello \U0001f600 World") == "Hello  World"

    def test_result_is_always_gsm7_basic(self):
        wild = "Farmer’s “Best” — Peña… \U0001f600 José ₱"
        assert is_gsm7_basic(sanitize_for_sms(wild))
