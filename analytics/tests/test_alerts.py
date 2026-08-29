# analytics/tests/test_alerts.py
"""
Computed alerts — GET /api/farms/<farm_pk>/alerts/.

The endpoint derives standing conditions from live data. These tests pin
the two things that matter: the arithmetic that decides whether a condition
fires, and the role filter that decides who is told about it.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from production.models import Batch, DailyRecord, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def url(staffed_farm):
    return reverse("analytics:alerts", kwargs={"farm_pk": staffed_farm.pk})


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="House 1", capacity=20000)


def make_batch(house, owner, *, code, birds=1000, started_days_ago=10):
    return Batch.objects.create(
        house=house,
        batch_code=code,
        initial_bird_count=birds,
        start_date=timezone.localdate() - timedelta(days=started_days_ago),
        created_by=owner,
    )


def add_record(batch, owner, *, day, disease=0, feed="50.00"):
    return DailyRecord.objects.create(
        batch=batch,
        record_date=day,
        mortality_disease=disease,
        mortality_heat=0,
        mortality_culled=0,
        mortality_unknown=0,
        feed_kg=Decimal(feed),
        recorded_by=owner,
    )


class TestMortalityThreshold:
    def test_batch_at_9pct_is_a_danger_alert_4pct_is_not(
        self, auth, owner, url, house
    ):
        today = timezone.localdate()
        hot = make_batch(house, owner, code="HOT-1")
        add_record(hot, owner, day=today, disease=90)  # 90 / 1000 = 9%

        # A second house — one ACTIVE batch per house is enforced.
        house2 = House.objects.create(
            farm=house.farm, name="House 2", capacity=20000
        )
        calm = make_batch(house2, owner, code="CALM-1")
        add_record(calm, owner, day=today, disease=40)  # 4%

        resp = auth(owner).get(url)
        assert resp.status_code == 200

        mortality = {
            a["id"]: a for a in resp.data["alerts"] if a["id"].startswith("mortality:")
        }
        assert set(mortality) == {f"mortality:{hot.id}"}
        assert mortality[f"mortality:{hot.id}"]["severity"] == "danger"
        assert "9.00%" in mortality[f"mortality:{hot.id}"]["detail"]


class TestRoleFilter:
    def test_worker_does_not_receive_owner_manager_alerts(
        self, auth, owner, worker, url, house
    ):
        today = timezone.localdate()
        batch = make_batch(house, owner, code="FEED-1")
        # Consumption with no deliveries → negative feed balance (owner/manager).
        add_record(batch, owner, day=today, feed="500.00")

        manager_ids = {a["id"] for a in auth(owner).get(url).data["alerts"]}
        worker_ids = {a["id"] for a in auth(worker).get(url).data["alerts"]}

        assert "feed-balance" in manager_ids
        assert "feed-balance" not in worker_ids
        # The worker still sees everyone-audience alerts.
        assert f"today:{batch.id}" not in worker_ids  # recorded today
        for aid in worker_ids:
            assert not aid.startswith("feed-balance")
            assert not aid.startswith("invitation:")
            assert not aid.startswith("harvest:")

    def test_worker_still_gets_shared_alerts(self, auth, owner, worker, url, house):
        started = timezone.localdate() - timedelta(days=3)
        batch = make_batch(house, owner, code="SHARED-1", started_days_ago=3)
        # No record today, started before today → "not recorded" (everyone).
        assert batch.start_date < timezone.localdate() and started == batch.start_date

        worker_ids = {a["id"] for a in auth(worker).get(url).data["alerts"]}
        assert f"today:{batch.id}" in worker_ids


class TestAccess:
    def test_member_of_another_farm_gets_403(self, auth, rival_owner, url):
        assert auth(rival_owner).get(url).status_code == 403

    def test_unauthenticated_gets_401(self, api, url):
        assert api.get(url).status_code == 401


class TestStability:
    def test_ids_are_stable_across_two_requests(self, auth, owner, url, house):
        today = timezone.localdate()
        b1 = make_batch(house, owner, code="STABLE-1")
        add_record(b1, owner, day=today, disease=95)

        house2 = House.objects.create(
            farm=house.farm, name="House 2", capacity=20000
        )
        b2 = make_batch(house2, owner, code="STABLE-2", started_days_ago=2)

        first = [a["id"] for a in auth(owner).get(url).data["alerts"]]
        second = [a["id"] for a in auth(owner).get(url).data["alerts"]]

        assert first == second
        assert first  # something actually fired, so the check has teeth


class TestTodayNotRecorded:
    def test_active_batch_with_todays_record_has_no_not_recorded_alert(
        self, auth, owner, url, house
    ):
        batch = make_batch(house, owner, code="DONE-1", started_days_ago=5)
        add_record(batch, owner, day=timezone.localdate())

        ids = {a["id"] for a in auth(owner).get(url).data["alerts"]}
        assert f"today:{batch.id}" not in ids

    def test_batch_started_today_has_no_not_recorded_alert(
        self, auth, owner, url, house
    ):
        batch = make_batch(house, owner, code="NEW-1", started_days_ago=0)
        ids = {a["id"] for a in auth(owner).get(url).data["alerts"]}
        assert f"today:{batch.id}" not in ids


class TestEmpty:
    def test_farm_with_nothing_wrong_returns_empty_list(self, auth, owner, url):
        resp = auth(owner).get(url)
        assert resp.status_code == 200
        assert resp.data["alerts"] == []
        assert resp.data["counts"] == {"danger": 0, "warning": 0, "info": 0}
