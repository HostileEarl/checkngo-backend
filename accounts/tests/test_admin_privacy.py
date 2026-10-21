# accounts/tests/test_admin_privacy.py
"""
The technical administrator creates owner accounts and maintains
accounts — they take no part in farm operations. This proves:

- every farm-operational admin page is gone (404, not just hidden)
- the admin index still loads and owner creation still works exactly as
  before, PIN included
- phone numbers are masked in the User changelist, but an exact-number
  search still finds the person
- platform_stats() is counts only — no names, no phone numbers
"""
import re

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts.models import Invitation, User, mask_phone
from checkngo.platform_stats import platform_stats
from checkngo.urls import ADMIN_URL

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-9-Battery"

# Every farm-operational admin this task removes, as (app_label, model_name).
REMOVED_MODELS = [
    ("production", "house"),
    ("production", "batch"),
    ("production", "dailyrecord"),
    ("production", "weightsample"),
    ("production", "harvest"),
    ("production", "saleevent"),
    ("production", "payment"),
    ("production", "feeddelivery"),
    ("production", "inventoryitem"),
    ("production", "inventorystockin"),
    ("production", "inventoryusagelog"),
    ("production", "recordcorrection"),
    ("production", "tasktemplate"),
    ("production", "taskcompletion"),
]


@pytest.fixture(autouse=True)
def _static_storage_for_admin_pages(settings):
    """
    Rendering an admin page needs a collectstatic manifest under
    ManifestStaticFilesStorage, which pytest-django's forced DEBUG=False
    would otherwise require. Swap to the plain storage, same fix as
    accounts/tests/test_admin_hardening.py.
    """
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
    # Not Client.login() — axes' auth backend requires a request, which
    # only a real POST to the login view supplies (same as
    # test_admin_hardening.py).
    client = Client()
    resp = client.post(
        reverse("admin:login"),
        {"username": superuser.phone_number, "password": PASSWORD},
    )
    assert resp.status_code == 302, resp.content
    return client


class TestRemovedAdminPagesAre404:
    @pytest.mark.parametrize("app_label,model_name", REMOVED_MODELS)
    def test_changelist_is_404(self, admin_client, app_label, model_name):
        url = f"/{ADMIN_URL}{app_label}/{model_name}/"
        assert admin_client.get(url).status_code == 404

    @pytest.mark.parametrize("app_label,model_name", REMOVED_MODELS)
    def test_add_page_is_404(self, admin_client, app_label, model_name):
        url = f"/{ADMIN_URL}{app_label}/{model_name}/add/"
        assert admin_client.get(url).status_code == 404

    def test_a_farms_operational_data_is_unreachable_from_any_admin_url(
        self, admin_client
    ):
        """
        Belt and suspenders beyond the per-model 404s above: the whole
        production app namespace is gone from the admin, so even a path
        for a model the test above doesn't enumerate 404s the same way.
        """
        for path in [
            f"/{ADMIN_URL}production/",
            f"/{ADMIN_URL}production/house/1/change/",
            f"/{ADMIN_URL}production/batch/1/change/",
            f"/{ADMIN_URL}production/saleevent/1/change/",
        ]:
            assert admin_client.get(path).status_code == 404


class TestAdminIndexLoads:
    def test_index_loads_for_a_superuser(self, admin_client):
        resp = admin_client.get(f"/{ADMIN_URL}")
        assert resp.status_code == 200

    def test_unauthenticated_is_redirected_not_500(self):
        resp = Client().get(f"/{ADMIN_URL}")
        assert resp.status_code == 302


class TestOwnerCreationStillWorks:
    def test_creating_an_owner_shows_the_pin_once(self, admin_client):
        resp = admin_client.post(
            f"/{ADMIN_URL}accounts/farmowneraccount/add/",
            {
                "full_name": "New Owner",
                "phone_number": "+639170001234",
                "email": "",
            },
            follow=True,
        )
        assert resp.status_code == 200
        messages = [str(m) for m in resp.context["messages"]]
        pin_message = next((m for m in messages if "One-time PIN" in m), None)
        assert pin_message is not None, messages

        match = re.search(r"One-time PIN for New Owner: (\d{6})", pin_message)
        assert match is not None, pin_message

        user = User.objects.get(phone_number="+639170001234")
        assert user.role == User.Role.OWNER
        assert user.must_change_credential is True
        assert user.check_password(match.group(1))


class TestPhoneNumberMasking:
    def test_user_changelist_masks_the_phone_number(self, admin_client, owner):
        resp = admin_client.get(f"/{ADMIN_URL}accounts/user/")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert owner.phone_number not in body
        assert mask_phone(owner.phone_number) in body

    def test_exact_phone_search_still_finds_the_user(self, admin_client, owner):
        resp = admin_client.get(
            f"/{ADMIN_URL}accounts/user/", {"q": owner.phone_number}
        )
        assert resp.status_code == 200
        assert owner.full_name in resp.content.decode()

    def test_user_changelist_never_shows_email(self, admin_client, owner):
        owner.email = "owner@example.com"
        owner.save(update_fields=["email"])
        resp = admin_client.get(f"/{ADMIN_URL}accounts/user/")
        assert "owner@example.com" not in resp.content.decode()

    def test_user_change_form_has_no_farm_data_or_email_field(
        self, admin_client, owner, staffed_farm
    ):
        resp = admin_client.get(f"/{ADMIN_URL}accounts/user/{owner.pk}/change/")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert staffed_farm.name not in body
        assert 'name="email"' not in body

    def test_invitation_changelist_masks_the_phone_number(self, admin_client, owner):
        invitation = Invitation.objects.create(
            phone_number="+639171112222",
            full_name="Some Buyer",
            account_role=User.Role.CONSUMER,
            membership_role="",
            pin_hash="x",
        )
        resp = admin_client.get(f"/{ADMIN_URL}accounts/invitation/")
        body = resp.content.decode()
        assert invitation.phone_number not in body
        assert mask_phone(invitation.phone_number) in body

    def test_farm_owner_account_changelist_masks_the_phone_number(
        self, admin_client, owner
    ):
        resp = admin_client.get(f"/{ADMIN_URL}accounts/farmowneraccount/")
        body = resp.content.decode()
        assert owner.phone_number not in body
        assert mask_phone(owner.phone_number) in body


class TestPlatformStatsIsCountsOnly:
    def _assert_all_leaves_are_ints(self, value, path=""):
        if isinstance(value, dict):
            for key, sub in value.items():
                self._assert_all_leaves_are_ints(sub, f"{path}.{key}")
        elif isinstance(value, str):
            # Only "generated_at" (an ISO timestamp) may be a string.
            assert path.endswith("generated_at"), f"unexpected string at {path}: {value!r}"
        else:
            assert isinstance(value, int), f"non-int leaf at {path}: {value!r}"

    def test_every_count_is_an_integer(self, db):
        cache.clear()
        stats = platform_stats()
        self._assert_all_leaves_are_ints(stats)

    def test_no_names_or_phone_numbers_leak_through(
        self, db, owner, manager, worker, consumer, staffed_farm
    ):
        cache.clear()
        stats = platform_stats()
        blob = str(stats)
        for leaked in (
            owner.full_name,
            owner.phone_number,
            manager.full_name,
            worker.phone_number,
            consumer.full_name,
            staffed_farm.name,
        ):
            assert leaked not in blob

    def test_has_the_expected_top_level_keys(self, db):
        cache.clear()
        stats = platform_stats()
        assert set(stats) == {
            "generated_at",
            "farms",
            "users",
            "houses",
            "batches",
            "invitations",
            "daily_records_last_7_days",
            "daily_records_last_30_days",
            "sales_last_30_days",
            "sms_last_30_days",
        }

    def test_is_cached_for_five_minutes(self, db, owner):
        cache.clear()
        first = platform_stats()
        User.objects.create_user(
            "+639170009999", "123456", full_name="Late Owner", role=User.Role.OWNER
        )
        second = platform_stats()
        assert first["users"]["owners"] == second["users"]["owners"]
