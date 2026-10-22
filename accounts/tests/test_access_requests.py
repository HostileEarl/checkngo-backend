# accounts/tests/test_access_requests.py
"""
The landing page's public "Request access" form, POST /api/access-requests/.

A request is a contact record, not a sign-up: it never creates a User or a
Farm. It texts the administrator (farm name and location only), at most
ACCESS_REQUEST_SMS_DAILY_CAP times per rolling 24 hours, and the admin list
masks the requester's phone number.
"""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import AccessRequest, User, mask_phone
from checkngo.urls import ADMIN_URL
from farms.models import Farm
from notifications.models import SmsLog

pytestmark = pytest.mark.django_db

URL = "/api/access-requests/"
NOTIFY_PHONE = "+639170000001"
SUCCESS = {"detail": "Thanks! We'll contact you soon."}
PASSWORD = "Correct-Horse-9-Battery"


def payload(**overrides):
    data = {
        "full_name": "Liza Manalo",
        "phone_number": "+639181234567",
        "email": "liza@example.com",
        "farm_name": "Manalo Broiler Farm",
        "location": "Capas, Tarlac",
        "houses": 3,
        "role": "OWNER",
        "message": "We run three tunnel-vent houses.",
        "consent": True,
        "website": "",
    }
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def _fresh_throttle_cache():
    # Throttle counters live in LocMemCache, which outlives a single test.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def notify_phone(settings):
    settings.ACCESS_REQUEST_NOTIFY_PHONE = NOTIFY_PHONE
    return NOTIFY_PHONE


def alert_logs():
    return SmsLog.objects.filter(purpose=SmsLog.Purpose.ACCESS_REQUEST)


def post(api, data, ip="203.0.113.10"):
    return api.post(URL, data, format="json", REMOTE_ADDR=ip)


class TestValidRequest:
    def test_saved_and_sms_sent_once(self, api, notify_phone, fake_semaphore):
        r = post(api, payload())
        assert r.status_code == 201, r.content
        assert r.json() == SUCCESS

        req = AccessRequest.objects.get()
        assert req.full_name == "Liza Manalo"
        assert req.phone_number == "+639181234567"
        assert req.email == "liza@example.com"
        assert req.farm_name == "Manalo Broiler Farm"
        assert req.location == "Capas, Tarlac"
        assert req.houses == 3
        assert req.role == AccessRequest.Role.OWNER
        assert req.consent is True
        assert req.status == AccessRequest.Status.NEW
        assert req.handled_at is None

        log = alert_logs().get()
        assert log.status == SmsLog.Status.SENT
        assert log.recipient == "09170000001"
        assert fake_semaphore["data"]["number"] == "09170000001"
        assert fake_semaphore["data"]["message"] == (
            "CheckN Go: new access request from Manalo Broiler Farm, "
            "Capas, Tarlac. Open the admin to reply."
        )

    def test_sms_log_holds_no_message_body(self, api, notify_phone, fake_semaphore):
        post(api, payload())
        log = alert_logs().get()
        assert log.message == "Access request alert sent"
        assert "Manalo" not in log.message
        assert "+639181234567" not in log.message

    def test_phone_is_normalized(self, api):
        r = post(api, payload(phone_number="+63 918-123 4567"))
        assert r.status_code == 201, r.content
        assert AccessRequest.objects.get().phone_number == "+639181234567"

    def test_optional_fields_may_be_left_out(self, api):
        data = payload()
        for key in ("email", "houses", "message", "website"):
            data.pop(key)
        r = post(api, data)
        assert r.status_code == 201, r.content
        req = AccessRequest.objects.get()
        assert req.email == ""
        assert req.houses is None
        assert req.message == ""

    def test_no_ip_address_is_stored(self, api):
        post(api, payload(), ip="198.51.100.77")
        req = AccessRequest.objects.get()
        field_names = {f.name for f in AccessRequest._meta.get_fields()}
        assert not {"ip", "ip_address", "remote_addr"} & field_names
        values = [str(getattr(req, f.attname)) for f in AccessRequest._meta.concrete_fields]
        assert not any("198.51.100.77" in v for v in values)

    def test_never_creates_a_user_or_farm(self, api, notify_phone, fake_semaphore):
        users, farms = User.objects.count(), Farm.objects.count()
        r = post(api, payload())
        assert r.status_code == 201
        assert User.objects.count() == users
        assert Farm.objects.count() == farms
        assert not User.objects.filter(phone_number="+639181234567").exists()

    def test_no_login_needed_and_a_bad_token_is_ignored(self, api):
        api.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        r = post(api, payload())
        assert r.status_code == 201


class TestValidation:
    def test_consent_false_is_400(self, api, notify_phone, fake_semaphore):
        r = post(api, payload(consent=False))
        assert r.status_code == 400
        assert "consent" in r.json()
        assert not AccessRequest.objects.exists()
        assert not alert_logs().exists()

    def test_consent_missing_is_400(self, api):
        data = payload()
        data.pop("consent")
        r = post(api, data)
        assert r.status_code == 400
        assert "consent" in r.json()

    @pytest.mark.parametrize("phone", ["09181234567", "12345", "+63abc1234567", ""])
    def test_bad_phone_is_400(self, api, phone):
        r = post(api, payload(phone_number=phone))
        assert r.status_code == 400
        assert "phone_number" in r.json()
        assert not AccessRequest.objects.exists()

    @pytest.mark.parametrize(
        "field,limit", [("full_name", 100), ("farm_name", 150), ("location", 150), ("message", 500)]
    )
    def test_too_long_is_400(self, api, field, limit):
        assert post(api, payload(**{field: "x" * (limit + 1)})).status_code == 400
        cache.clear()
        assert post(api, payload(**{field: "x" * limit})).status_code == 201

    @pytest.mark.parametrize("field", ["full_name", "phone_number", "farm_name", "location", "role"])
    def test_required_fields(self, api, field):
        data = payload()
        data.pop(field)
        r = post(api, data)
        assert r.status_code == 400
        assert field in r.json()

    def test_unknown_role_is_400(self, api):
        r = post(api, payload(role="WORKER"))
        assert r.status_code == 400
        assert "role" in r.json()

    def test_bad_email_is_400(self, api):
        r = post(api, payload(email="not-an-email"))
        assert r.status_code == 400
        assert "email" in r.json()

    @pytest.mark.parametrize("houses", [-1, 1000, "many"])
    def test_bad_houses_is_400(self, api, houses):
        r = post(api, payload(houses=houses))
        assert r.status_code == 400
        assert "houses" in r.json()

    def test_status_cannot_be_set_by_the_requester(self, api):
        r = post(api, payload(status="APPROVED", handled_at="2026-01-01T00:00:00Z"))
        assert r.status_code == 201
        req = AccessRequest.objects.get()
        assert req.status == AccessRequest.Status.NEW
        assert req.handled_at is None

    def test_a_json_array_body_is_400_not_500(self, api):
        r = api.post(URL, [payload()], format="json")
        assert r.status_code == 400


class TestHoneypot:
    def test_filled_honeypot_saves_nothing_and_sends_nothing(
        self, api, notify_phone, fake_semaphore
    ):
        r = post(api, payload(website="https://spam.example"))
        assert r.status_code == 201
        assert r.json() == SUCCESS
        assert not AccessRequest.objects.exists()
        assert not alert_logs().exists()
        assert "data" not in fake_semaphore

    def test_filled_honeypot_with_invalid_data_still_looks_like_success(self, api):
        r = post(api, {"website": "x", "consent": False})
        assert r.status_code == 201
        assert r.json() == SUCCESS
        assert not AccessRequest.objects.exists()


class TestThrottle:
    def test_fourth_request_in_an_hour_from_one_ip_is_429(self, api):
        for _ in range(3):
            assert post(api, payload()).status_code == 201
        r = post(api, payload())
        assert r.status_code == 429
        assert AccessRequest.objects.count() == 3

    def test_another_ip_is_not_affected(self, api):
        for _ in range(3):
            post(api, payload(), ip="203.0.113.10")
        assert post(api, payload(), ip="203.0.113.10").status_code == 429
        assert post(api, payload(), ip="203.0.113.99").status_code == 201

    def test_honeypot_hits_count_toward_the_throttle(self, api):
        for _ in range(3):
            post(api, payload(website="spam"))
        assert post(api, payload()).status_code == 429


class TestSmsRules:
    def test_eleventh_sms_in_24_hours_is_not_sent_but_request_is_saved(
        self, api, notify_phone, fake_semaphore
    ):
        for i in range(10):
            SmsLog.objects.create(
                recipient="09170000001",
                message="Access request alert sent",
                purpose=SmsLog.Purpose.ACCESS_REQUEST,
                status=SmsLog.Status.SENT,
            )
        r = post(api, payload())
        assert r.status_code == 201
        assert r.json() == SUCCESS
        assert AccessRequest.objects.count() == 1
        assert alert_logs().count() == 10
        assert "data" not in fake_semaphore

    def test_cap_counts_only_the_last_24_hours(self, api, notify_phone, fake_semaphore):
        for _ in range(10):
            SmsLog.objects.create(
                recipient="09170000001",
                message="Access request alert sent",
                purpose=SmsLog.Purpose.ACCESS_REQUEST,
                status=SmsLog.Status.SENT,
            )
        alert_logs().update(sent_at=timezone.now() - timedelta(hours=25))
        post(api, payload())
        assert alert_logs().count() == 11
        assert fake_semaphore["data"]["number"] == "09170000001"

    def test_cap_ignores_other_sms_purposes(self, api, notify_phone, fake_semaphore):
        for _ in range(10):
            SmsLog.objects.create(
                recipient="09171112222",
                message="Invitation PIN sent",
                purpose=SmsLog.Purpose.INVITATION,
                status=SmsLog.Status.SENT,
            )
        post(api, payload())
        assert alert_logs().count() == 1

    def test_ten_real_requests_send_ten_then_stop(self, api, notify_phone, fake_semaphore):
        # Spread over many IPs so the per-IP throttle doesn't intervene.
        for i in range(11):
            r = post(api, payload(farm_name=f"Farm {i}"), ip=f"203.0.113.{i + 1}")
            assert r.status_code == 201
        assert AccessRequest.objects.count() == 11
        assert alert_logs().count() == 10
        assert alert_logs().filter(status=SmsLog.Status.SENT).count() == 10

    def test_no_sms_when_the_variable_is_empty(self, api, settings, fake_semaphore):
        settings.ACCESS_REQUEST_NOTIFY_PHONE = ""
        r = post(api, payload())
        assert r.status_code == 201
        assert AccessRequest.objects.count() == 1
        assert not alert_logs().exists()
        assert "data" not in fake_semaphore

    def test_no_sms_when_sms_is_disabled(self, api, notify_phone, settings):
        settings.SMS_ENABLED = False
        r = post(api, payload())
        assert r.status_code == 201
        assert AccessRequest.objects.count() == 1
        assert not alert_logs().exists()

    def test_provider_failure_still_saves_and_answers_201(self, api, notify_phone, settings):
        # SMS on, but the autouse network block makes requests.post raise —
        # send_sms swallows it and records FAILED.
        settings.SMS_ENABLED = True
        settings.SEMAPHORE_API_KEY = "fake"
        from unittest.mock import patch

        with patch("notifications.sms.requests.post", side_effect=ConnectionError("down")):
            r = post(api, payload())
        assert r.status_code == 201
        assert AccessRequest.objects.count() == 1
        assert alert_logs().get().status == SmsLog.Status.FAILED

    def test_sms_never_contains_the_requesters_phone_or_email(
        self, api, notify_phone, fake_semaphore
    ):
        post(api, payload())
        text = fake_semaphore["data"]["message"]
        assert "+639181234567" not in text
        assert "09181234567" not in text
        assert "9181234567" not in text
        assert "liza@example.com" not in text
        assert len(text) <= 160

    def test_phone_or_email_typed_into_farm_or_location_is_cut_out(
        self, api, notify_phone, fake_semaphore
    ):
        post(
            api,
            payload(
                farm_name="Manalo Farm 0918 123 4567",
                location="Capas liza@example.com +639181234567",
            ),
        )
        text = fake_semaphore["data"]["message"]
        assert "9181234567" not in text
        assert "liza@example.com" not in text
        assert "Manalo Farm" in text and "Capas" in text

    def test_long_farm_and_location_fit_one_sms(self, api, notify_phone, fake_semaphore):
        post(api, payload(farm_name="F" * 150, location="L" * 150))
        text = fake_semaphore["data"]["message"]
        assert len(text) <= 160
        assert alert_logs().get().status == SmsLog.Status.SENT


# --- admin ---------------------------------------------------------------------


@pytest.fixture
def admin_client(db, settings):
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    superuser = User.objects.create_superuser(
        phone_number="+639170007000", password=PASSWORD, full_name="Platform Admin"
    )
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": superuser.phone_number, "password": PASSWORD},
    )
    assert resp.status_code == 302, resp.content
    return client


def make_request(**overrides):
    fields = payload(**overrides)
    fields.pop("website")
    return AccessRequest.objects.create(**fields)


CHANGELIST = f"/{ADMIN_URL}accounts/accessrequest/"


class TestAdmin:
    def test_list_masks_the_phone_and_hides_the_email(self, admin_client):
        make_request()
        html = admin_client.get(CHANGELIST).content.decode()
        assert mask_phone("+639181234567") in html
        assert "+639181234567" not in html
        assert "liza@example.com" not in html
        assert "Manalo Broiler Farm" in html

    def test_exact_phone_search_still_finds_the_request(self, admin_client):
        make_request()
        html = admin_client.get(CHANGELIST, {"q": "+639181234567"}).content.decode()
        assert "Manalo Broiler Farm" in html

    def test_detail_page_shows_full_phone_and_email(self, admin_client):
        req = make_request()
        html = admin_client.get(f"{CHANGELIST}{req.pk}/change/").content.decode()
        assert "+639181234567" in html
        assert "liza@example.com" in html

    def test_list_is_newest_first(self, admin_client):
        old = make_request(farm_name="Old Farm")
        AccessRequest.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        make_request(farm_name="New Farm")
        html = admin_client.get(CHANGELIST).content.decode()
        assert html.index("New Farm") < html.index("Old Farm")

    def test_filter_by_status(self, admin_client):
        make_request(farm_name="Unanswered Farm")
        make_request(farm_name="Answered Farm", status=AccessRequest.Status.CONTACTED)
        html = admin_client.get(CHANGELIST, {"status__exact": "CONTACTED"}).content.decode()
        assert "Answered Farm" in html
        assert "Unanswered Farm" not in html

    def test_filter_by_date(self, admin_client):
        old = make_request(farm_name="Old Farm")
        AccessRequest.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        make_request(farm_name="Recent Farm")
        since = (timezone.now() - timedelta(days=7)).isoformat()
        html = admin_client.get(CHANGELIST, {"created_at__gte": since}).content.decode()
        assert "Recent Farm" in html
        assert "Old Farm" not in html

    @pytest.mark.parametrize(
        "action,status",
        [
            ("mark_contacted", AccessRequest.Status.CONTACTED),
            ("mark_approved", AccessRequest.Status.APPROVED),
            ("mark_declined", AccessRequest.Status.DECLINED),
        ],
    )
    def test_actions_set_status_and_handled_at(self, admin_client, action, status):
        req = make_request()
        resp = admin_client.post(
            CHANGELIST, {"action": action, "_selected_action": [req.pk]}, follow=True
        )
        assert resp.status_code == 200
        req.refresh_from_db()
        assert req.status == status
        assert req.handled_at is not None

    def test_actions_never_create_a_user_or_farm(self, admin_client):
        req = make_request()
        users, farms = User.objects.count(), Farm.objects.count()
        admin_client.post(CHANGELIST, {"action": "mark_approved", "_selected_action": [req.pk]})
        assert User.objects.count() == users
        assert Farm.objects.count() == farms

    def test_no_add_page(self, admin_client):
        assert admin_client.get(f"{CHANGELIST}add/").status_code == 403

    def test_a_new_request_cannot_be_deleted(self, admin_client):
        req = make_request()
        resp = admin_client.post(f"{CHANGELIST}{req.pk}/delete/", {"post": "yes"})
        assert resp.status_code == 403
        assert AccessRequest.objects.filter(pk=req.pk).exists()

    def test_a_handled_request_can_be_deleted(self, admin_client):
        req = make_request(status=AccessRequest.Status.DECLINED, handled_at=timezone.now())
        resp = admin_client.post(f"{CHANGELIST}{req.pk}/delete/", {"post": "yes"})
        assert resp.status_code == 302
        assert not AccessRequest.objects.filter(pk=req.pk).exists()

    def test_bulk_delete_only_removes_handled_requests(self, admin_client):
        new = make_request(farm_name="New Farm")
        handled = make_request(
            farm_name="Handled Farm",
            status=AccessRequest.Status.CONTACTED,
            handled_at=timezone.now(),
        )
        # Step 1: the confirmation page lists only the handled request.
        resp = admin_client.post(
            CHANGELIST,
            {"action": "delete_handled", "_selected_action": [new.pk, handled.pk]},
        )
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Handled Farm" in html
        assert "New Farm" not in html
        # Step 2: confirm.
        admin_client.post(
            CHANGELIST,
            {"action": "delete_handled", "_selected_action": [handled.pk], "post": "yes"},
        )
        assert list(AccessRequest.objects.values_list("pk", flat=True)) == [new.pk]

    def test_stock_delete_selected_action_is_not_offered(self, admin_client):
        make_request()
        html = admin_client.get(CHANGELIST).content.decode()
        assert 'value="delete_selected"' not in html
        assert 'value="delete_handled"' in html

    def test_sidebar_lists_access_requests_under_people(self):
        from checkngo.admin_theme import SIDEBAR_NAVIGATION

        people = next(g for g in SIDEBAR_NAVIGATION if str(g["title"]) == "People")
        links = [str(item["link"]) for item in people["items"]]
        assert reverse("admin:accounts_accessrequest_changelist") in links
