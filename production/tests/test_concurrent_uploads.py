# production/tests/test_concurrent_uploads.py
"""
Concurrent duplicate uploads (manuscript review, item A3).

A phone that loses its connection mid-request retries; two phones on one
farm can queue the same day. These tests send the same upload from several
threads at once, each thread on its own database connection
(transactional_db, so every thread sees what the others commit).

Real concurrency is not deterministic: left alone, the first request often
finishes before the second starts, and the test passes for a reason that
proves nothing. So each test holds every thread at a barrier placed just
before the INSERT, after the existence checks the view and serializer run.
That reproduces the worst interleaving on purpose: every request has
already concluded "this id is new" when the first one writes. The gate
also counts its arrivals, and each test asserts all threads reached it,
so the race is shown to have happened.

What the offline-sync contract promises (OfflineSyncModel's docstring:
"a sync retry updates rather than duplicates"):

  same UUID, many uploads      → exactly one row, no 500, every response
                                 a success (201 created / 200 updated, or
                                 a bulk result listing the id as created
                                 or updated, never as failed);
  different UUIDs, same batch
  and record_date              → exactly one row; the other upload is
                                 refused as a date conflict (400 on the
                                 single POST, a failed[] entry in bulk
                                 sync), never a 500. The sequential form
                                 of this rule is already proven by
                                 test_daily_record_idempotency.py
                                 ::TestSinglePostIdempotency and
                                 ::TestBulkSyncIdempotency
                                 ::test_different_id_same_date_is_rejected_not_merged.
"""
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from farms.models import Farm
from farms.tests.two_farms import FAST_HASHERS, make_user
from production.models import Batch, DailyRecord, House, SaleEvent, WeightSample
from production.serializers import DailyRecordSerializer

pytestmark = pytest.mark.django_db(transaction=True)

THREADS = 5


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def owner():
    return make_user("+639174000001", "Concurrency Owner", User.Role.OWNER)


@pytest.fixture
def batch(owner):
    farm = Farm.objects.create(name="Concurrency Farm", owner=owner)
    house = House.objects.create(farm=farm, name="House 1", capacity=5000)
    return Batch.objects.create(
        house=house,
        batch_code="CC-1",
        initial_bird_count=1000,
        start_date=timezone.localdate() - timedelta(days=10),
        created_by=owner,
    )


def _url(name, batch):
    return reverse(name, kwargs={"farm_pk": batch.house.farm_id, "batch_pk": batch.pk})


def _daily(record_id, **over):
    body = {
        "id": str(record_id),
        "record_date": timezone.localdate().isoformat(),
        "mortality_disease": 2,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": "40.00",
    }
    return body | over


def _sale(sale_id):
    return {
        "id": str(sale_id),
        "sale_date": timezone.localdate().isoformat(),
        "sale_type": "LIVE",
        "bird_count": 25,
        "total_weight_kg": "50.00",
        "price_per_kg": "160.00",
    }


class _Gate:
    """A barrier that also counts how many threads reached it."""

    def __init__(self, parties):
        self._barrier = threading.Barrier(parties, timeout=10)
        self._lock = threading.Lock()
        self.arrived = 0

    def wait(self):
        with self._lock:
            self.arrived += 1
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            # A thread that took another path never arrives; let the rest go.
            pass


@contextmanager
def _held_before(target, attribute, parties=THREADS):
    """Hold every caller of target.attribute until `parties` have arrived."""
    gate = _Gate(parties)
    original = getattr(target, attribute)

    def gated(*args, **kwargs):
        gate.wait()
        return original(*args, **kwargs)

    with patch.object(target, attribute, gated):
        yield gate


def _post_concurrently(user, url, bodies):
    """POST each body from its own thread, all at once. Returns (status, data) per body."""
    results = [None] * len(bodies)

    def run(i):
        client = APIClient()
        client.raise_request_exception = False
        client.force_authenticate(user=user)
        try:
            response = client.post(url, bodies[i], format="json")
            results[i] = (response.status_code, getattr(response, "data", None))
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(bodies))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(r is not None for r in results), "a request thread did not finish"
    return results


def _statuses(results):
    return sorted(status for status, _ in results)


class TestSameUuidManyUploads:
    def test_daily_record_single_post(self, owner, batch):
        record_id = uuid.uuid4()
        # Held inside DailyRecordSerializer.create: past the view's "is this
        # id already here?" lookup and its same-date check, before INSERT.
        with _held_before(DailyRecordSerializer, "create") as gate:
            results = _post_concurrently(
                owner, _url("production:daily-list", batch), [_daily(record_id)] * THREADS
            )
        assert gate.arrived == THREADS

        assert DailyRecord.objects.filter(batch=batch).count() == 1
        assert DailyRecord.objects.filter(pk=record_id).count() == 1
        assert all(status in (200, 201) for status, _ in results), _statuses(results)

    def test_daily_record_bulk_sync(self, owner, batch):
        record_id = uuid.uuid4()
        with _held_before(DailyRecord.objects, "create") as gate:
            results = _post_concurrently(
                owner,
                _url("production:daily-bulk-sync", batch),
                [{"records": [_daily(record_id)]}] * THREADS,
            )
        assert gate.arrived == THREADS

        assert DailyRecord.objects.filter(batch=batch).count() == 1
        batch.refresh_from_db()
        assert batch.total_mortality == 2
        for status, data in results:
            assert status == 200, (status, data)
            assert data["created"] + data["updated"] == [str(record_id)], data

    def test_sale_event_bulk_sync(self, owner, batch):
        sale_id = uuid.uuid4()
        with _held_before(SaleEvent.objects, "create") as gate:
            results = _post_concurrently(
                owner,
                _url("production:sale-bulk-sync", batch),
                [{"records": [_sale(sale_id)]}] * THREADS,
            )
        assert gate.arrived == THREADS

        assert SaleEvent.objects.filter(batch=batch).count() == 1
        batch.refresh_from_db()
        assert batch.total_birds_sold == 25
        for status, data in results:
            assert status == 200, (status, data)
            assert data["created"] + data["updated"] == [str(sale_id)], data


class TestDifferentUuidsSameBatchAndDate:
    def test_single_post_one_lands_the_other_is_a_conflict(self, owner, batch):
        bodies = [_daily(uuid.uuid4()), _daily(uuid.uuid4(), mortality_disease=5)]
        with _held_before(DailyRecordSerializer, "create", parties=2) as gate:
            results = _post_concurrently(owner, _url("production:daily-list", batch), bodies)
        assert gate.arrived == 2

        assert DailyRecord.objects.filter(batch=batch).count() == 1
        assert _statuses(results) == [201, 400], results

    def test_bulk_sync_one_lands_the_other_fails_per_record(self, owner, batch):
        bodies = [
            {"records": [_daily(uuid.uuid4())]},
            {"records": [_daily(uuid.uuid4(), mortality_disease=5)]},
        ]
        with _held_before(DailyRecord.objects, "create", parties=2) as gate:
            results = _post_concurrently(owner, _url("production:daily-bulk-sync", batch), bodies)
        assert gate.arrived == 2

        assert DailyRecord.objects.filter(batch=batch).count() == 1
        assert _statuses(results) == [200, 207], results
        landed = [data for status, data in results if status == 200]
        refused = [data for status, data in results if status == 207]
        assert len(landed[0]["created"]) == 1
        assert refused[0]["created"] == [] and refused[0]["updated"] == []
        assert "already exists" in refused[0]["failed"][0]["error"]


class TestSequentialRetry:
    def test_weight_sample_retry_with_the_same_uuid_is_not_a_500(self, owner, batch):
        """
        WeightSample is an OfflineSyncModel like the others, with a client
        UUID, but its list view has no retry branch. The other single-POST
        paths are covered: daily records
        (test_daily_record_idempotency.py), sales (test_sale_events.py),
        usage logs (test_inventory.py), routine ticks (test_tasks.py).
        """
        client = APIClient()
        client.raise_request_exception = False
        client.force_authenticate(user=owner)
        url = _url("production:weight-list", batch)
        body = {
            "id": str(uuid.uuid4()),
            "sample_date": timezone.localdate().isoformat(),
            "birds_weighed": 10,
            "average_grams": "900.00",
        }
        first = client.post(url, body, format="json")
        assert first.status_code == 201, first.data
        retry = client.post(url, body | {"average_grams": "905.00"}, format="json")
        assert retry.status_code in (200, 201), retry.status_code
        assert WeightSample.objects.filter(batch=batch).count() == 1
