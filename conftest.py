# conftest.py
"""
Shared fixtures. Two farms with overlapping and non-overlapping staff —
the shape needed to prove data isolation actually holds.
"""
import pytest
from rest_framework.test import APIClient

from accounts.models import Invitation, User
from farms.models import Farm, FarmMembership


@pytest.fixture
def api():
    return APIClient()


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