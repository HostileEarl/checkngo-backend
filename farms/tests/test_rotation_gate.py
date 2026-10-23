# farms/tests/test_rotation_gate.py
"""
THE GATE everywhere: a user still holding a one-time PIN may use nothing
except the endpoints needed to change it.

HasRotatedCredential used to be applied only through
REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]. A view that sets its own
permission_classes REPLACES that list, so every farm-scoped view skipped
the gate: a gated worker could read batches and post daily records. The
gate now also lives in FarmScopedPermission (and the partner permissions),
and the URL walk below fails for any view that ends up without it.
"""
from datetime import date
from types import SimpleNamespace

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from accounts.models import Invitation, User
from accounts.views import (
    AccessRequestCreateView,
    CredentialChangeView,
    InvitationAcceptView,
    LogoutView,
    MeView,
    PhonePinLoginView,
)
from farms.models import Farm
from farms.permissions import (
    FarmScopedPermission,
    HasRotatedCredential,
    IsExternalPartner,
)
from partners.models import FarmPartnerLink
from partners.permissions import IsLinkedPartner
from production.models import Batch, DailyRecord, House

GATE_MESSAGE = HasRotatedCredential.message

# The deliberate exceptions, by URL name, pinned to the view class that
# serves them so a different view can't be slipped in under an exempt name.
UNGATED_BY_DESIGN = {
    # Needed to get to, and through, the PIN change.
    "accounts:login": PhonePinLoginView,
    "accounts:token-refresh": TokenRefreshView,
    "accounts:token-verify": TokenVerifyView,
    "accounts:logout": LogoutView,
    "accounts:me": MeView,
    "accounts:credential-change": CredentialChangeView,
    # Public: the caller has no account yet.
    "accounts:invitation-accept": InvitationAcceptView,
    "accounts:access-request-create": AccessRequestCreateView,
    # Public API documentation (SERVE_PERMISSIONS = AllowAny): no user or
    # farm data, and nothing a gated user sees that an anonymous one can't.
    "schema": SpectacularAPIView,
    "swagger-ui": SpectacularSwaggerView,
    "redoc": SpectacularRedocView,
}

# A permission class counts as the gate if it is, or extends, one of these.
# Each refuses a user whose must_change_credential is True — proved by
# TestGateClassesRefuseGatedUsers rather than assumed.
GATE_BASES = (HasRotatedCredential, FarmScopedPermission, IsExternalPartner, IsLinkedPartner)
PARTNER_GATES = (IsExternalPartner, IsLinkedPartner)

# Django's admin is session-authenticated staff tooling, not the API.
SKIPPED_NAMESPACES = {"admin"}


def _walk(patterns, namespace=None, prefix=""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            inner = entry.namespace
            if namespace and inner:
                inner = f"{namespace}:{inner}"
            yield from _walk(entry.url_patterns, inner or namespace, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            name = entry.name
            if namespace and name:
                name = f"{namespace}:{name}"
            yield namespace, name, prefix + str(entry.pattern), entry.callback


def _api_routes():
    """(name, route, view class, effective permission classes) for every API URL."""
    routes = []
    for namespace, name, route, callback in _walk(get_resolver().url_patterns):
        if namespace and namespace.split(":")[0] in SKIPPED_NAMESPACES:
            continue
        view_cls = getattr(callback, "view_class", None)
        initkwargs = getattr(callback, "view_initkwargs", {}) or {}
        permissions = initkwargs.get(
            "permission_classes", getattr(view_cls, "permission_classes", ())
        )
        routes.append((name, route, view_cls, tuple(permissions)))
    return routes


def _is_gate(permission):
    # A composed permission (A | B, A & B) is not a type; it fails here and
    # has to be looked at by hand, since "gate | anything" is no gate.
    return isinstance(permission, type) and issubclass(permission, GATE_BASES)


class TestEveryViewIsGated:
    """Walks the real URLconf, so a view added later is covered automatically."""

    def test_every_api_view_is_gated_or_explicitly_exempt(self):
        problems = []
        for name, route, view_cls, permissions in _api_routes():
            if view_cls is None or not issubclass(view_cls, APIView):
                problems.append(f"{route} ({name}): not a DRF view; review the gate by hand")
                continue
            if name in UNGATED_BY_DESIGN:
                if view_cls is not UNGATED_BY_DESIGN[name]:
                    problems.append(
                        f"{route} ({name}): exempt name now served by {view_cls.__name__}, "
                        f"not {UNGATED_BY_DESIGN[name].__name__}"
                    )
                continue
            if view_cls.get_permissions is not APIView.get_permissions:
                problems.append(
                    f"{route} ({name}): {view_cls.__name__} overrides get_permissions; "
                    "the gate cannot be checked statically"
                )
                continue
            if not any(_is_gate(p) for p in permissions):
                listed = ", ".join(getattr(p, "__name__", repr(p)) for p in permissions)
                problems.append(f"{route} ({name}): {view_cls.__name__} [{listed}] skips the gate")

        assert not problems, "Views reachable with an unrotated PIN:\n  " + "\n  ".join(problems)

    def test_exemptions_are_not_stale(self):
        names = {name for name, _, _, _ in _api_routes()}
        assert set(UNGATED_BY_DESIGN) <= names

    def test_walk_reaches_the_farm_scoped_views(self):
        # Guards the walk itself: if it silently found nothing, the test
        # above would pass vacuously.
        names = {name for name, _, _, _ in _api_routes()}
        assert {
            "production:batch-list",
            "production:daily-list",
            "analytics:alerts",
            "farms:farm-members",
            "partners:farm-partners",
            "partners:my-purchases",
        } <= names


def _gates_in_use():
    found = {p for _, _, _, perms in _api_routes() for p in perms if _is_gate(p)}
    return sorted(found | set(PARTNER_GATES), key=lambda p: f"{p.__module__}.{p.__name__}")


@pytest.mark.django_db
class TestGateClassesRefuseGatedUsers:
    """
    The walk trusts a class because of what it extends; this proves each
    one actually refuses a gated user. The probe is the most privileged
    caller each class knows — the owner of the farm (or, for partner
    classes, a buyer linked to it) — so the only reason left to refuse is
    the PIN. The rotated control proves the refusal is the gate's.
    """

    @pytest.mark.parametrize("permission_cls", _gates_in_use(), ids=lambda p: p.__name__)
    @pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
    def test_refuses_until_rotated(self, permission_cls, method):
        owner = User.objects.create_user(
            "+639170000901", "901901", full_name="Probe Owner", role=User.Role.OWNER
        )
        farm = Farm.objects.create(name="Probe Farm", owner=owner)
        user = owner
        if issubclass(permission_cls, PARTNER_GATES):
            user = User.objects.create_user(
                "+639170000902", "902902", full_name="Probe Buyer", role=User.Role.CONSUMER
            )
            FarmPartnerLink.objects.create(
                farm=farm,
                partner=user,
                link_type=FarmPartnerLink.LinkType.CONSUMER,
                linked_by=owner,
            )
        assert user.must_change_credential is True
        view = SimpleNamespace(kwargs={"farm_pk": farm.pk, "pk": farm.pk})

        def check():
            permission = permission_cls()
            request = SimpleNamespace(user=user, method=method, data={})
            return permission.has_permission(request, view), getattr(permission, "message", None)

        allowed, message = check()
        assert allowed is False
        assert message == GATE_MESSAGE

        user.must_change_credential = False
        assert check()[0] is True


# ── behaviour, end to end ────────────────────────────────────────────────


def _rotate(client, current_pin):
    response = client.post(
        reverse("accounts:credential-change"),
        {"current_pin": current_pin, "new_pin": "246810", "confirm_pin": "246810"},
        format="json",
    )
    assert response.status_code == 200, response.data


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Gate House", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="GATE-001",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


WORKER_READS = [
    "production:batch-list",
    "production:house-list",
    "analytics:alerts",
    "farms:farm-members",
    "production:feed-stock",
    "production:inventory-item-list",
]


def _daily_record_url(farm, batch):
    return reverse("production:daily-list", kwargs={"farm_pk": farm.pk, "batch_pk": batch.pk})


DAILY_RECORD = {
    "record_date": "2026-01-05",
    "mortality_disease": 1,
    "mortality_heat": 0,
    "mortality_culled": 0,
    "mortality_unknown": 0,
    "feed_kg": "10.00",
    "notes": "",
}


@pytest.mark.django_db
class TestGatedWorker:
    @pytest.mark.parametrize("url_name", WORKER_READS)
    def test_farm_reads_refused(self, auth, gated_worker, staffed_farm, batch, url_name):
        response = auth(gated_worker).get(reverse(url_name, kwargs={"farm_pk": staffed_farm.pk}))
        assert response.status_code == 403
        assert response.data["detail"] == GATE_MESSAGE

    def test_daily_record_refused_and_nothing_saved(self, auth, gated_worker, staffed_farm, batch):
        response = auth(gated_worker).post(
            _daily_record_url(staffed_farm, batch), DAILY_RECORD, format="json"
        )
        assert response.status_code == 403
        assert response.data["detail"] == GATE_MESSAGE
        assert not DailyRecord.objects.filter(batch=batch).exists()

    def test_rotation_opens_the_same_requests(self, auth, gated_worker, staffed_farm, batch):
        client = auth(gated_worker)
        _rotate(client, "444444")

        for url_name in WORKER_READS:
            response = client.get(reverse(url_name, kwargs={"farm_pk": staffed_farm.pk}))
            assert response.status_code == 200, (url_name, response.data)

        response = client.post(_daily_record_url(staffed_farm, batch), DAILY_RECORD, format="json")
        assert response.status_code == 201, response.data
        assert DailyRecord.objects.filter(batch=batch).count() == 1


@pytest.fixture
def gated_owner(db):
    return User.objects.create_user(
        "+639170000801", "801801", full_name="New Owner", role=User.Role.OWNER
    )


@pytest.fixture
def gated_owner_farm(gated_owner):
    return Farm.objects.create(name="Fresh Start Farm", owner=gated_owner)


OWNER_READS = [
    ("farms:farm-detail", "pk"),
    ("farms:ownership-history", "farm_pk"),
    ("farms:farm-invitations", "farm_pk"),
    ("partners:farm-partners", "farm_pk"),
    ("analytics:profitability", "farm_pk"),
    ("analytics:report-daily-records", "farm_pk"),
]


@pytest.mark.django_db
class TestGatedOwner:
    @pytest.mark.parametrize("url_name,kwarg", OWNER_READS)
    def test_owner_reads_refused(self, auth, gated_owner, gated_owner_farm, url_name, kwarg):
        response = auth(gated_owner).get(reverse(url_name, kwargs={kwarg: gated_owner_farm.pk}))
        assert response.status_code == 403
        assert response.data["detail"] == GATE_MESSAGE

    def test_owner_writes_refused_and_nothing_changes(self, auth, gated_owner, gated_owner_farm):
        client = auth(gated_owner)
        farm_pk = {"farm_pk": gated_owner_farm.pk}

        response = client.post(reverse("farms:farm-archive", kwargs=farm_pk))
        assert response.status_code == 403
        gated_owner_farm.refresh_from_db()
        assert gated_owner_farm.is_active is True

        response = client.post(
            reverse("production:house-list", kwargs=farm_pk),
            {"name": "House 1", "capacity": 1000},
            format="json",
        )
        assert response.status_code == 403
        assert not House.objects.filter(farm=gated_owner_farm).exists()

        response = client.post(
            reverse("farms:farm-invitations", kwargs=farm_pk),
            {"phone_number": "+639170000802", "full_name": "Would-be Worker", "membership_role": "WORKER"},
            format="json",
        )
        assert response.status_code == 403
        assert not Invitation.objects.filter(farm=gated_owner_farm).exists()

    def test_rotation_opens_the_same_requests(self, auth, gated_owner, gated_owner_farm):
        client = auth(gated_owner)
        _rotate(client, "801801")

        for url_name, kwarg in OWNER_READS:
            response = client.get(reverse(url_name, kwargs={kwarg: gated_owner_farm.pk}))
            assert response.status_code == 200, (url_name, getattr(response, "data", None))

        response = client.post(
            reverse("production:house-list", kwargs={"farm_pk": gated_owner_farm.pk}),
            {"name": "House 1", "capacity": 1000},
            format="json",
        )
        assert response.status_code == 201, response.data


@pytest.fixture
def gated_buyer(db, farm, owner):
    buyer = User.objects.create_user(
        "+639170000701", "701701", full_name="New Buyer", role=User.Role.CONSUMER
    )
    FarmPartnerLink.objects.create(
        farm=farm, partner=buyer, link_type=FarmPartnerLink.LinkType.CONSUMER, linked_by=owner
    )
    return buyer


BUYER_READS = ["partners:my-partner-farms", "partners:my-purchases", "partners:my-payments"]


@pytest.mark.django_db
class TestGatedBuyer:
    @pytest.mark.parametrize("url_name", BUYER_READS)
    def test_buyer_reads_refused(self, auth, gated_buyer, url_name):
        response = auth(gated_buyer).get(reverse(url_name))
        assert response.status_code == 403
        assert response.data["detail"] == GATE_MESSAGE

    def test_rotation_opens_the_same_requests(self, auth, gated_buyer):
        client = auth(gated_buyer)
        _rotate(client, "701701")

        for url_name in BUYER_READS:
            response = client.get(reverse(url_name))
            assert response.status_code == 200, (url_name, response.data)
