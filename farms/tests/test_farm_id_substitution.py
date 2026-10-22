# farms/tests/test_farm_id_substitution.py
"""
Farm ID substitution sweep (manuscript review, item A1).

Two farms, ALPHA and BRAVO, each fully populated by farms/tests/two_farms.py:
owner, manager, worker, houses, a live and a closed batch, a daily record,
weight sample, sale, payment, harvest, inventory items, stock-in, usage
log, routine task and completion, feed delivery, invitation and buyer link.

Every request is made as ALPHA's owner, the most privileged caller ALPHA
has, so the only thing between that caller and BRAVO's data is the farm
scoping itself. The URLconf is walked (checkngo/url_inventory.py, the same
walk test_rotation_gate.py does), so a farm-scoped view added later is
swept automatically, and a URL kwarg this file cannot resolve to a farm
object fails the sweep instead of being skipped.

Four ways to name the other farm's data:

  1. BRAVO's farm id in the URL. Every route, every method: 403.
  2. ALPHA's farm id with a BRAVO object id in the path (batch, sale,
     record, item, house, membership, invitation, buyer link, ...). 404 or
     403 for the methods the view implements, and 404/403/405 for the
     rest. Never 2xx.
  3. ALPHA's own URL with a BRAVO id in the request body: the client UUID
     of an offline record, or a foreign key (house, feed item, buyer link,
     the payment being reversed, the routine item). Refused with
     400/403/404, or a bulk-sync result that created and updated nothing.
     Each case has a control, the same body with ALPHA's own reference,
     which must succeed, so the refusal is shown to come from the BRAVO
     reference and not from a malformed body.
  4. ALPHA's own URL with BRAVO ids in the query string (?batch=, ?house=,
     ?item=, ...), every CSV and Excel export included. No BRAVO row comes
     back.

After every request BRAVO's rows are snapshotted and compared with the
snapshot taken before the sweep: nothing of BRAVO may change, appear,
disappear, or move to ALPHA.
"""
import io
import itertools
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from urllib.parse import urlencode

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APIClient

from checkngo.url_inventory import METHODS, api_routes
from farms.permissions import FarmScopedPermission
from farms.views import FarmReactivateView

from .two_farms import FAST_HASHERS, build_farm, snapshot, snapshot_diff

pytestmark = pytest.mark.django_db

FARM_ROUTES = [r for r in api_routes() if r.is_farm_scoped]
ROUTES_BY_NAME = {r.name: r for r in FARM_ROUTES}

# Farm-nested views that do not authorize by membership. FarmReactivateView
# looks the farm up by owner instead (a membership permission refuses every
# write to an archived farm, this one included), so another farm's id is a
# 404 there rather than a 403.
OWNER_LOOKUP_VIEWS = {"farms:farm-reactivate": FarmReactivateView}

# URL kwarg -> the FarmWorld attributes it can name. The first is the one
# used when the kwarg stays on ALPHA; every one is tried for BRAVO.
NAMED_KWARGS = {
    "farm_pk": ("farm",),
    "batch_pk": ("batch", "closed_batch"),
    "sale_pk": ("sale", "closed_sale"),
    "item_pk": ("item", "feed_item"),
}
# A bare <pk> is named by the path segment in front of it.
PK_BY_SEGMENT = {
    "farms": ("farm",),
    "invitations": ("invitation",),
    "members": ("worker_membership", "manager_membership"),
    "houses": ("house", "house2"),
    "batches": ("batch", "closed_batch"),
    "daily-records": ("daily_record",),
    "weights": ("weight_sample",),
    "sales": ("sale", "closed_sale"),
    "items": ("item", "feed_item"),
    "usage": ("usage_log",),
    "templates": ("template",),
    "completions": ("completion",),
    "partners": ("buyer_link",),
}


def _attrs_for(route, kwarg):
    if kwarg in NAMED_KWARGS:
        return NAMED_KWARGS[kwarg]
    if kwarg == "pk":
        parts = route.route.strip("/").split("/")
        for i, part in enumerate(parts):
            if part in ("<int:pk>", "<uuid:pk>") and parts[i - 1] in PK_BY_SEGMENT:
                return PK_BY_SEGMENT[parts[i - 1]]
    raise LookupError(
        f"{route.route} ({route.name}): no farm object is mapped to URL kwarg "
        f"{kwarg!r}. Add it to NAMED_KWARGS or PK_BY_SEGMENT so the sweep covers it."
    )


def _farm_kwarg(route):
    return "farm_pk" if "farm_pk" in route.kwargs else "pk"


def _object_kwargs(route):
    return [k for k in route.kwargs if k != _farm_kwarg(route)]


def _kwargs(route, farm_world, objects_world):
    kwargs = {_farm_kwarg(route): farm_world.farm.pk}
    for kwarg in _object_kwargs(route):
        kwargs[kwarg] = getattr(objects_world, _attrs_for(route, kwarg)[0]).pk
    return kwargs


def _mixed_kwargs(route, alpha, bravo):
    """Every combination of ALPHA and BRAVO objects with at least one BRAVO."""
    choices = []
    for kwarg in _object_kwargs(route):
        attrs = _attrs_for(route, kwarg)
        options = [("ALPHA", kwarg, getattr(alpha, attrs[0]).pk)]
        options += [(f"BRAVO.{a}", kwarg, getattr(bravo, a).pk) for a in attrs]
        choices.append(options)
    for combo in itertools.product(*choices):
        if all(owner == "ALPHA" for owner, _, _ in combo):
            continue
        kwargs = {_farm_kwarg(route): alpha.farm.pk}
        kwargs.update({kwarg: pk for _, kwarg, pk in combo})
        label = ", ".join(f"{kwarg}={owner}" for owner, kwarg, _ in combo)
        yield kwargs, label


def _send(client, method, url, body=None, query=None):
    """
    One request, in its own savepoint. In production a statement that fails
    in a view not wrapped in atomic() just fails, under autocommit; inside
    the test's transaction it would poison every later query, the BRAVO
    snapshot included. atomic() rolls back to the savepoint when the failed
    statement marked the connection for rollback, which restores the
    production behaviour, so the snapshot still runs.
    """
    if query:
        url = f"{url}?{urlencode(query)}"
    with transaction.atomic():
        if method in ("GET", "DELETE"):
            return getattr(client, method.lower())(url)
        return getattr(client, method.lower())(url, body or {}, format="json")


def _body_text(response):
    """Response body as text, CSV streams and Excel workbooks included."""
    # A streamed CSV can be read only once; keep the text on the response.
    if hasattr(response, "_sweep_text"):
        return response._sweep_text
    if getattr(response, "streaming", False):
        content = b"".join(response.streaming_content)
    else:
        content = response.content
    if "spreadsheetml" in response.get("Content-Type", ""):
        workbook = load_workbook(io.BytesIO(content), read_only=True)
        text = "\n".join(
            str(cell)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows(values_only=True)
            for cell in row
            if cell is not None
        )
    else:
        text = content.decode("utf-8", errors="replace")
    response._sweep_text = text
    return text


def _leaks(response, world, allowed=()):
    text = _body_text(response)
    return [m for m in world.markers if m in text and m not in allowed]


def _today():
    return timezone.localdate()


# ── request bodies ───────────────────────────────────────────────────────
# A minimal body that would be VALID for a write against `w`'s own objects.
# Case 1 sends BRAVO's version (so a missing permission check would act on
# BRAVO); case 2 sends ALPHA's (so a missing object lookup would). Routes
# without an entry get {}.


def _daily(w, **over):
    body = {
        "id": str(uuid.uuid4()),
        "record_date": _today().isoformat(),
        "mortality_disease": 1,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_sacks": "1.00",
        "feed_item": w.feed_item.pk,
        "notes": "sweep",
    }
    return body | over


def _weight(w, **over):
    body = {
        "id": str(uuid.uuid4()),
        "sample_date": _today().isoformat(),
        "birds_weighed": 10,
        "average_grams": "900.00",
    }
    return body | over


def _sale(w, **over):
    body = {
        "id": str(uuid.uuid4()),
        "sale_date": _today().isoformat(),
        "sale_type": "LIVE",
        "bird_count": 5,
        "total_weight_kg": "10.00",
        "price_per_kg": "150.00",
        "buyer_link": w.buyer_link.pk,
    }
    return body | over


def _usage(w, **over):
    body = {
        "id": str(uuid.uuid4()),
        "item": w.item.pk,
        "quantity_used": "1.00",
        "usage_date": _today().isoformat(),
    }
    return body | over


def _completion(w, **over):
    body = {
        "id": str(uuid.uuid4()),
        "template": w.template.pk,
        "house": w.house.pk,
        "completion_date": _today().isoformat(),
    }
    return body | over


def _feed_delivery(w, **over):
    body = {
        "inventory_item": w.feed_item.pk,
        "delivery_date": _today().isoformat(),
        "feed_type": "STARTER",
        "quantity_kg": "100.00",
        "unit_cost": "30.00",
    }
    return body | over


def _payment(w, **over):
    body = {
        "amount": "100.00",
        "received_date": _today().isoformat(),
        "buyer_link": w.buyer_link.pk,
    }
    return body | over


WRITE_BODIES = {
    ("farms:farm-detail", "PATCH"): lambda w: {"name": "Sweep renamed"},
    ("farms:farm-detail", "PUT"): lambda w: {"name": "Sweep renamed"},
    ("farms:farm-invitations", "POST"): lambda w: {
        "phone_number": "+639179990001",
        "full_name": "Sweep Invitee",
        "account_role": "WORKER",
        "membership_role": "WORKER",
    },
    ("farms:member-houses", "PATCH"): lambda w: {"houses": [w.house2.pk]},
    ("farms:ownership-transfer", "POST"): lambda w: {
        "new_owner_phone": w.manager.phone_number,
        "confirm_transfer": True,
    },
    ("production:house-list", "POST"): lambda w: {"name": "Sweep House", "capacity": 100},
    ("production:house-detail", "PATCH"): lambda w: {"capacity": 123},
    ("production:house-detail", "PUT"): lambda w: {"name": "Sweep House", "capacity": 123},
    ("production:batch-list", "POST"): lambda w: {
        "house": w.house2.pk,
        "batch_code": "SWEEP-1",
        "initial_bird_count": 100,
        "start_date": (_today() - timedelta(days=1)).isoformat(),
    },
    ("production:batch-terminate", "POST"): lambda w: {"reason": "Sweep test termination"},
    ("production:daily-list", "POST"): lambda w: _daily(w),
    ("production:daily-bulk-sync", "POST"): lambda w: {"records": [_daily(w)]},
    ("production:daily-detail", "PATCH"): lambda w: {"notes": "sweep edit"},
    ("production:daily-detail", "PUT"): lambda w: _daily(w, record_date=w.daily_record.record_date.isoformat()),
    ("production:daily-correct", "POST"): lambda w: {
        "mortality_heat": 1,
        "reason": "Recounted the losses on this day",
    },
    ("production:weight-list", "POST"): lambda w: _weight(w),
    ("production:weight-detail", "PATCH"): lambda w: {"notes": "sweep edit"},
    ("production:weight-detail", "PUT"): lambda w: _weight(w, sample_date=w.weight_sample.sample_date.isoformat()),
    ("production:sale-list", "POST"): lambda w: _sale(w),
    ("production:sale-bulk-sync", "POST"): lambda w: {"records": [_sale(w)]},
    ("production:sale-detail", "PATCH"): lambda w: {"notes": "sweep edit"},
    ("production:sale-detail", "PUT"): lambda w: _sale(w, sale_date=w.sale.sale_date.isoformat()),
    ("production:payment-list", "POST"): lambda w: _payment(w),
    ("production:batch-close", "POST"): lambda w: {"buyer_link": w.buyer_link.pk},
    ("production:feed-list", "POST"): lambda w: _feed_delivery(w),
    ("production:feed-with-stock", "POST"): lambda w: _feed_delivery(w),
    ("production:inventory-item-list", "POST"): lambda w: {"name": "Sweep item", "unit": "dose"},
    ("production:inventory-item-detail", "PATCH"): lambda w: {"low_stock_threshold": "3.00"},
    ("production:inventory-item-detail", "PUT"): lambda w: {"name": "Sweep item", "unit": "dose"},
    ("production:inventory-stock-in-list", "POST"): lambda w: {
        "quantity": "10.00",
        "stock_in_date": _today().isoformat(),
    },
    ("production:inventory-usage-list", "POST"): lambda w: _usage(w),
    ("production:inventory-usage-bulk-sync", "POST"): lambda w: {"records": [_usage(w)]},
    ("production:inventory-usage-detail", "PATCH"): lambda w: {"notes": "sweep edit"},
    ("production:inventory-usage-detail", "PUT"): lambda w: _usage(w, usage_date=w.usage_log.usage_date.isoformat()),
    ("production:task-template-list", "POST"): lambda w: {"name": "Sweep task"},
    ("production:task-template-detail", "PATCH"): lambda w: {"suggested_time": "7:00 AM"},
    ("production:task-template-detail", "PUT"): lambda w: {"name": "Sweep task"},
    ("production:task-completion-list", "POST"): lambda w: _completion(w),
    ("production:task-completion-bulk-sync", "POST"): lambda w: {"records": [_completion(w)]},
    ("partners:farm-partners", "POST"): lambda w: {
        "phone_number": "+639179990002",
        "full_name": "Sweep Buyer",
        "account_role": "CONSUMER",
    },
}


def _write_body(route, method, world):
    builder = WRITE_BODIES.get((route.name, method))
    return builder(world) if builder else {}


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def alpha(db):
    return build_farm("ALPHA", "+639171000")


@pytest.fixture
def bravo(db):
    return build_farm("BRAVO", "+639172000")


@pytest.fixture
def as_alpha(alpha):
    client = APIClient()
    client.force_authenticate(user=alpha.owner)
    # An unhandled server error is a finding to report alongside the rest
    # (did BRAVO change? did anything leak?), not an exception that stops
    # the test before those checks run.
    client.raise_request_exception = False
    return client


def _short(response):
    return _body_text(response)[:200].replace("\n", " ")


# ── the sweep covers what it claims to ───────────────────────────────────


class TestSweepCoverage:
    def test_walk_reaches_the_farm_scoped_routes(self):
        names = set(ROUTES_BY_NAME)
        assert {
            "farms:farm-detail",
            "farms:member-houses",
            "production:daily-bulk-sync",
            "production:payment-list",
            "production:task-completion-detail",
            "analytics:report-sales-history",
            "analytics:report-sales-history-xlsx",
            "partners:partner-restore",
        } <= names
        assert len(FARM_ROUTES) >= 70

    def test_every_url_kwarg_maps_to_a_farm_object(self):
        problems = []
        for route in FARM_ROUTES:
            for kwarg in _object_kwargs(route):
                try:
                    _attrs_for(route, kwarg)
                except LookupError as exc:
                    problems.append(str(exc))
        assert not problems, "\n".join(problems)

    def test_every_route_is_membership_scoped_or_listed(self):
        problems = []
        for route in FARM_ROUTES:
            scoped = any(
                isinstance(p, type) and issubclass(p, FarmScopedPermission)
                for p in route.permission_classes
            )
            if route.name in OWNER_LOOKUP_VIEWS:
                if route.view_class is not OWNER_LOOKUP_VIEWS[route.name] or scoped:
                    problems.append(f"{route.name}: OWNER_LOOKUP_VIEWS entry is stale")
            elif not scoped:
                problems.append(
                    f"{route.route} ({route.name}): not membership-scoped and not "
                    "listed in OWNER_LOOKUP_VIEWS"
                )
        assert not problems, "\n".join(problems)

    def test_every_body_substitution_names_a_real_write(self):
        for sub in SUBSTITUTIONS:
            route = ROUTES_BY_NAME[sub.route]
            assert sub.method in route.methods, sub.id


# ── 1. the other farm's id in the URL ────────────────────────────────────


def _expected_foreign_farm_status(route, method):
    if route.name in OWNER_LOOKUP_VIEWS:
        return 404 if method in route.methods else 405
    return 403


@pytest.mark.parametrize("route", FARM_ROUTES, ids=lambda r: r.name)
def test_other_farms_id_in_the_url_is_refused(route, alpha, bravo, as_alpha):
    reference = snapshot(bravo)
    problems = []
    for objects_world in (bravo, alpha):
        url = reverse(route.name, kwargs=_kwargs(route, bravo, objects_world))
        for method in METHODS:
            response = _send(as_alpha, method, url, _write_body(route, method, bravo))
            expected = _expected_foreign_farm_status(route, method)
            where = f"{method} {url} (objects from {objects_world.label})"
            if response.status_code != expected:
                problems.append(f"{where}: {response.status_code}, expected {expected}: {_short(response)}")
            leaked = _leaks(response, bravo)
            if leaked:
                problems.append(f"{where}: response contains BRAVO data {leaked}")
            changed = snapshot_diff(reference, snapshot(bravo))
            if changed:
                problems.append(f"{where}: changed BRAVO rows in {changed}")
                reference = snapshot(bravo)
    assert not problems, "\n".join(problems)


# ── 2. the other farm's object id under this farm's URL ──────────────────

OBJECT_ROUTES = [r for r in FARM_ROUTES if _object_kwargs(r)]


@pytest.mark.parametrize("route", OBJECT_ROUTES, ids=lambda r: r.name)
def test_other_farms_object_id_under_own_farm_is_refused(route, alpha, bravo, as_alpha):
    reference = snapshot(bravo)
    problems = []
    for kwargs, label in _mixed_kwargs(route, alpha, bravo):
        url = reverse(route.name, kwargs=kwargs)
        for method in METHODS:
            response = _send(as_alpha, method, url, _write_body(route, method, alpha))
            allowed = {403, 404} if method in route.methods else {403, 404, 405}
            where = f"{method} {url} ({label})"
            if response.status_code not in allowed:
                problems.append(
                    f"{where}: {response.status_code}, expected one of {sorted(allowed)}: "
                    f"{_short(response)}"
                )
            leaked = _leaks(response, bravo)
            if leaked:
                problems.append(f"{where}: response contains BRAVO data {leaked}")
            changed = snapshot_diff(reference, snapshot(bravo))
            if changed:
                problems.append(f"{where}: changed BRAVO rows in {changed}")
                reference = snapshot(bravo)
    assert not problems, "\n".join(problems)


# ── 3. the other farm's id in the request body ───────────────────────────


@dataclass(frozen=True)
class Substitution:
    id: str
    route: str
    method: str
    url_objects: dict
    # (alpha, ref) -> body. `ref` is BRAVO in the attack and ALPHA in the
    # control, so the two bodies differ only in whose object they name.
    body: Callable

    @property
    def is_bulk(self):
        return self.route.endswith("bulk-sync")


def _new_or(ref, alpha, attr):
    """BRAVO's object id in the attack; a fresh client UUID in the control."""
    return str(uuid.uuid4()) if ref is alpha else str(getattr(ref, attr).pk)


_BATCH = {"batch_pk": "batch"}

SUBSTITUTIONS = [
    Substitution("member-houses.houses", "farms:member-houses", "PATCH",
                 {"pk": "worker_membership"}, lambda a, r: {"houses": [r.house2.pk]}),
    Substitution("batch-create.house", "production:batch-list", "POST",
                 {}, lambda a, r: WRITE_BODIES[("production:batch-list", "POST")](a) | {"house": r.house2.pk}),
    Substitution("daily-create.id", "production:daily-list", "POST",
                 _BATCH, lambda a, r: _daily(a, id=_new_or(r, a, "daily_record"))),
    Substitution("daily-create.feed_item", "production:daily-list", "POST",
                 _BATCH, lambda a, r: _daily(a, feed_item=r.feed_item.pk)),
    Substitution("daily-bulk.id", "production:daily-bulk-sync", "POST",
                 _BATCH, lambda a, r: {"records": [_daily(a, id=_new_or(r, a, "daily_record"))]}),
    Substitution("daily-bulk.feed_item", "production:daily-bulk-sync", "POST",
                 _BATCH, lambda a, r: {"records": [_daily(a, feed_item=r.feed_item.pk)]}),
    Substitution("daily-edit.id", "production:daily-detail", "PATCH",
                 {"batch_pk": "batch", "pk": "daily_record"},
                 lambda a, r: {"id": str(r.daily_record.pk), "notes": "sweep edit"}),
    Substitution("daily-edit.feed_item", "production:daily-detail", "PATCH",
                 {"batch_pk": "batch", "pk": "daily_record"},
                 lambda a, r: {"feed_item": r.feed_item.pk, "feed_sacks": "3.00"}),
    Substitution("daily-correct.feed_item", "production:daily-correct", "POST",
                 {"batch_pk": "batch", "pk": "daily_record"},
                 lambda a, r: {"feed_item": r.feed_item.pk, "feed_sacks": "3.00",
                               "reason": "Recounted the sacks used that day"}),
    Substitution("weight-create.id", "production:weight-list", "POST",
                 _BATCH, lambda a, r: _weight(a, id=_new_or(r, a, "weight_sample"))),
    Substitution("weight-edit.id", "production:weight-detail", "PATCH",
                 {"batch_pk": "batch", "pk": "weight_sample"},
                 lambda a, r: {"id": str(r.weight_sample.pk), "notes": "sweep edit"}),
    Substitution("sale-create.id", "production:sale-list", "POST",
                 _BATCH, lambda a, r: _sale(a, id=_new_or(r, a, "sale"))),
    Substitution("sale-create.buyer_link", "production:sale-list", "POST",
                 _BATCH, lambda a, r: _sale(a, buyer_link=r.buyer_link.pk)),
    Substitution("sale-bulk.id", "production:sale-bulk-sync", "POST",
                 _BATCH, lambda a, r: {"records": [_sale(a, id=_new_or(r, a, "sale"))]}),
    Substitution("sale-bulk.buyer_link", "production:sale-bulk-sync", "POST",
                 _BATCH, lambda a, r: {"records": [_sale(a, buyer_link=r.buyer_link.pk)]}),
    Substitution("sale-edit.id", "production:sale-detail", "PATCH",
                 {"batch_pk": "batch", "pk": "sale"},
                 lambda a, r: {"id": str(r.sale.pk), "notes": "sweep edit"}),
    Substitution("sale-edit.buyer_link", "production:sale-detail", "PATCH",
                 {"batch_pk": "batch", "pk": "sale"},
                 lambda a, r: {"buyer_link": r.buyer_link.pk}),
    Substitution("payment.buyer_link", "production:payment-list", "POST",
                 {"batch_pk": "batch", "sale_pk": "sale"},
                 lambda a, r: _payment(a, buyer_link=r.buyer_link.pk)),
    Substitution("payment.reverses", "production:payment-list", "POST",
                 {"batch_pk": "batch", "sale_pk": "sale"},
                 lambda a, r: {"amount": str(-r.payment.amount),
                               "received_date": _today().isoformat(),
                               "reverses": r.payment.pk}),
    Substitution("batch-close.buyer_link", "production:batch-close", "POST",
                 _BATCH, lambda a, r: {"buyer_link": r.buyer_link.pk}),
    Substitution("feed-delivery.inventory_item", "production:feed-list", "POST",
                 {}, lambda a, r: _feed_delivery(a, inventory_item=r.feed_item.pk)),
    Substitution("feed-with-stock.inventory_item", "production:feed-with-stock", "POST",
                 {}, lambda a, r: _feed_delivery(a, inventory_item=r.feed_item.pk)),
    Substitution("usage-create.id", "production:inventory-usage-list", "POST",
                 {}, lambda a, r: _usage(a, id=_new_or(r, a, "usage_log"))),
    Substitution("usage-create.item", "production:inventory-usage-list", "POST",
                 {}, lambda a, r: _usage(a, item=r.item.pk)),
    Substitution("usage-bulk.id", "production:inventory-usage-bulk-sync", "POST",
                 {}, lambda a, r: {"records": [_usage(a, id=_new_or(r, a, "usage_log"))]}),
    Substitution("usage-bulk.item", "production:inventory-usage-bulk-sync", "POST",
                 {}, lambda a, r: {"records": [_usage(a, item=r.item.pk)]}),
    Substitution("usage-edit.id", "production:inventory-usage-detail", "PATCH",
                 {"pk": "usage_log"},
                 lambda a, r: {"id": str(r.usage_log.pk), "notes": "sweep edit"}),
    Substitution("completion-create.id", "production:task-completion-list", "POST",
                 {}, lambda a, r: _completion(a, id=_new_or(r, a, "completion"))),
    Substitution("completion-create.template", "production:task-completion-list", "POST",
                 {}, lambda a, r: _completion(a, template=r.template.pk)),
    Substitution("completion-create.house", "production:task-completion-list", "POST",
                 {}, lambda a, r: _completion(a, house=r.house.pk)),
    Substitution("completion-bulk.id", "production:task-completion-bulk-sync", "POST",
                 {}, lambda a, r: {"records": [_completion(a, id=_new_or(r, a, "completion"))]}),
    Substitution("completion-bulk.template", "production:task-completion-bulk-sync", "POST",
                 {}, lambda a, r: {"records": [_completion(a, template=r.template.pk)]}),
    Substitution("completion-bulk.house", "production:task-completion-bulk-sync", "POST",
                 {}, lambda a, r: {"records": [_completion(a, house=r.house.pk)]}),
]


def _substitution_url(sub, alpha):
    kwargs = {"farm_pk": alpha.farm.pk}
    kwargs.update({k: getattr(alpha, attr).pk for k, attr in sub.url_objects.items()})
    return reverse(sub.route, kwargs=kwargs)


def _supplied_ids(body):
    records = body.get("records", [body])
    return {str(r["id"]) for r in records if "id" in r}


@pytest.mark.parametrize("sub", SUBSTITUTIONS, ids=lambda s: s.id)
def test_other_farms_id_in_the_body_is_refused(sub, alpha, bravo, as_alpha):
    reference = snapshot(bravo)
    body = sub.body(alpha, bravo)
    response = _send(as_alpha, sub.method, _substitution_url(sub, alpha), body)

    problems = []
    status = response.status_code
    if sub.is_bulk and status in (200, 207):
        outcome = response.data
        refused = not outcome["created"] and not outcome["updated"] and bool(outcome["failed"])
    else:
        refused = status in (400, 403, 404)
    if not refused:
        problems.append(f"not refused: {status} {_short(response)}")

    changed = snapshot_diff(reference, snapshot(bravo))
    if changed:
        problems.append(f"BRAVO rows changed in {changed}")

    # The caller supplied the client UUID itself; echoing it back in failed[]
    # tells them nothing new. Anything else of BRAVO's would be a leak.
    leaked = _leaks(response, bravo, allowed=_supplied_ids(body))
    if leaked:
        problems.append(f"response contains BRAVO data {leaked}: {_short(response)}")

    assert not problems, f"{sub.method} {sub.route} with a BRAVO id: " + "; ".join(problems)


@pytest.mark.parametrize("sub", SUBSTITUTIONS, ids=lambda s: s.id)
def test_the_same_body_with_own_ids_succeeds(sub, alpha, bravo, as_alpha):
    """Control: the refusal above is caused by the BRAVO id, not by the body."""
    response = _send(as_alpha, sub.method, _substitution_url(sub, alpha), sub.body(alpha, alpha))
    assert response.status_code in (200, 201), _short(response)
    if sub.is_bulk:
        assert not response.data["failed"], response.data
        assert response.data["created"] or response.data["updated"]


# ── 4. the other farm's ids in the query string, exports included ────────

GET_ROUTES = [r for r in FARM_ROUTES if "GET" in r.methods]
REPORT_ROUTES = {r.name for r in GET_ROUTES if r.name.startswith("analytics:report-")}


def _foreign_query(bravo):
    return {
        "batch": bravo.batch.pk,
        "house": bravo.house.pk,
        "item": bravo.item.pk,
        "template": bravo.template.pk,
        "sale": bravo.sale.pk,
        "farm": bravo.farm.pk,
    }


@pytest.mark.parametrize("route", GET_ROUTES, ids=lambda r: r.name)
def test_other_farms_ids_in_the_query_string_return_nothing_of_theirs(
    route, alpha, bravo, as_alpha
):
    reference = snapshot(bravo)
    url = reverse(route.name, kwargs=_kwargs(route, alpha, alpha))

    plain = _send(as_alpha, "GET", url)
    filtered = _send(as_alpha, "GET", url, query=_foreign_query(bravo))

    for label, response in (("plain", plain), ("BRAVO query", filtered)):
        assert response.status_code < 500, f"{label}: {response.status_code}"
        assert not _leaks(response, bravo), f"{label}: {_leaks(response, bravo)}"
    if route.name in REPORT_ROUTES:
        # Proves the export was actually read: ALPHA's own rows are there.
        assert plain.status_code == 200
        assert alpha.label in _body_text(plain)
    assert not snapshot_diff(reference, snapshot(bravo))


def test_farm_list_names_only_own_farms(alpha, bravo, as_alpha):
    response = as_alpha.get(reverse("farms:farm-list"))
    assert response.status_code == 200
    assert {row["id"] for row in response.data} == {alpha.farm.pk}
    assert not _leaks(response, bravo)
