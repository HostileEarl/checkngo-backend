# conftest.py
"""
Shared fixtures. Two farms with overlapping and non-overlapping staff —
the shape needed to prove data isolation actually holds.
"""
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from accounts.models import Invitation, User
from farms.models import Farm, FarmMembership


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture(autouse=True)
def _block_semaphore_network(settings):
    """
    Safety net: no test may ever reach Semaphore's real API, regardless of
    what a developer's .env has set. Two layers:

      1. Forces SMS_ENABLED off and clears the API key, so send_sms takes
         its normal "skipped" path by default.
      2. Patches the one call site send_sms ever makes (requests.post)
         with a stub that records the call and raises — belt — but
         send_sms catches broad exceptions and logs them as an ordinary
         FAILED delivery, so a raise alone would be silently swallowed
         and the test would pass anyway. The real guarantee is the
         teardown check below: if the stub was ever actually invoked,
         this fixture fails the test itself. A test that needs to
         exercise a real-looking send should use the `fake_semaphore`
         fixture, which layers its own patch on top of this one.
    """
    settings.SEMAPHORE_API_KEY = ""
    settings.SMS_ENABLED = False

    calls = []

    def _blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError(
            "Blocked an unmocked call to Semaphore's API. Use the "
            "fake_semaphore fixture to simulate a response instead of "
            "reaching the real network."
        )

    with patch("notifications.sms.requests.post", side_effect=_blocked):
        yield

    if calls:
        pytest.fail(
            f"{len(calls)} unmocked call(s) reached "
            "notifications.sms.requests.post during this test. Tests must "
            "never talk to Semaphore's real API — use the fake_semaphore "
            "fixture."
        )


@pytest.fixture
def fake_semaphore(settings):
    """
    Opt in to a simulated Semaphore call: turns on SMS_ENABLED and a fake
    API key, and stubs requests.post to return a realistic v4 /messages
    response (a JSON list with one message object). Yields the captured
    request so a test can assert on what was actually posted — recipient,
    message body, sender name — without reaching into mock call args
    itself.
    """
    settings.SEMAPHORE_API_KEY = "fake-test-key"
    settings.SMS_ENABLED = True

    captured = {}

    def _respond(url, data=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = [
            {
                "message_id": 1000001,
                "user_id": 1,
                "user": "test@checkngo.app",
                "account_id": 1,
                "account": "CheckN Go Test Account",
                "recipient": (data or {}).get("number", ""),
                "message": (data or {}).get("message", ""),
                "sender_name": (data or {}).get("sendername", "Semaphore"),
                "network": "Globe",
                "status": "Pending",
                "type": "single",
                "source": "api",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            }
        ]
        return response

    with patch("notifications.sms.requests.post", side_effect=_respond):
        yield captured


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "+639171112222",
        "111111",
        full_name="Maria Santos",
        role=User.Role.OWNER,
        must_change_credential=False,
    )


@pytest.fixture
def rival_owner(db):
    return User.objects.create_user(
        "+639179998888",
        "999999",
        full_name="Rival Owner",
        role=User.Role.OWNER,
        must_change_credential=False,
    )


@pytest.fixture
def manager(db):
    return User.objects.create_user(
        "+639172223333",
        "222222",
        full_name="Jose Cruz",
        role=User.Role.MANAGER,
        must_change_credential=False,
    )


@pytest.fixture
def worker(db):
    return User.objects.create_user(
        "+639173334444",
        "333333",
        full_name="Ana Reyes",
        role=User.Role.WORKER,
        must_change_credential=False,
    )


@pytest.fixture
def gated_worker(db):
    """A freshly onboarded worker who has NOT yet rotated their PIN."""
    return User.objects.create_user(
        "+639174445555",
        "444444",
        full_name="Pedro Baldo",
        role=User.Role.WORKER,
    )


@pytest.fixture
def supplier(db):
    return User.objects.create_user(
        "+639175556666",
        "555555",
        full_name="FeedCo Supplier",
        role=User.Role.SUPPLIER,
        must_change_credential=False,
    )


@pytest.fixture
def farm(db, owner):
    return Farm.objects.create(name="Santos Layer Farm", owner=owner)


@pytest.fixture
def rival_farm(db, rival_owner):
    return Farm.objects.create(name="Rival Broiler Site", owner=rival_owner)


@pytest.fixture
def staffed_farm(farm, manager, worker, gated_worker, owner):
    FarmMembership.objects.create(
        farm=farm, user=manager, role=FarmMembership.Role.MANAGER, invited_by=owner
    )
    FarmMembership.objects.create(
        farm=farm, user=worker, role=FarmMembership.Role.WORKER, invited_by=owner
    )
    FarmMembership.objects.create(
        farm=farm, user=gated_worker, role=FarmMembership.Role.WORKER, invited_by=owner
    )
    return farm


@pytest.fixture
def auth(api):
    """Authenticate the client as a given user."""

    def _auth(user):
        api.force_authenticate(user=user)
        return api

    return _auth