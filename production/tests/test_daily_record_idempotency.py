# production/tests/test_daily_record_idempotency.py
"""
Regression tests for the client-generated-id bug.

DailyRecordSerializer (and WeightSampleSerializer) declared
extra_kwargs = {"id": {"required": False}}, but DRF's ModelSerializer
forces a primary-key field to read_only=True regardless of that override.
The client's UUID was silently discarded and the server generated its
own — which broke the offline design's whole idempotency guarantee: a
synced-then-retried record was supposed to update in place, keyed on the
id the device chose before it ever reached the server.

The (batch, record_date) unique constraint was masking this in the common
case — a naive retry still failed, just on the wrong constraint (the date,
not the id), with an error that read like a duplicate day rather than a
duplicate id.

Covered here, on both entry points a client can create a record through:
  - single POST  (DailyRecordListCreateView)
  - bulk sync    (DailyRecordBulkSyncSerializer, via DailyRecordBulkSyncView)

Design decision, made explicit because the task asked for it: two
DIFFERENT ids on the SAME (batch, record_date) are a genuine conflict, not
a record to merge. "One entry per batch per day" is a business rule, not
just an idempotency mechanism — a second id claiming the same day almost
always means either a stale/duplicate outbox entry or two different
devices both trying to record the same day, and either way the existing
day's data should not be silently overwritten by an unrelated id outside
the app's own audit-logged correction flow. Both entry points reject this
with a clear message rather than a raw IntegrityError.
"""
import uuid
from datetime import date

import pytest

from production.models import Batch, DailyRecord, House, WeightSample

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Test House", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="ID-TEST-001",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


def _payload(record_date, **overrides):
    body = {
        "record_date": record_date,
        "mortality_disease": 1,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": "10.00",
        "notes": "",
    }
    body.update(overrides)
    return body


class TestSinglePostIdempotency:
    """POST /farms/<id>/batches/<id>/daily-records/"""

    def url(self, farm, batch):
        return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/"

    def test_supplied_id_is_stored_exactly(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        response = auth(owner).post(
            self.url(farm, batch),
            _payload("2026-01-05", id=rid),
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["id"] == rid
        assert DailyRecord.objects.filter(pk=rid).exists()

    def test_posting_same_id_twice_updates_not_duplicates(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        first = auth(owner).post(
            self.url(farm, batch), _payload("2026-01-05", id=rid), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            self.url(farm, batch), _payload("2026-01-05", id=rid), format="json"
        )
        assert second.status_code == 200, second.data
        assert second.data["id"] == rid
        assert DailyRecord.objects.filter(batch=batch).count() == 1

    def test_posting_same_id_with_different_values_updates_them(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        auth(owner).post(
            self.url(farm, batch),
            _payload("2026-01-05", id=rid, mortality_disease=1, feed_kg="10.00"),
            format="json",
        )

        response = auth(owner).post(
            self.url(farm, batch),
            _payload("2026-01-05", id=rid, mortality_disease=7, feed_kg="25.50"),
            format="json",
        )

        assert response.status_code == 200, response.data
        record = DailyRecord.objects.get(pk=rid)
        assert record.mortality_disease == 7
        assert str(record.feed_kg) == "25.50"

    def test_posting_without_id_still_works_and_generates_one(self, auth, owner, farm, batch):
        response = auth(owner).post(
            self.url(farm, batch), _payload("2026-01-05"), format="json"
        )
        assert response.status_code == 201, response.data
        assert response.data["id"]  # server generated a UUID
        assert DailyRecord.objects.filter(batch=batch).count() == 1

    def test_different_id_same_date_is_rejected_not_merged(self, auth, owner, farm, batch):
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())

        first = auth(owner).post(
            self.url(farm, batch), _payload("2026-01-05", id=first_id), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            self.url(farm, batch), _payload("2026-01-05", id=second_id), format="json"
        )

        assert second.status_code == 400
        assert "record_date" in second.data
        # The first record's data is untouched by the rejected second id.
        assert DailyRecord.objects.filter(batch=batch).count() == 1
        assert DailyRecord.objects.get(pk=first_id).mortality_disease == 1

    def test_malformed_id_is_a_clean_validation_error_not_a_crash(self, auth, owner, farm, batch):
        response = auth(owner).post(
            self.url(farm, batch),
            _payload("2026-01-05", id="not-a-uuid"),
            format="json",
        )
        assert response.status_code == 400
        assert "id" in response.data


class TestBulkSyncIdempotency:
    """POST /farms/<id>/batches/<id>/daily-records/bulk-sync/"""

    def url(self, farm, batch):
        return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/bulk-sync/"

    def _sync(self, api_client, farm, batch, records):
        return api_client.post(
            self.url(farm, batch), {"records": records}, format="json"
        )

    def test_supplied_id_is_stored_exactly(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        response = self._sync(
            auth(owner), farm, batch, [_payload("2026-01-05", id=rid)]
        )
        assert response.status_code == 200, response.data
        assert response.data["created"] == [rid]
        assert DailyRecord.objects.filter(pk=rid).exists()

    def test_syncing_same_id_twice_updates_not_duplicates(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        self._sync(auth(owner), farm, batch, [_payload("2026-01-05", id=rid)])

        response = self._sync(
            auth(owner), farm, batch, [_payload("2026-01-05", id=rid)]
        )

        assert response.data["failed"] == []
        assert response.data["updated"] == [rid]
        assert response.data["created"] == []
        assert DailyRecord.objects.filter(batch=batch).count() == 1

    def test_syncing_same_id_with_different_values_updates_them(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        self._sync(
            auth(owner), farm, batch,
            [_payload("2026-01-05", id=rid, mortality_disease=1, feed_kg="10.00")],
        )

        self._sync(
            auth(owner), farm, batch,
            [_payload("2026-01-05", id=rid, mortality_disease=9, feed_kg="40.00")],
        )

        record = DailyRecord.objects.get(pk=rid)
        assert record.mortality_disease == 9
        assert str(record.feed_kg) == "40.00"

    def test_syncing_without_id_still_works_and_generates_one(self, auth, owner, farm, batch):
        response = self._sync(auth(owner), farm, batch, [_payload("2026-01-05")])

        assert response.status_code == 200, response.data
        assert response.data["failed"] == []
        assert len(response.data["created"]) == 1
        record = DailyRecord.objects.get(pk=response.data["created"][0])
        assert record.record_date == date(2026, 1, 5)

    def test_different_id_same_date_is_rejected_not_merged(self, auth, owner, farm, batch):
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())

        self._sync(auth(owner), farm, batch, [_payload("2026-01-05", id=first_id)])
        response = self._sync(
            auth(owner), farm, batch, [_payload("2026-01-05", id=second_id)]
        )

        assert response.data["created"] == []
        assert response.data["updated"] == []
        assert len(response.data["failed"]) == 1
        assert "different id" in response.data["failed"][0]["error"]
        # The first record's data is untouched by the rejected second id.
        assert DailyRecord.objects.filter(batch=batch).count() == 1
        assert DailyRecord.objects.get(pk=first_id).mortality_disease == 1

    def test_two_different_dates_two_different_ids_both_succeed(self, auth, owner, farm, batch):
        """Sanity check: the id-first lookup doesn't over-reject unrelated records."""
        response = self._sync(
            auth(owner), farm, batch,
            [
                _payload("2026-01-05", id=str(uuid.uuid4())),
                _payload("2026-01-06", id=str(uuid.uuid4())),
            ],
        )
        assert response.data["failed"] == []
        assert len(response.data["created"]) == 2


class TestWeightSampleIdField:
    """WeightSampleSerializer had the identical read-only-id bug."""

    def test_supplied_id_is_stored_exactly(self, auth, owner, farm, batch):
        rid = str(uuid.uuid4())
        response = auth(owner).post(
            f"/api/farms/{farm.id}/batches/{batch.id}/weights/",
            {
                "id": rid,
                "sample_date": "2026-01-05",
                "birds_weighed": 30,
                "average_grams": "450.00",
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["id"] == rid
        assert WeightSample.objects.filter(pk=rid).exists()
