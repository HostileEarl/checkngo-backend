"""
The "Create Farm Owner" admin flow and its shared issuance helper.

The point being proved: an owner account minted here is indistinguishable
from one produced by an invitee accepting an invitation — system PIN, stored
only as the hash, forced rotation on first login, no farm — and the global
password validators are left intact.
"""
import re

import pytest
from django.contrib import admin
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse

from accounts.models import FarmOwnerAccount, User
from accounts.services import issue_owner_account

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _admin_render_prereqs(settings):
    """
    Rendering admin pages needs the plain static-files storage (pytest-django
    forces DEBUG=False, under which the manifest storage wants a collectstatic
    manifest the test run has not built). Also clear the throttle cache so a
    real login POST here is not affected by earlier tests.
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
        phone_number="+639179000009",
        password="ops-secret-99",
        full_name="Platform Ops",
    )


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


# ── the shared helper ────────────────────────────────────────────────────


class TestIssueOwnerAccount:
    def test_shape_matches_an_accepted_invitation(self):
        user, pin = issue_owner_account(
            full_name="Shape Test", phone_number="+639170000201"
        )

        assert pin.isdigit() and len(pin) == 6
        assert user.role == User.Role.OWNER
        assert user.must_change_credential is True
        assert user.is_staff is False
        assert user.is_active is True
        assert user.check_password(pin)
        assert not user.farm_memberships.exists()
        assert not user.owned_farms.exists()

    def test_pin_logs_in_once_then_forces_rotation(self, api):
        _, pin = issue_owner_account(
            full_name="Gate Test", phone_number="+639170000202"
        )
        login = reverse("accounts:login")

        first = api.post(
            login, {"phone_number": "+639170000202", "pin": pin}, format="json"
        )
        assert first.status_code == 200
        assert first.data["must_change_credential"] is True

        api.credentials(HTTP_AUTHORIZATION=f"Bearer {first.data['access']}")
        # Gate closed: every non-exempt endpoint is 403 until rotation.
        assert api.get(reverse("farms:farm-list")).status_code == 403

        rotate = api.post(
            reverse("accounts:credential-change"),
            {"current_pin": pin, "new_pin": "778899", "confirm_pin": "778899"},
            format="json",
        )
        assert rotate.status_code == 200
        assert api.get(reverse("farms:farm-list")).status_code == 200

        # The issued PIN is spent — it no longer authenticates.
        api.credentials()
        again = api.post(
            login, {"phone_number": "+639170000202", "pin": pin}, format="json"
        )
        assert again.status_code == 401

    def test_duplicate_phone_raises_and_writes_nothing(self):
        issue_owner_account(full_name="First", phone_number="+639170000203")
        before = User.objects.count()

        with pytest.raises(ValidationError):
            issue_owner_account(full_name="Second", phone_number="+639170000203")

        assert User.objects.count() == before

    def test_accepts_a_fixed_pin_for_local_testing(self):
        _, pin = issue_owner_account(
            full_name="Fixed", phone_number="+639170000204", pin="424242"
        )
        assert pin == "424242"


# ── the admin flow ───────────────────────────────────────────────────────


class TestFarmOwnerAccountAdmin:
    add_url = reverse("admin:accounts_farmowneraccount_add")

    def test_add_creates_owner_and_shows_pin_exactly_once(self, admin_client):
        resp = admin_client.post(
            self.add_url,
            {
                "full_name": "Panel Owner",
                "phone_number": "+63 917 000 0301",  # spaced — must normalise
                "email": "",
            },
            follow=True,
        )
        assert resp.status_code == 200

        owner = User.objects.get(phone_number="+639170000301")
        assert owner.role == User.Role.OWNER
        assert owner.must_change_credential is True
        assert owner.is_staff is False
        assert not owner.farm_memberships.exists()

        match = re.search(
            r"One-time PIN for Panel Owner: (\d{6})", resp.content.decode()
        )
        assert match, "the one-time PIN banner was not rendered"
        pin = match.group(1)
        assert owner.check_password(pin)

        # It is never shown again.
        changelist = admin_client.get(
            reverse("admin:accounts_farmowneraccount_changelist")
        )
        assert pin not in changelist.content.decode()
        change_page = admin_client.get(
            reverse("admin:accounts_user_change", args=[owner.pk])
        )
        assert pin not in change_page.content.decode()

    def test_no_password_fields_on_the_add_form(self, admin_client):
        body = admin_client.get(self.add_url).content.decode()
        assert 'name="password1"' not in body
        assert 'name="password2"' not in body

    def test_changelist_is_scoped_to_owners(self, admin_client):
        User.objects.create_user(
            phone_number="+639170000302",
            password="worker-pass-9",
            full_name="Worker Bee",
            role=User.Role.WORKER,
        )
        issue_owner_account(full_name="Real Owner", phone_number="+639170000303")

        body = admin_client.get(
            reverse("admin:accounts_farmowneraccount_changelist")
        ).content.decode()
        assert "Real Owner" in body
        assert "Worker Bee" not in body

    def test_no_edit_or_delete(self, rf, superuser):
        model_admin = admin.site._registry[FarmOwnerAccount]
        request = rf.get("/")
        request.user = superuser
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False

        owner, _ = issue_owner_account(
            full_name="Undeletable", phone_number="+639170000304"
        )
        client_resp = self._client(superuser).get(
            reverse("admin:accounts_farmowneraccount_delete", args=[owner.pk])
        )
        assert client_resp.status_code == 403

    @staticmethod
    def _client(user):
        from django.test import Client

        c = Client()
        c.force_login(user)
        return c


# ── the compromise that was NOT made ─────────────────────────────────────


class TestGlobalValidatorsUntouched:
    def test_validators_still_configured(self, settings):
        names = {
            v["NAME"].rsplit(".", 1)[-1] for v in settings.AUTH_PASSWORD_VALIDATORS
        }
        assert "MinimumLengthValidator" in names
        assert "NumericPasswordValidator" in names

    def test_stock_user_add_form_still_rejects_a_six_digit_pin(self, admin_client):
        resp = admin_client.post(
            reverse("admin:accounts_user_add"),
            {
                "phone_number": "+639170000401",
                "full_name": "Should Fail",
                "role": "OWNER",
                "password1": "123456",
                "password2": "123456",
            },
        )
        assert resp.status_code == 200  # re-rendered with errors, not saved
        assert not User.objects.filter(phone_number="+639170000401").exists()


# ── the owner is not an operator ─────────────────────────────────────────


def test_issued_owner_cannot_reach_the_admin(client):
    owner, _ = issue_owner_account(
        full_name="Not An Operator", phone_number="+639170000501"
    )
    assert owner.is_staff is False

    client.force_login(owner)
    resp = client.get(reverse("admin:index"))
    assert resp.status_code == 302
    assert "login" in resp["Location"]
