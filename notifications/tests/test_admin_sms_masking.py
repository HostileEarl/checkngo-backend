# notifications/tests/test_admin_sms_masking.py
"""
SmsLog's admin changelist shows date, purpose, status and farm — the
recipient is masked, exact search still works, and the page stays fully
read-only.
"""
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts.models import User, mask_phone
from checkngo.urls import ADMIN_URL
from notifications.models import SmsLog

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-9-Battery"


@pytest.fixture(autouse=True)
def _static_storage_for_admin_pages(settings):
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(
        phone_number="+639170007000",
        password=PASSWORD,
        full_name="Platform Admin",
    )


@pytest.fixture
def admin_client(superuser):
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": superuser.phone_number, "password": PASSWORD},
    )
    assert resp.status_code == 302, resp.content
    return client


@pytest.fixture
def sms_log(db, staffed_farm):
    return SmsLog.objects.create(
        recipient="+639171112222",
        message="redacted",
        purpose=SmsLog.Purpose.INVITATION,
        status=SmsLog.Status.SENT,
        related_farm=staffed_farm,
    )


class TestSmsLogAdminMasking:
    def test_changelist_masks_the_recipient(self, admin_client, sms_log):
        resp = admin_client.get(f"/{ADMIN_URL}notifications/smslog/")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert sms_log.recipient not in body
        assert mask_phone(sms_log.recipient) in body

    def test_exact_recipient_search_still_finds_the_row(self, admin_client, sms_log):
        resp = admin_client.get(
            f"/{ADMIN_URL}notifications/smslog/", {"q": sms_log.recipient}
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert str(sms_log.purpose) in body or "Invitation" in body

    def test_changelist_shows_farm_column(self, admin_client, sms_log, staffed_farm):
        resp = admin_client.get(f"/{ADMIN_URL}notifications/smslog/")
        assert staffed_farm.name in resp.content.decode()

    def test_is_fully_read_only(self, admin_client, sms_log):
        assert admin_client.get(f"/{ADMIN_URL}notifications/smslog/add/").status_code == 403
        resp = admin_client.get(
            f"/{ADMIN_URL}notifications/smslog/{sms_log.pk}/delete/"
        )
        assert resp.status_code == 403
