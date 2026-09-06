"""
Admin hardening — django-axes rate limiting on the admin login, and the
admin served from a non-default, env-configured path.

The load-bearing test here is `TestApiLoginUntouchedByAxes`. Adding an
authentication backend is exactly the kind of change that silently breaks
an unrelated login path, so we prove the farm-facing API login still
returns 401 and is still governed by its own SimpleJWT throttle, not axes.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from axes.models import AccessAttempt

from checkngo.urls import ADMIN_URL

pytestmark = pytest.mark.django_db

User = get_user_model()

WRONG = "not-the-password"
RIGHT = "Correct-Horse-9-Battery"


@pytest.fixture(autouse=True)
def _clean_slate(settings):
    """
    axes counts failures in the database (rolled back per test); the API
    throttle counts in the cache (LocMemCache, which persists for the whole
    run). Clear both so each test starts from zero regardless of order.

    Also swap the manifest static-files storage for the plain one: these
    tests render the admin login page, and pytest-django forces DEBUG=False,
    under which ManifestStaticFilesStorage insists on a collectstatic
    manifest the test run has not built.
    """
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
    cache.clear()
    AccessAttempt.objects.all().delete()
    yield
    cache.clear()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        phone_number="+639170007000",
        password=RIGHT,
        full_name="Platform Admin",
    )


@pytest.fixture
def login_url():
    return reverse("admin:login")


class TestAdminLoginLockout:
    def _fail(self, client, login_url, phone):
        return client.post(login_url, {"username": phone, "password": WRONG})

    def test_five_failures_lock_the_account(self, admin_user, login_url):
        client = Client()
        for _ in range(5):
            self._fail(client, login_url, admin_user.phone_number)

        # Sixth attempt — this time with the CORRECT password. Still refused.
        locked = client.post(
            login_url,
            {"username": admin_user.phone_number, "password": RIGHT},
        )
        assert locked.status_code == 429
        assert "_auth_user_id" not in client.session

    def test_successful_login_resets_the_counter(self, admin_user, login_url):
        client = Client()
        for _ in range(4):  # one short of the limit
            self._fail(client, login_url, admin_user.phone_number)
        assert (
            AccessAttempt.objects.filter(
                username=admin_user.phone_number
            ).count()
            == 1
        )

        ok = client.post(
            login_url,
            {"username": admin_user.phone_number, "password": RIGHT},
        )
        assert ok.status_code == 302  # into the admin
        assert "_auth_user_id" in client.session
        assert (
            AccessAttempt.objects.filter(
                username=admin_user.phone_number
            ).count()
            == 0
        )

        # A fresh client: four more failures still do not lock, proving the
        # counter was reset rather than merely paused.
        other = Client()
        last = None
        for _ in range(4):
            last = self._fail(other, login_url, admin_user.phone_number)
        assert last.status_code == 200  # the login form, not a lockout
        assert "_auth_user_id" not in other.session


class TestApiLoginUntouchedByAxes:
    def test_failed_api_login_is_401_never_an_axes_lockout(self, api, owner):
        url = reverse("accounts:login")
        # Seven failures — past the axes limit of 5. If axes governed this
        # path the later attempts would be a 429 lockout; every one is a
        # plain 401 from SimpleJWT instead.
        for _ in range(7):
            r = api.post(
                url,
                {"phone_number": owner.phone_number, "pin": "000000"},
                format="json",
            )
            assert r.status_code == 401

        # And axes did not lock the admin for this username as a side effect.
        client = Client()
        r = client.post(
            reverse("admin:login"),
            {"username": owner.phone_number, "password": "000000"},
        )
        assert r.status_code == 200

    def test_api_login_still_governed_by_its_own_throttle(self, api, owner):
        url = reverse("accounts:login")
        codes = [
            api.post(
                url,
                {"phone_number": owner.phone_number, "pin": "000000"},
                format="json",
            ).status_code
            for _ in range(12)
        ]
        # The existing PhoneLoginThrottle is 10/hour: the first ten are 401,
        # then it trips to 429 — and it is DRF's throttle, not an axes page.
        assert codes[:10] == [401] * 10
        assert codes[10:] == [429, 429]

        r = api.post(
            url,
            {"phone_number": owner.phone_number, "pin": "000000"},
            format="json",
        )
        assert r.status_code == 429
        assert "throttled" in str(r.data).lower()

    def test_valid_api_login_still_works(self, api, owner):
        r = api.post(
            reverse("accounts:login"),
            {"phone_number": owner.phone_number, "pin": "111111"},
            format="json",
        )
        assert r.status_code == 200
        assert "access" in r.data


class TestAdminPath:
    def test_admin_is_served_at_the_configured_path(self, client):
        r = client.get("/" + ADMIN_URL)
        assert r.status_code == 302  # unauthenticated -> admin login
        assert ADMIN_URL in r["Location"]
        assert client.get("/" + ADMIN_URL + "login/").status_code == 200

    @pytest.mark.skipif(
        ADMIN_URL == "admin/",
        reason="ADMIN_URL left at its default; nothing to prove about /admin/",
    )
    def test_default_admin_path_is_dead(self, client):
        assert client.get("/admin/").status_code == 404
        assert client.get("/admin/login/").status_code == 404
