# accounts/tests/test_login_throttle.py
"""
Login throttling (manuscript review evidence).

Two layers guard POST /api/auth/login/ (accounts/throttles.py, rates in
settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]):

  PhoneLoginThrottle  10/hour per phone number: the targeted account.
  LoginIPThrottle     30/hour per IP: one source spraying many accounts.

The per-phone limit already has a test
(test_admin_hardening.py::TestApiLoginUntouchedByAxes::test_api_login_still_governed_by_its_own_throttle).
This file adds the IP layer, the PIN-change throttle, and the per-phone
limit under the number formats the login itself accepts: login strips
spaces and dashes before looking the account up
(UserManager.normalize_phone), so "+63 917 111 2222" is the same account
as "+639171112222" and must share its limit.
"""
import pytest
from django.core.cache import cache
from django.urls import reverse

from accounts.models import User
from farms.tests.two_farms import FAST_HASHERS

pytestmark = pytest.mark.django_db

LOGIN = reverse("accounts:login")


@pytest.fixture(autouse=True)
def _fresh_throttle_cache(settings):
    # Every login attempt checks a PIN hash; the production hasher would
    # make the fifty attempts here take a minute.
    settings.PASSWORD_HASHERS = FAST_HASHERS
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "+639171112222", "111111", full_name="Throttle Owner",
        role=User.Role.OWNER, must_change_credential=False,
    )


@pytest.fixture
def gated_worker(db):
    return User.objects.create_user(
        "+639174445555", "444444", full_name="Throttle Worker", role=User.Role.WORKER
    )


def _login(api, phone, pin="000000", ip="198.51.100.7"):
    return api.post(LOGIN, {"phone_number": phone, "pin": pin}, format="json", REMOTE_ADDR=ip)


def test_the_per_phone_limit_covers_every_format_of_the_number(api, owner):
    assert owner.phone_number == "+639171112222"
    for _ in range(10):
        assert _login(api, owner.phone_number).status_code == 401
    assert _login(api, owner.phone_number).status_code == 429

    # The same account, written the way people type it. Each is accepted by
    # the login itself; none may open a fresh ten attempts.
    variants = ["+63 917 111 2222", "+63-917-111-2222", "+63917 1112222", " +639171112222 "]
    codes = {variant: _login(api, variant).status_code for variant in variants}
    assert all(code == 429 for code in codes.values()), codes


def test_a_formatted_number_does_log_in(api, owner):
    """Guards the test above: the variants really are this account."""
    response = _login(api, "+63 917 111 2222", pin="111111")
    assert response.status_code == 200, response.data


def test_the_ip_layer_stops_one_source_spraying_many_accounts(api):
    for n in range(30):
        assert _login(api, f"+6391790000{n:02d}").status_code == 401
    assert _login(api, "+639179000099").status_code == 429
    # Another source is unaffected.
    assert _login(api, "+639179000099", ip="198.51.100.8").status_code == 401


def test_a_throttled_account_is_refused_even_with_the_right_pin(api, owner):
    for _ in range(10):
        _login(api, owner.phone_number)
    assert _login(api, owner.phone_number, pin="111111").status_code == 429


def test_pin_change_is_throttled_at_five_an_hour(auth, gated_worker):
    client = auth(gated_worker)
    url = reverse("accounts:credential-change")
    body = {"current_pin": "000000", "new_pin": "246810", "confirm_pin": "246810"}
    codes = [client.post(url, body, format="json").status_code for _ in range(6)]
    assert codes[:5] == [400] * 5
    assert codes[5] == 429
    gated_worker.refresh_from_db()
    assert gated_worker.must_change_credential
