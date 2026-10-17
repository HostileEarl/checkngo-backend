# production/tests/test_house_archive.py
"""
Archiving and restoring a house — owner only, refused while a house has
an active batch, and hidden from daily use once archived while its past
records stay visible (and labelled) everywhere else.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.services import fcr_by_batch
from production.models import Batch, DailyRecord, Harvest, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="House 1", capacity=5000)


def archive_url(farm, house):
    return reverse(
        "production:house-archive", kwargs={"farm_pk": farm.pk, "pk": house.pk}
    )


def restore_url(farm, house):
    return reverse(
        "production:house-restore", kwargs={"farm_pk": farm.pk, "pk": house.pk}
    )


def houses_url(farm):
    return reverse("production:house-list", kwargs={"farm_pk": farm.pk})


class TestOwnerCanArchiveAndRestore:
    def test_owner_can_archive(self, auth, owner, staffed_farm, house):
        client = auth(owner)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 200
        house.refresh_from_db()
        assert house.archived_at is not None
        assert resp.data["archived_at"] is not None

    def test_owner_can_restore(self, auth, owner, staffed_farm, house):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        resp = client.post(restore_url(staffed_farm, house))
        assert resp.status_code == 200
        house.refresh_from_db()
        assert house.archived_at is None

    def test_archiving_twice_is_refused(self, auth, owner, staffed_farm, house):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 400

    def test_restoring_an_unarchived_house_is_refused(self, auth, owner, staffed_farm, house):
        client = auth(owner)
        resp = client.post(restore_url(staffed_farm, house))
        assert resp.status_code == 400


class TestManagerAndWorkerAreRefused:
    def test_manager_gets_403_on_archive(self, auth, manager, staffed_farm, house):
        client = auth(manager)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 403

    def test_worker_gets_403_on_archive(self, auth, worker, staffed_farm, house):
        client = auth(worker)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 403

    def test_manager_gets_403_on_restore(self, auth, manager, staffed_farm, house):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])
        client = auth(manager)
        resp = client.post(restore_url(staffed_farm, house))
        assert resp.status_code == 403


class TestActiveBatchBlocksArchiving:
    def test_a_house_with_an_active_batch_cannot_be_archived(
        self, auth, owner, staffed_farm, house
    ):
        Batch.objects.create(
            house=house,
            batch_code="B-1",
            initial_bird_count=1000,
            start_date=timezone.localdate() - timedelta(days=5),
            created_by=owner,
        )

        client = auth(owner)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 400
        assert "active batch" in resp.data["detail"]
        house.refresh_from_db()
        assert house.archived_at is None

    def test_archiving_is_allowed_once_the_batch_is_harvested(
        self, auth, owner, staffed_farm, house
    ):
        Batch.objects.create(
            house=house,
            batch_code="B-1",
            initial_bird_count=1000,
            start_date=timezone.localdate() - timedelta(days=40),
            status=Batch.Status.HARVESTED,
            created_by=owner,
        )

        client = auth(owner)
        resp = client.post(archive_url(staffed_farm, house))
        assert resp.status_code == 200


class TestArchivedHouseHiddenFromDailyUse:
    def test_archived_house_is_left_out_of_the_default_house_list(
        self, auth, owner, staffed_farm, house
    ):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        resp = client.get(houses_url(staffed_farm))
        assert resp.status_code == 200
        assert house.pk not in [h["id"] for h in resp.data]

    def test_include_archived_param_brings_it_back(
        self, auth, owner, staffed_farm, house
    ):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        resp = client.get(houses_url(staffed_farm) + "?include_archived=true")
        assert resp.status_code == 200
        assert house.pk in [h["id"] for h in resp.data]

    def test_a_batch_cannot_be_placed_in_an_archived_house(
        self, auth, owner, staffed_farm, house
    ):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        resp = client.post(
            reverse("production:batch-list", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "house": house.pk,
                "batch_code": "B-NEW",
                "initial_bird_count": 1000,
                "start_date": str(timezone.localdate()),
            },
        )
        assert resp.status_code == 400
        assert "archived" in str(resp.data).lower()

    def test_a_worker_cannot_be_assigned_to_an_archived_house(
        self, auth, owner, staffed_farm, house, worker
    ):
        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        client = auth(owner)
        membership = worker.farm_memberships.get(farm=staffed_farm)
        resp = client.patch(
            reverse(
                "farms:member-houses",
                kwargs={"farm_pk": staffed_farm.pk, "pk": membership.pk},
            ),
            {"houses": [house.pk]},
            format="json",
        )
        assert resp.status_code == 400
        assert "archived" in str(resp.data).lower()


class TestArchivedHouseHistoryStillReportable:
    def test_a_harvested_batch_in_an_archived_house_still_appears_in_fcr_report(
        self, owner, staffed_farm, house
    ):
        batch = Batch.objects.create(
            house=house,
            batch_code="B-OLD",
            initial_bird_count=1000,
            start_date=timezone.localdate() - timedelta(days=40),
            status=Batch.Status.HARVESTED,
            total_feed_kg="2000.00",
            created_by=owner,
        )
        Harvest.objects.create(
            batch=batch,
            harvest_date=timezone.localdate(),
            birds_harvested=950,
            total_weight_kg="1500.00",
            closing_notes="",
        )

        house.archived_at = timezone.now()
        house.save(update_fields=["archived_at"])

        result = fcr_by_batch(staffed_farm)
        row = next(r for r in result["rows"] if r["batch_code"] == "B-OLD")
        assert row["house_name"] == "House 1 (archived)"
