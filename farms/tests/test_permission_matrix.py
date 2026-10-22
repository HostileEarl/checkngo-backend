# farms/tests/test_permission_matrix.py
"""
docs/PERMISSION_MATRIX.md stays true.

  1. The committed file equals what `python manage.py permission_matrix`
     would write today, so a new route or a changed role set cannot land
     without the matrix changing in the same commit.
  2. Every hand-written note names a route and method that exist.
  3. Every "no" in the table is refused by the running app (401 or 403,
     or 404 for the one owner-lookup view), for every role and route,
     unsafe methods included, with an otherwise valid body (the sweep's,
     test_farm_id_substitution.WRITE_BODIES). Every "yes" on a GET is let
     through. A matrix derived from the permission classes could still be
     wrong about what a request gets; this is what checks it.
"""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from farms.management.commands import permission_matrix
from farms.models import FarmMembership
from production.models import House

from . import test_farm_id_substitution as sweep
from .two_farms import FAST_HASHERS, build_farm, make_user

ROWS = permission_matrix.matrix_rows()


def test_committed_matrix_is_current():
    committed = permission_matrix.OUTPUT.read_text(encoding="utf-8")
    assert committed == permission_matrix.render(), (
        "docs/PERMISSION_MATRIX.md is out of date. Run: python manage.py permission_matrix"
    )


def test_every_note_names_a_real_route_and_method():
    methods = {(route.name, method) for route, method, _, _ in ROWS}
    stale = [
        (name, method)
        for name, by_method in permission_matrix.NOTES.items()
        for method in by_method
        if (name, method) not in methods
    ]
    stale += [
        (name, method)
        for name, by_method in permission_matrix.HOUSE_SCOPED.items()
        for method in by_method
        if (name, method) not in methods
    ]
    assert not stale, stale


def _cells(name, method):
    return next(cells for route, m, cells, _ in ROWS if route.name == name and m == method)


@pytest.mark.parametrize(
    "name,method,column,expected",
    [
        ("production:house-archive", "POST", "owner", "yes"),
        ("production:house-archive", "POST", "manager", "no"),
        ("production:daily-list", "POST", "worker", "yes"),
        ("production:daily-list", "POST", "worker_elsewhere", "no"),
        ("production:daily-list", "GET", "worker_elsewhere", "yes"),
        ("production:daily-bulk-sync", "POST", "worker_elsewhere", "per record"),
        ("production:task-completion-bulk-sync", "POST", "worker_elsewhere", "per record"),
        ("production:sale-list", "POST", "worker", "no"),
        ("analytics:report-feed-margin-xlsx", "GET", "worker", "no"),
        ("analytics:report-feed-margin-xlsx", "GET", "manager", "yes"),
        ("partners:farm-partners", "POST", "manager", "no"),
        ("accounts:credential-change", "POST", "unrotated", "yes"),
        ("accounts:me", "GET", "anonymous", "no"),
        ("accounts:login", "POST", "anonymous", "yes"),
        ("production:batch-list", "GET", "buyer", "no"),
        ("production:batch-list", "GET", "unrotated", "no"),
    ],
)
def test_spot_checks(name, method, column, expected):
    assert _cells(name, method)[column] == expected


# ── the matrix against the running app ──────────────────────────────────

# Not exercised live: the schema view renders the whole OpenAPI document
# (seconds per request) and carries no user or farm data; it is AllowAny,
# like the two documentation pages that ARE checked.
SKIP_LIVE = {"schema"}


@pytest.fixture
def callers(db, settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS
    world = build_farm("ALPHA", "+639171000")
    # A worker assigned to House 2 only: "unassigned" for House 1's batch.
    elsewhere = make_user("+639171000020", "ALPHA Elsewhere Worker", User.Role.WORKER)
    membership = FarmMembership.objects.create(
        farm=world.farm, user=elsewhere, role=FarmMembership.Role.WORKER
    )
    membership.houses.set([world.house2])
    unrotated = User.objects.create_user(
        "+639171000021", "246802", full_name="ALPHA Unrotated Worker", role=User.Role.WORKER
    )
    FarmMembership.objects.create(farm=world.farm, user=unrotated, role=FarmMembership.Role.WORKER)
    assert unrotated.must_change_credential
    users = {
        "owner": world.owner,
        "manager": world.manager,
        "worker": world.worker,
        "worker_elsewhere": elsewhere,
        "buyer": world.buyer,
        "anonymous": None,
        "unrotated": unrotated,
    }
    return world, users


def _url(route, world):
    if not route.kwargs:
        return reverse(route.name)
    return reverse(route.name, kwargs=sweep._kwargs(route, world, world))


def _refused(route, status):
    if route.name == "farms:farm-reactivate":
        return status in (401, 403, 404)
    return status in (401, 403)


@pytest.mark.django_db
def test_every_no_is_refused_and_every_get_yes_is_admitted(callers):
    world, users = callers
    problems = []
    for route, method, cells, _ in ROWS:
        if route.name in SKIP_LIVE:
            continue
        url = _url(route, world)
        for column, user in users.items():
            cell = cells[column]
            if cell == "per record" or (cell == "yes" and method != "GET"):
                continue  # admitted writes would change data; A1 and the role tests cover them
            client = APIClient()
            client.raise_request_exception = False
            if user is not None:
                client.force_authenticate(user=user)
            # The sweep's valid body for this write: a "no" must hold even
            # when nothing but the caller is wrong with the request.
            body = sweep._write_body(route, method, world) if route.is_farm_scoped else {}
            response = getattr(client, method.lower())(url, body, format="json")
            if cell == "no" and not _refused(route, response.status_code):
                problems.append(f"{method} {url} as {column}: matrix says no, got {response.status_code}")
            if cell == "yes" and response.status_code in (401, 403):
                problems.append(f"{method} {url} as {column}: matrix says yes, got {response.status_code}")
    assert not problems, "\n".join(problems)


def test_house_one_is_the_unassigned_workers_other_house(callers):
    """Guards the live check above: the 'elsewhere' worker really is outside House 1."""
    world, users = callers
    membership = FarmMembership.objects.get(user=users["worker_elsewhere"], farm=world.farm)
    assert not membership.may_write_to_house(world.house)
    assert isinstance(world.house, House)
