# production/tests/test_house_scoping.py
"""
House-level write scoping on the production write paths.

A worker restricted to certain houses may still READ any batch on their
farm, but may only WRITE against batches in the houses they are assigned
to. Owners and managers are never restricted.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from production.models import Batch, DailyRecord, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def houses(staffed_farm):
    return {
        n: House.objects.create(farm=staffed_farm, name=f"House {n}", capacity=5000)
        for n in (1, 3)
    }


def make_batch(house, owner, code):
    return Batch.objects.create(
        house=house,
        batch_code=code,
        initial_bird_count=1000,
        start_date=timezone.localdate() - timedelta(days=10),
        created_by=owner,
    )


@pytest.fixture
def batches(houses, owner):
    return {
        1: make_batch(houses[1], owner, "H1-1"),
        3: make_batch(houses[3], owner, "H3-1"),
    }


def daily_url(farm, batch):
    return reverse(
        "production:daily-list",
        kwargs={"farm_pk": farm.pk, "batch_pk": batch.id},
    )


def bulk_sync_url(farm, batch):
    return reverse(
        "production:daily-bulk-sync",
        kwargs={"farm_pk": farm.pk, "batch_pk": batch.id},
    )


RECORD = {
    "record_date": timezone.localdate().isoformat(),
    "mortality_disease": 1,
    "mortality_heat": 0,
    "mortality_culled": 0,
    "mortality_unknown": 0,
    "feed_kg": "10.00",
}


class TestWorkerWrites:
    def test_unassigned_worker_can_write_to_any_batch(
        self, auth, worker, staffed_farm, batches
    ):
        """Backward compatibility: no assignments means unrestricted."""
        resp = auth(worker).post(
            daily_url(staffed_farm, batches[3]), RECORD, format="json"
        )
        assert resp.status_code == 201

    def test_assigned_worker_can_write_to_their_house(
        self, auth, worker, staffed_farm, batches
    ):
        staffed_farm.memberships.get(user=worker).houses.set(
            [batches[1].house]
        )
        resp = auth(worker).post(
            daily_url(staffed_farm, batches[1]), RECORD, format="json"
        )
        assert resp.status_code == 201

    def test_assigned_worker_is_refused_another_house(
        self, auth, worker, staffed_farm, batches
    ):
        staffed_farm.memberships.get(user=worker).houses.set(
            [batches[1].house]
        )
        resp = auth(worker).post(
            daily_url(staffed_farm, batches[3]), RECORD, format="json"
        )
        assert resp.status_code == 403
        assert "House 3" in str(resp.data)
        assert not DailyRecord.objects.filter(batch=batches[3]).exists()

    def test_assigned_worker_can_still_read_another_house(
        self, auth, worker, staffed_farm, batches, owner
    ):
        staffed_farm.memberships.get(user=worker).houses.set(
            [batches[1].house]
        )
        DailyRecord.objects.create(
            batch=batches[3],
            record_date=timezone.localdate(),
            mortality_disease=2,
            feed_kg="15.00",
            recorded_by=owner,
        )

        detail = auth(worker).get(
            reverse(
                "production:batch-detail",
                kwargs={"farm_pk": staffed_farm.pk, "pk": batches[3].id},
            )
        )
        assert detail.status_code == 200

        records = auth(worker).get(daily_url(staffed_farm, batches[3]))
        assert records.status_code == 200
        assert len(records.data) == 1


class TestBulkSync:
    def test_unassigned_house_fails_per_record_not_whole_request(
        self, auth, worker, staffed_farm, batches
    ):
        staffed_farm.memberships.get(user=worker).houses.set(
            [batches[1].house]
        )
        resp = auth(worker).post(
            bulk_sync_url(staffed_farm, batches[3]),
            {"records": [RECORD]},
            format="json",
        )
        assert resp.status_code == 207
        assert resp.data["created"] == []
        assert len(resp.data["failed"]) == 1
        assert "House 3" in resp.data["failed"][0]["error"]
        assert not DailyRecord.objects.filter(batch=batches[3]).exists()


class TestOwnerManagerUnrestricted:
    def test_owner_writes_to_any_house(self, auth, owner, staffed_farm, batches):
        resp = auth(owner).post(
            daily_url(staffed_farm, batches[3]), RECORD, format="json"
        )
        assert resp.status_code == 201

    def test_manager_writes_to_any_house(self, auth, manager, staffed_farm, batches):
        resp = auth(manager).post(
            daily_url(staffed_farm, batches[3]), RECORD, format="json"
        )
        assert resp.status_code == 201
