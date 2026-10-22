# production/tests/test_access_lost_before_sync.py
"""
A worker loses access before their phone syncs (manuscript review, item A4).

The worker typed records while offline; before the phone reconnected,
the owner changed their access. The queued records then arrive through
bulk sync. Access is checked when the records ARRIVE, not when they were
typed (recorded_at is the client's clock and is never trusted for
authorization), so:

  (a) membership deactivated  → every request is refused with a 403
                                 (FarmScopedPermission finds no active
                                 membership) and nothing is saved;
  (b) unassigned from House 1 → the House 1 records come back one by one
                                 in failed[] of a 207 (the bulk serializers
                                 check FarmMembership.may_write_to_house
                                 per record), and nothing is saved for that
                                 house. The same backlog for a house they
                                 are still assigned to lands, which shows
                                 the refusal is about House 1 alone.

Changes of access go through the owner's real endpoints (member revoke,
member house assignment), the way they would in the app.
"""
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
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    TaskCompletion,
    TaskTemplate,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def farm_world():
    today = timezone.localdate()
    owner = make_user("+639175000001", "Sync Owner", User.Role.OWNER)
    worker = make_user("+639175000002", "Offline Worker", User.Role.WORKER)
    farm = Farm.objects.create(name="Sync Farm", owner=owner)
    membership = FarmMembership.objects.create(
        farm=farm, user=worker, role=FarmMembership.Role.WORKER, invited_by=owner
    )
    house1 = House.objects.create(farm=farm, name="House 1", capacity=5000)
    house2 = House.objects.create(farm=farm, name="House 2", capacity=5000)
    membership.houses.set([house1, house2])

    def batch(house, code):
        return Batch.objects.create(
            house=house,
            batch_code=code,
            initial_bird_count=1000,
            start_date=today - timedelta(days=10),
            created_by=owner,
        )

    item = InventoryItem.objects.create(farm=farm, name="Disinfectant", unit="litre")
    InventoryStockIn.objects.create(item=item, quantity=Decimal("50"), stock_in_date=today)
    template = TaskTemplate.objects.create(farm=farm, name="Water check", created_by=owner)
    return {
        "owner": owner,
        "worker": worker,
        "farm": farm,
        "membership": membership,
        "house1": house1,
        "house2": house2,
        "batch1": batch(house1, "H1-B"),
        "batch2": batch(house2, "H2-B"),
        "item": item,
        "template": template,
    }


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _days_back(n):
    return (timezone.localdate() - timedelta(days=n)).isoformat()


def _queued_daily_records():
    """Three days typed offline, the oldest first, each with a client UUID."""
    return [
        {
            "id": f"00000000-0000-4000-8000-00000000000{n}",
            "record_date": _days_back(n),
            "mortality_disease": n,
            "mortality_heat": 0,
            "mortality_culled": 0,
            "mortality_unknown": 0,
            "feed_kg": "35.00",
            "recorded_at": f"{_days_back(n)}T06:00:00+08:00",
        }
        for n in (3, 2, 1)
    ]


def _daily_sync(client, world, batch):
    url = reverse(
        "production:daily-bulk-sync",
        kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk},
    )
    return client.post(url, {"records": _queued_daily_records()}, format="json")


def _ticks_sync(client, world, house):
    url = reverse("production:task-completion-bulk-sync", kwargs={"farm_pk": world["farm"].pk})
    records = [
        {"template": world["template"].pk, "house": house.pk, "completion_date": _days_back(n)}
        for n in (2, 1)
    ]
    return client.post(url, {"records": records}, format="json")


def _usage_sync(client, world):
    url = reverse("production:inventory-usage-bulk-sync", kwargs={"farm_pk": world["farm"].pk})
    records = [{"item": world["item"].pk, "quantity_used": "1.00", "usage_date": _days_back(1)}]
    return client.post(url, {"records": records}, format="json")


def _revoke(world):
    url = reverse(
        "farms:member-revoke",
        kwargs={"farm_pk": world["farm"].pk, "pk": world["membership"].pk},
    )
    response = _client(world["owner"]).post(url)
    assert response.status_code == 200, response.data


def _assign(world, houses):
    url = reverse(
        "farms:member-houses",
        kwargs={"farm_pk": world["farm"].pk, "pk": world["membership"].pk},
    )
    response = _client(world["owner"]).patch(
        url, {"houses": [h.pk for h in houses]}, format="json"
    )
    assert response.status_code == 200, response.data


class TestMembershipDeactivated:
    def test_daily_backlog_is_refused_and_nothing_saved(self, farm_world):
        _revoke(farm_world)
        response = _daily_sync(_client(farm_world["worker"]), farm_world, farm_world["batch1"])
        assert response.status_code == 403
        assert not DailyRecord.objects.exists()
        farm_world["batch1"].refresh_from_db()
        assert farm_world["batch1"].total_mortality == 0

    def test_routine_and_usage_backlogs_are_refused_too(self, farm_world):
        _revoke(farm_world)
        worker = _client(farm_world["worker"])
        assert _ticks_sync(worker, farm_world, farm_world["house1"]).status_code == 403
        assert _usage_sync(worker, farm_world).status_code == 403
        assert not TaskCompletion.objects.exists()
        assert not InventoryUsageLog.objects.exists()

    def test_a_single_queued_post_is_refused(self, farm_world):
        _revoke(farm_world)
        url = reverse(
            "production:daily-list",
            kwargs={"farm_pk": farm_world["farm"].pk, "batch_pk": farm_world["batch1"].pk},
        )
        record = _queued_daily_records()[0]
        response = _client(farm_world["worker"]).post(url, record, format="json")
        assert response.status_code == 403
        assert not DailyRecord.objects.exists()

    def test_a_record_synced_before_revocation_cannot_be_updated_after(self, farm_world):
        worker = _client(farm_world["worker"])
        first = _daily_sync(worker, farm_world, farm_world["batch1"])
        assert first.status_code == 200, first.data
        before = list(DailyRecord.objects.order_by("record_date").values())

        _revoke(farm_world)
        changed = _queued_daily_records()
        for record in changed:
            record["mortality_unknown"] = 9
        url = reverse(
            "production:daily-bulk-sync",
            kwargs={"farm_pk": farm_world["farm"].pk, "batch_pk": farm_world["batch1"].pk},
        )
        response = worker.post(url, {"records": changed}, format="json")
        assert response.status_code == 403
        assert list(DailyRecord.objects.order_by("record_date").values()) == before


class TestUnassignedFromHouseOne:
    def test_house_one_records_fail_one_by_one_and_nothing_is_saved(self, farm_world):
        _assign(farm_world, [farm_world["house2"]])
        response = _daily_sync(_client(farm_world["worker"]), farm_world, farm_world["batch1"])

        assert response.status_code == 207, response.data
        assert response.data["created"] == []
        assert response.data["updated"] == []
        assert len(response.data["failed"]) == 3
        assert {f["record_date"] for f in response.data["failed"]} == {_days_back(n) for n in (3, 2, 1)}
        assert all("not assigned to House 1" in f["error"] for f in response.data["failed"])
        assert not DailyRecord.objects.filter(batch=farm_world["batch1"]).exists()

    def test_the_same_backlog_for_a_house_still_assigned_lands(self, farm_world):
        _assign(farm_world, [farm_world["house2"]])
        response = _daily_sync(_client(farm_world["worker"]), farm_world, farm_world["batch2"])
        assert response.status_code == 200, response.data
        assert len(response.data["created"]) == 3
        assert DailyRecord.objects.filter(batch=farm_world["batch2"]).count() == 3

    def test_house_one_routine_ticks_fail_per_record(self, farm_world):
        _assign(farm_world, [farm_world["house2"]])
        response = _ticks_sync(_client(farm_world["worker"]), farm_world, farm_world["house1"])
        assert response.status_code == 207, response.data
        assert response.data["created"] == [] and response.data["updated"] == []
        assert len(response.data["failed"]) == 2
        assert not TaskCompletion.objects.filter(house=farm_world["house1"]).exists()

    def test_a_single_queued_post_for_house_one_is_a_403(self, farm_world):
        _assign(farm_world, [farm_world["house2"]])
        url = reverse(
            "production:daily-list",
            kwargs={"farm_pk": farm_world["farm"].pk, "batch_pk": farm_world["batch1"].pk},
        )
        response = _client(farm_world["worker"]).post(url, _queued_daily_records()[0], format="json")
        assert response.status_code == 403
        assert not DailyRecord.objects.filter(batch=farm_world["batch1"]).exists()

    def test_a_house_one_record_synced_before_cannot_be_updated_after(self, farm_world):
        worker = _client(farm_world["worker"])
        assert _daily_sync(worker, farm_world, farm_world["batch1"]).status_code == 200
        before = list(DailyRecord.objects.order_by("record_date").values())

        _assign(farm_world, [farm_world["house2"]])
        response = _daily_sync(worker, farm_world, farm_world["batch1"])
        assert response.status_code == 207
        assert response.data["updated"] == []
        assert list(DailyRecord.objects.order_by("record_date").values()) == before

    def test_clearing_every_assignment_reopens_all_houses_by_design(self, farm_world):
        """
        Not a refusal case: it pins down the rule the case above depends on.
        FarmMembership.may_write_to_house treats an EMPTY assignment as
        "every house" (FarmMemberHouseAssignmentView's docstring: "An empty
        list clears the restriction"). Taking a worker off their only house
        therefore widens their access instead of ending it; to stop a
        worker writing anywhere, revoke the membership.
        """
        _assign(farm_world, [farm_world["house1"]])
        _assign(farm_world, [])
        response = _daily_sync(_client(farm_world["worker"]), farm_world, farm_world["batch1"])
        assert response.status_code == 200
        assert len(response.data["created"]) == 3
