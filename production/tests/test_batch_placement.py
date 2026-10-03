# production/tests/test_batch_placement.py
"""
Placing a new batch — POST /farms/<farm_pk>/batches/.

No test previously covered this endpoint; batches in other test files are
all created directly through the ORM, which never exercises
BatchCreateSerializer.validate(). These are new.

House.capacity is guidance, not a hard ceiling: a supplier sometimes
delivers more than was ordered (compensation for a prior batch's losses,
for instance), and the delivered count — not an idealized one capped at
capacity — is what mortality rate, FCR, and birds-alive must be computed
from for the rest of the cycle. So placing above capacity must succeed.
The one-active-batch-per-house rule is a different kind of constraint —
real data integrity, not guidance — and stays enforced.
"""
from datetime import date

import pytest

from production.models import Batch, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed 1", capacity=500)


def _url(farm):
    return f"/api/farms/{farm.id}/batches/"


def _payload(house, **overrides):
    payload = {
        "house": house.id,
        "batch_code": "B-2026-01",
        "start_date": "2026-01-01",
        "initial_bird_count": 500,
    }
    payload.update(overrides)
    return payload


def test_count_above_capacity_is_allowed(auth, owner, staffed_farm, house):
    """
    A delivery of more chicks than the house's stated capacity — e.g. 514
    against a 500-bird house — is recorded as-is, not rejected.
    """
    response = auth(owner).post(
        _url(staffed_farm),
        _payload(house, initial_bird_count=514),
        format="json",
    )
    assert response.status_code == 201, response.data

    batch = Batch.objects.get(pk=response.data["id"])
    assert batch.initial_bird_count == 514
    assert batch.initial_bird_count > house.capacity


def test_count_at_or_below_capacity_still_allowed(auth, owner, staffed_farm, house):
    """The ordinary case keeps working once the ceiling is removed."""
    response = auth(owner).post(
        _url(staffed_farm),
        _payload(house, initial_bird_count=500),
        format="json",
    )
    assert response.status_code == 201, response.data


def test_count_below_one_is_still_rejected(auth, owner, staffed_farm, house):
    """The capacity ceiling is gone; the model's MinValueValidator(1) is not."""
    response = auth(owner).post(
        _url(staffed_farm),
        _payload(house, initial_bird_count=0),
        format="json",
    )
    assert response.status_code == 400
    assert "initial_bird_count" in response.data


def test_second_active_batch_in_one_house_is_still_blocked(
    auth, owner, staffed_farm, house
):
    """
    The one-active-batch-per-house rule is unrelated to capacity and must
    keep rejecting a second placement into an occupied house.
    """
    Batch.objects.create(
        house=house,
        batch_code="B-2026-00",
        initial_bird_count=400,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )

    response = auth(owner).post(
        _url(staffed_farm),
        _payload(house, batch_code="B-2026-01"),
        format="json",
    )
    assert response.status_code == 400
    assert "house" in response.data


def test_manager_can_place_a_batch(auth, manager, staffed_farm, house):
    response = auth(manager).post(
        _url(staffed_farm),
        _payload(house),
        format="json",
    )
    assert response.status_code == 201, response.data


def test_worker_cannot_place_a_batch(auth, worker, staffed_farm, house):
    response = auth(worker).post(
        _url(staffed_farm),
        _payload(house),
        format="json",
    )
    assert response.status_code == 403
