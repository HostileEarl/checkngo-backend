# farms/tests/test_house_assignment.py
"""
House-level write scoping — the membership model and the assignment API.

The endpoint decides which houses a worker may record against; the model
method `may_write_to_house` is what every write path consults.
"""
import pytest
from django.urls import reverse

from farms.models import FarmMembership
from production.models import House

pytestmark = pytest.mark.django_db


@pytest.fixture
def houses(staffed_farm):
    return [
        House.objects.create(farm=staffed_farm, name=f"House {n}", capacity=5000)
        for n in (1, 2, 3)
    ]


class TestMayWriteToHouse:
    def test_owner_and_manager_write_anywhere(self, staffed_farm, owner, manager, houses):
        owner_m = staffed_farm.memberships.get(user=owner)
        manager_m = staffed_farm.memberships.get(user=manager)
        assert owner_m.may_write_to_house(houses[2]) is True
        assert manager_m.may_write_to_house(houses[2]) is True

    def test_worker_with_no_assignments_is_unrestricted(
        self, staffed_farm, worker, houses
    ):
        worker_m = staffed_farm.memberships.get(user=worker)
        assert worker_m.houses.exists() is False
        assert all(worker_m.may_write_to_house(h) for h in houses)

    def test_worker_with_assignments_is_limited_to_them(
        self, staffed_farm, worker, houses
    ):
        worker_m = staffed_farm.memberships.get(user=worker)
        worker_m.houses.set([houses[0]])
        assert worker_m.may_write_to_house(houses[0]) is True
        assert worker_m.may_write_to_house(houses[2]) is False


class TestAssignmentEndpoint:
    def url(self, farm, membership):
        return reverse(
            "farms:member-houses", kwargs={"farm_pk": farm.pk, "pk": membership.pk}
        )

    def test_owner_can_assign_houses(self, auth, owner, staffed_farm, worker, houses):
        membership = staffed_farm.memberships.get(user=worker)
        resp = auth(owner).patch(
            self.url(staffed_farm, membership),
            {"houses": [houses[0].pk, houses[1].pk]},
            format="json",
        )
        assert resp.status_code == 200
        membership.refresh_from_db()
        assert set(membership.houses.values_list("pk", flat=True)) == {
            houses[0].pk,
            houses[1].pk,
        }
        assert sorted(resp.data["house_names"]) == ["House 1", "House 2"]

    def test_manager_can_assign_houses(self, auth, manager, staffed_farm, worker, houses):
        membership = staffed_farm.memberships.get(user=worker)
        resp = auth(manager).patch(
            self.url(staffed_farm, membership),
            {"houses": [houses[0].pk]},
            format="json",
        )
        assert resp.status_code == 200
        membership.refresh_from_db()
        assert list(membership.houses.values_list("pk", flat=True)) == [houses[0].pk]

    def test_empty_list_clears_the_restriction(
        self, auth, owner, staffed_farm, worker, houses
    ):
        membership = staffed_farm.memberships.get(user=worker)
        membership.houses.set([houses[0]])
        resp = auth(owner).patch(
            self.url(staffed_farm, membership), {"houses": []}, format="json"
        )
        assert resp.status_code == 200
        assert membership.houses.exists() is False

    def test_house_from_another_farm_is_rejected(
        self, auth, owner, staffed_farm, worker, rival_farm
    ):
        stray = House.objects.create(farm=rival_farm, name="Rival House", capacity=1000)
        membership = staffed_farm.memberships.get(user=worker)
        resp = auth(owner).patch(
            self.url(staffed_farm, membership),
            {"houses": [stray.pk]},
            format="json",
        )
        assert resp.status_code == 400
        assert membership.houses.exists() is False

    def test_worker_cannot_assign_houses_to_themselves(
        self, auth, worker, staffed_farm, houses
    ):
        membership = staffed_farm.memberships.get(user=worker)
        resp = auth(worker).patch(
            self.url(staffed_farm, membership),
            {"houses": [houses[0].pk]},
            format="json",
        )
        assert resp.status_code == 403
        assert membership.houses.exists() is False
