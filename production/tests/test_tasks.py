# production/tests/test_tasks.py
"""
Daily routine — task templates and completions.

A manager defines the routine (TaskTemplate), farm-wide; any worker ticks
items off for their shed on the day (TaskCompletion, per-house).
Completions extend OfflineSyncModel, so they carry the client UUID PK and
the 24-hour edit lock. Two divergences from the daily-record path: a
second device ticking the same (item, day, house) is absorbed rather than
rejected — a completion has no content to lose — and the routine is
farm-wide while its completion is per-house.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from production.models import House, TaskCompletion, TaskTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture
def templates(staffed_farm):
    return [
        TaskTemplate.objects.create(
            farm=staffed_farm, name=name, order=i, suggested_time="6:00 AM"
        )
        for i, name in enumerate(["Morning feed", "Health check", "Water check"], start=1)
    ]


@pytest.fixture
def houses(staffed_farm):
    return [
        House.objects.create(farm=staffed_farm, name=f"House {n}", capacity=5000)
        for n in (1, 2, 3)
    ]


def template_list_url(farm):
    return reverse("production:task-template-list", kwargs={"farm_pk": farm.pk})


def template_detail_url(farm, template):
    return reverse(
        "production:task-template-detail",
        kwargs={"farm_pk": farm.pk, "pk": template.pk},
    )


def completion_list_url(farm):
    return reverse("production:task-completion-list", kwargs={"farm_pk": farm.pk})


def completion_detail_url(farm, pk):
    return reverse(
        "production:task-completion-detail", kwargs={"farm_pk": farm.pk, "pk": pk}
    )


def bulk_sync_url(farm):
    return reverse(
        "production:task-completion-bulk-sync", kwargs={"farm_pk": farm.pk}
    )


def tick_payload(template, house, *, id=None, date=None):
    body = {
        "template": template.pk,
        "house": house.pk,
        "completion_date": (date or timezone.localdate()).isoformat(),
    }
    if id:
        body["id"] = id
    return body


class TestTickAndUntick:
    def test_worker_ticks_then_unticks_within_24h(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        resp = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0]),
            format="json",
        )
        assert resp.status_code == 201
        completion_id = resp.data["id"]
        assert TaskCompletion.objects.filter(pk=completion_id).exists()

        undo = client.delete(completion_detail_url(staffed_farm, completion_id))
        assert undo.status_code == 204
        assert not TaskCompletion.objects.filter(pk=completion_id).exists()

    def test_locked_completion_cannot_be_edited_or_deleted_by_worker(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        created = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0]),
            format="json",
        )
        completion_id = created.data["id"]

        # Age it past the 24-hour window (created_at is auto_now_add).
        TaskCompletion.objects.filter(pk=completion_id).update(
            created_at=timezone.now() - timedelta(hours=25)
        )

        # "Edit" is a re-POST carrying the same id — routed through update().
        edit = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id=completion_id),
            format="json",
        )
        assert edit.status_code == 400
        assert "locked" in str(edit.data).lower()

        undo = client.delete(completion_detail_url(staffed_farm, completion_id))
        assert undo.status_code == 400
        assert "locked" in str(undo.data).lower()
        assert TaskCompletion.objects.filter(pk=completion_id).exists()


class TestPerHouse:
    def test_same_template_completed_once_per_house_same_day(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        for house in (houses[0], houses[1]):
            resp = client.post(
                completion_list_url(staffed_farm),
                tick_payload(templates[0], house),
                format="json",
            )
            assert resp.status_code == 201
        assert TaskCompletion.objects.filter(template=templates[0]).count() == 2

    def test_completions_list_can_filter_by_house(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0]),
            format="json",
        )
        client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[1]),
            format="json",
        )

        all_rows = client.get(completion_list_url(staffed_farm)).data
        assert len(all_rows) == 2

        h1_rows = client.get(
            f"{completion_list_url(staffed_farm)}?house={houses[0].pk}"
        ).data
        assert len(h1_rows) == 1
        assert h1_rows[0]["house"] == houses[0].pk


class TestIdempotency:
    def test_duplicate_post_same_id_returns_200_not_integrity_error(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        cid = "11111111-1111-1111-1111-111111111111"

        first = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id=cid),
            format="json",
        )
        assert first.status_code == 201

        again = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id=cid),
            format="json",
        )
        assert again.status_code == 200
        assert TaskCompletion.objects.filter(template=templates[0]).count() == 1

    def test_two_ids_same_template_day_and_house_are_absorbed(
        self, auth, worker, staffed_farm, templates, houses
    ):
        """
        Decided divergence from DailyRecordListCreateView: a completion has
        no content, so the second write wins nothing and loses nothing —
        it is absorbed, not rejected, and never 500s.
        """
        client = auth(worker)

        a = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            format="json",
        )
        assert a.status_code == 201

        b = client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            format="json",
        )
        assert b.status_code == 200
        assert TaskCompletion.objects.filter(template=templates[0]).count() == 1

    def test_bulk_sync_absorbs_a_same_day_conflict(
        self, auth, worker, staffed_farm, templates, houses
    ):
        client = auth(worker)
        client.post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[0], id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            format="json",
        )

        conflicting_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        resp = client.post(
            bulk_sync_url(staffed_farm),
            {"records": [tick_payload(templates[0], houses[0], id=conflicting_id)]},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["created"] == []
        assert resp.data["updated"] == [conflicting_id]
        assert resp.data["failed"] == []
        assert TaskCompletion.objects.filter(template=templates[0]).count() == 1


class TestHouseWriteScoping:
    def test_assigned_worker_is_refused_another_house(
        self, auth, worker, staffed_farm, templates, houses
    ):
        staffed_farm.memberships.get(user=worker).houses.set([houses[0]])
        resp = auth(worker).post(
            completion_list_url(staffed_farm),
            tick_payload(templates[0], houses[2]),
            format="json",
        )
        assert resp.status_code == 403
        assert "House 3" in str(resp.data)
        assert not TaskCompletion.objects.filter(template=templates[0]).exists()

    def test_bulk_sync_rejects_unassigned_house_per_record(
        self, auth, worker, staffed_farm, templates, houses
    ):
        staffed_farm.memberships.get(user=worker).houses.set([houses[0]])
        resp = auth(worker).post(
            bulk_sync_url(staffed_farm),
            {"records": [tick_payload(templates[0], houses[2], id="dddddddd-dddd-dddd-dddd-dddddddddddd")]},
            format="json",
        )
        assert resp.status_code == 207
        assert resp.data["created"] == []
        assert len(resp.data["failed"]) == 1
        assert "House 3" in resp.data["failed"][0]["error"]
        assert not TaskCompletion.objects.filter(template=templates[0]).exists()

    def test_unassigned_worker_can_tick_any_house(
        self, auth, worker, staffed_farm, templates, houses
    ):
        assert not staffed_farm.memberships.get(user=worker).houses.exists()
        for house in houses:
            resp = auth(worker).post(
                completion_list_url(staffed_farm),
                tick_payload(templates[0], house),
                format="json",
            )
            assert resp.status_code == 201

    def test_owner_and_manager_can_tick_any_house(
        self, auth, owner, manager, staffed_farm, templates, houses
    ):
        assert (
            auth(owner)
            .post(
                completion_list_url(staffed_farm),
                tick_payload(templates[0], houses[2]),
                format="json",
            )
            .status_code
            == 201
        )
        assert (
            auth(manager)
            .post(
                completion_list_url(staffed_farm),
                tick_payload(templates[1], houses[2]),
                format="json",
            )
            .status_code
            == 201
        )


class TestTemplateAuthority:
    def test_worker_cannot_create_a_template(self, auth, worker, staffed_farm):
        resp = auth(worker).post(
            template_list_url(staffed_farm),
            {"name": "Sneaky task", "order": 9},
            format="json",
        )
        assert resp.status_code == 403

    def test_worker_cannot_edit_a_template(
        self, auth, worker, staffed_farm, templates
    ):
        resp = auth(worker).patch(
            template_detail_url(staffed_farm, templates[0]),
            {"name": "Renamed"},
            format="json",
        )
        assert resp.status_code == 403

    def test_manager_can_create_and_reorder_templates(
        self, auth, manager, staffed_farm, templates
    ):
        created = auth(manager).post(
            template_list_url(staffed_farm),
            {"name": "Afternoon feed", "suggested_time": "2:00 PM", "order": 4},
            format="json",
        )
        assert created.status_code == 201

        reordered = auth(manager).patch(
            template_detail_url(staffed_farm, templates[0]),
            {"order": 8},
            format="json",
        )
        assert reordered.status_code == 200
        assert reordered.data["order"] == 8


class TestCrossFarmIsolation:
    def test_rival_owner_cannot_read_templates(self, auth, rival_owner, staffed_farm, templates):
        assert auth(rival_owner).get(template_list_url(staffed_farm)).status_code == 403

    def test_rival_owner_cannot_read_completions(self, auth, rival_owner, staffed_farm):
        assert (
            auth(rival_owner).get(completion_list_url(staffed_farm)).status_code == 403
        )
