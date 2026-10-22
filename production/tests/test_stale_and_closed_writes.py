# production/tests/test_stale_and_closed_writes.py
"""
Stale or impossible edits (manuscript review, item A5).

Two rules, both server-side, both decided when the request ARRIVES:

  The 24-hour lock. A field record (OfflineSyncModel) is editable for 24
  hours after the server first received it (created_at, the server's
  clock). An edit that arrives later is refused with a 400 and not saved,
  however recently the phone queued it. Correcting it is then the
  manager's audited /correct/ endpoint.

  Closed and ended batches. Once a batch is HARVESTED (closed) or
  TERMINATED (ended) it accepts no new or changed field data.

Already proven elsewhere, so not repeated here:
  sales     test_sale_events.py::TestIdempotencyAndLock::test_a_sale_older_than_24_hours_cannot_be_edited,
            ::TestSaleBuyerLink::test_changing_buyer_link_past_24_hours_is_locked_same_as_any_other_edit,
            ::TestSaleBuyerLink::test_bulk_sync_cannot_change_buyer_link_on_a_locked_row
  usage     test_inventory.py::...::test_patch_after_24h_is_rejected,
            ::test_retried_post_after_24h_is_a_clean_400_not_a_500,
            ::test_bulk_sync_after_24h_reports_failed_with_id_not_500
  ticks     test_tasks.py::...::test_locked_completion_cannot_be_edited_or_deleted_by_worker
  closed    test_harvest.py::test_closed_batch_rejects_new_daily_records,
            test_harvest.py::test_double_close_is_refused,
            test_sale_events.py::TestClosing::test_a_closed_batch_refuses_new_sales

This file adds the daily record and the weight sample under the lock (no
test covered them), an edit that names a locked record by id from inside
an unlocked one, and every field write against a batch that is harvested
or terminated.

Payments are the one deliberate exception to the closed-batch rule: a
buyer pays after the birds have gone, so a payment against a closed
batch's sale is accepted. See test_payments.py
::test_fcr_and_feed_margin_are_unchanged_by_recording_a_payment.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from farms.models import Farm, FarmMembership
from farms.tests.two_farms import FAST_HASHERS, make_user
from production.models import (
    Batch,
    DailyRecord,
    Harvest,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    SaleEvent,
    WeightSample,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def world():
    today = timezone.localdate()
    owner = make_user("+639176000001", "Lock Owner", User.Role.OWNER)
    manager = make_user("+639176000002", "Lock Manager", User.Role.MANAGER)
    worker = make_user("+639176000003", "Lock Worker", User.Role.WORKER)
    farm = Farm.objects.create(name="Lock Farm", owner=owner)
    FarmMembership.objects.create(farm=farm, user=manager, role=FarmMembership.Role.MANAGER)
    FarmMembership.objects.create(farm=farm, user=worker, role=FarmMembership.Role.WORKER)
    house = House.objects.create(farm=farm, name="House 1", capacity=5000)
    batch = Batch.objects.create(
        house=house,
        batch_code="LOCK-1",
        initial_bird_count=1000,
        start_date=today - timedelta(days=20),
        created_by=owner,
    )
    return {"owner": owner, "manager": manager, "worker": worker, "farm": farm,
            "house": house, "batch": batch}


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    client.raise_request_exception = False
    return client


def _age_out(record):
    """Move the server-received time back past the 24-hour window."""
    type(record).objects.filter(pk=record.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    record.refresh_from_db()
    assert not record.is_editable


def _days_back(n):
    return (timezone.localdate() - timedelta(days=n)).isoformat()


def _url(name, world, **kwargs):
    return reverse(name, kwargs={"farm_pk": world["farm"].pk, **kwargs})


def _batch_url(name, world, **kwargs):
    return _url(name, world, batch_pk=world["batch"].pk, **kwargs)


def _daily(**over):
    body = {
        "id": str(uuid.uuid4()),
        "record_date": _days_back(2),
        "mortality_disease": 1,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": "20.00",
    }
    return body | over


def _row(record):
    return type(record).objects.filter(pk=record.pk).values().get()


def _changed_fields(before, record):
    after = _row(record)
    return sorted(k for k in before if before[k] != after[k])


# ── the 24-hour lock ─────────────────────────────────────────────────────


@pytest.fixture
def locked_record(world):
    """A daily record the worker synced, which has since passed the lock."""
    body = _daily()
    response = _client(world["worker"]).post(_batch_url("production:daily-list", world), body, format="json")
    assert response.status_code == 201, response.data
    record = DailyRecord.objects.get(pk=body["id"])
    _age_out(record)
    return record, body


class TestDailyRecordPastTheLock:
    @pytest.mark.parametrize("role", ["worker", "manager", "owner"])
    def test_patch_is_refused_and_nothing_saved(self, world, locked_record, role):
        record, _ = locked_record
        before = _row(record)
        response = _client(world[role]).patch(
            _batch_url("production:daily-detail", world, pk=record.pk),
            {"mortality_disease": 7},
            format="json",
        )
        assert response.status_code == 400
        assert "locked" in str(response.data).lower()
        assert _row(record) == before

    def test_retried_post_of_the_same_id_is_refused_and_nothing_saved(self, world, locked_record):
        record, body = locked_record
        before = _row(record)
        response = _client(world["worker"]).post(
            _batch_url("production:daily-list", world), body | {"mortality_disease": 7}, format="json"
        )
        assert response.status_code == 400
        assert "locked" in str(response.data).lower()
        assert _row(record) == before

    def test_bulk_sync_reports_it_failed_and_saves_nothing(self, world, locked_record):
        record, body = locked_record
        before = _row(record)
        response = _client(world["worker"]).post(
            _batch_url("production:daily-bulk-sync", world),
            {"records": [body | {"mortality_disease": 7}]},
            format="json",
        )
        assert response.status_code == 207
        assert response.data["updated"] == []
        assert response.data["failed"] == [
            {"record_date": body["record_date"], "error": "Locked — older than 24 hours."}
        ]
        assert _row(record) == before

    def test_the_rest_of_the_backlog_still_lands(self, world, locked_record):
        """Refused, never retried: the locked day fails alone."""
        record, body = locked_record
        fresh = _daily(record_date=_days_back(1))
        response = _client(world["worker"]).post(
            _batch_url("production:daily-bulk-sync", world),
            {"records": [body | {"mortality_disease": 7}, fresh]},
            format="json",
        )
        assert response.status_code == 207
        assert response.data["created"] == [fresh["id"]]
        assert len(response.data["failed"]) == 1

    def test_the_audited_correction_is_the_way_through(self, world, locked_record):
        record, _ = locked_record
        response = _client(world["manager"]).post(
            _batch_url("production:daily-correct", world, pk=record.pk),
            {"mortality_disease": 7, "reason": "Recounted the losses with the worker"},
            format="json",
        )
        assert response.status_code == 200, response.data
        record.refresh_from_db()
        assert record.mortality_disease == 7
        assert record.corrections_for_this().count() == 1


class TestWeightSamplePastTheLock:
    def test_patch_is_refused_and_nothing_saved(self, world):
        sample = WeightSample.objects.create(
            batch=world["batch"], sample_date=timezone.localdate() - timedelta(days=2),
            birds_weighed=10, average_grams=Decimal("700.00"), recorded_by=world["worker"],
        )
        _age_out(sample)
        before = _row(sample)
        response = _client(world["worker"]).patch(
            _batch_url("production:weight-detail", world, pk=sample.pk),
            {"average_grams": "999.00"},
            format="json",
        )
        assert response.status_code == 400
        assert "locked" in str(response.data).lower()
        assert _row(sample) == before


class TestNamingALockedRecordFromAnUnlockedOne:
    """
    PATCH an editable record with a body whose `id` is a locked record's id.
    The locked record must come out unchanged.
    """

    def test_sale(self, world):
        def sale(days_ago, notes):
            return SaleEvent.objects.create(
                batch=world["batch"], sale_date=timezone.localdate() - timedelta(days=days_ago),
                sale_type=SaleEvent.SaleType.LIVE, bird_count=10, total_weight_kg=Decimal("20.00"),
                price_per_kg=Decimal("150.00"), notes=notes, recorded_by=world["owner"],
            )

        locked = sale(3, "locked sale")
        _age_out(locked)
        editable = sale(1, "editable sale")
        before = _row(locked)

        _client(world["owner"]).patch(
            _batch_url("production:sale-detail", world, pk=editable.pk),
            {"id": str(locked.pk), "bird_count": 99, "notes": "overwritten"},
            format="json",
        )
        assert not _changed_fields(before, locked), (
            f"the locked sale was overwritten: {_changed_fields(before, locked)}"
        )

    def test_inventory_usage_log(self, world):
        item = InventoryItem.objects.create(farm=world["farm"], name="Vaccine", unit="dose")
        InventoryStockIn.objects.create(item=item, quantity=Decimal("100"), stock_in_date=timezone.localdate())

        def usage(notes):
            return InventoryUsageLog.objects.create(
                item=item, quantity_used=Decimal("2.00"),
                usage_date=timezone.localdate() - timedelta(days=1),
                notes=notes, recorded_by=world["worker"],
            )

        locked = usage("locked usage")
        _age_out(locked)
        editable = usage("editable usage")
        before = _row(locked)

        _client(world["worker"]).patch(
            _url("production:inventory-usage-detail", world, pk=editable.pk),
            {"id": str(locked.pk), "quantity_used": "50.00", "notes": "overwritten"},
            format="json",
        )
        assert not _changed_fields(before, locked), (
            f"the locked usage log was overwritten: {_changed_fields(before, locked)}"
        )


# ── closed and ended batches ─────────────────────────────────────────────


@pytest.fixture(params=["HARVESTED", "TERMINATED"])
def ended(request, world):
    """
    The batch with one record of each kind, all still inside their 24-hour
    window (so only the batch's status can be refusing them), then closed
    through the API or terminated through the API.
    """
    owner = _client(world["owner"])
    daily = _daily()
    assert owner.post(_batch_url("production:daily-list", world), daily, format="json").status_code == 201
    weight = {"id": str(uuid.uuid4()), "sample_date": _days_back(2), "birds_weighed": 10, "average_grams": "800.00"}
    assert owner.post(_batch_url("production:weight-list", world), weight, format="json").status_code == 201
    sale = {"id": str(uuid.uuid4()), "sale_date": _days_back(2), "sale_type": "LIVE",
            "bird_count": 100, "total_weight_kg": "200.00", "price_per_kg": "150.00"}
    assert owner.post(_batch_url("production:sale-list", world), sale, format="json").status_code == 201

    if request.param == "HARVESTED":
        response = owner.post(_batch_url("production:batch-close", world), {}, format="json")
        assert response.status_code == 201, response.data
    else:
        response = owner.post(
            _url("production:batch-terminate", world, pk=world["batch"].pk),
            {"reason": "Disease outbreak, flock culled"},
            format="json",
        )
        assert response.status_code == 200, response.data
    world["batch"].refresh_from_db()
    assert world["batch"].status == request.param
    return {"daily": daily, "weight": weight, "sale": sale}


def _batch_state(world):
    batch = world["batch"]
    return {
        "batch": Batch.objects.filter(pk=batch.pk).values().get(),
        "daily": list(DailyRecord.objects.filter(batch=batch).order_by("pk").values()),
        "weights": list(WeightSample.objects.filter(batch=batch).order_by("pk").values()),
        "sales": list(SaleEvent.objects.filter(batch=batch).order_by("pk").values()),
        "harvest": list(Harvest.objects.filter(batch=batch).values()),
    }


# (name, method, url builder, body builder) — every field write a batch takes.
ENDED_BATCH_WRITES = [
    ("new daily record", "post",
     lambda w, e: _batch_url("production:daily-list", w),
     lambda e: _daily(record_date=_days_back(1))),
    ("daily record bulk sync", "post",
     lambda w, e: _batch_url("production:daily-bulk-sync", w),
     lambda e: {"records": [_daily(record_date=_days_back(1))]}),
    ("edit an unlocked daily record", "patch",
     lambda w, e: _batch_url("production:daily-detail", w, pk=e["daily"]["id"]),
     lambda e: {"mortality_disease": 5}),
    ("correct a daily record", "post",
     lambda w, e: _batch_url("production:daily-correct", w, pk=e["daily"]["id"]),
     lambda e: {"mortality_disease": 5, "reason": "Recounted the losses with the worker"}),
    ("new weight sample", "post",
     lambda w, e: _batch_url("production:weight-list", w),
     lambda e: {"id": str(uuid.uuid4()), "sample_date": _days_back(1), "birds_weighed": 10, "average_grams": "900.00"}),
    ("edit an unlocked weight sample", "patch",
     lambda w, e: _batch_url("production:weight-detail", w, pk=e["weight"]["id"]),
     lambda e: {"average_grams": "950.00"}),
    ("new sale", "post",
     lambda w, e: _batch_url("production:sale-list", w),
     lambda e: {"id": str(uuid.uuid4()), "sale_date": _days_back(1), "sale_type": "LIVE",
                "bird_count": 5, "total_weight_kg": "10.00", "price_per_kg": "150.00"}),
    ("sale bulk sync", "post",
     lambda w, e: _batch_url("production:sale-bulk-sync", w),
     lambda e: {"records": [{"id": str(uuid.uuid4()), "sale_date": _days_back(1), "sale_type": "LIVE",
                             "bird_count": 5, "total_weight_kg": "10.00", "price_per_kg": "150.00"}]}),
    ("edit an unlocked sale", "patch",
     lambda w, e: _batch_url("production:sale-detail", w, pk=e["sale"]["id"]),
     lambda e: {"bird_count": 50}),
    ("close it", "post",
     lambda w, e: _batch_url("production:batch-close", w),
     lambda e: {}),
    ("terminate it", "post",
     lambda w, e: _url("production:batch-terminate", w, pk=w["batch"].pk),
     lambda e: {"reason": "Terminating an ended batch"}),
]


@pytest.mark.parametrize("write", ENDED_BATCH_WRITES, ids=lambda w: w[0])
def test_an_ended_batch_refuses_every_field_write(world, ended, write):
    _, method, url, body = write
    before = _batch_state(world)
    response = getattr(_client(world["owner"]), method)(url(world, ended), body(ended), format="json")
    assert response.status_code in (400, 404), (response.status_code, getattr(response, "data", None))
    assert _batch_state(world) == before
