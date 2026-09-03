# production/tests/test_harvest.py
"""
Closing a batch out.

HarvestCreateView already existed and validated; nothing but the seed
command called it. These pin the behaviour the new harvest screen depends
on: it sets the batch to HARVESTED, frees the house for a new placement,
shuts the door on further daily records, and stays owner/manager only.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from production.models import Batch, DailyRecord, Harvest, House

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    b = Batch.objects.create(
        house=house,
        batch_code="H-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    # 40 birds lost over 4 days -> 960 remaining.
    for day in range(1, 5):
        DailyRecord.objects.create(
            batch=b,
            record_date=date(2026, 1, day),
            mortality_disease=10,
            feed_kg=Decimal("100.00"),
            recorded_by=owner,
        )
    b.refresh_from_db()
    return b


def _url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/harvest/"


def _payload(**overrides):
    body = {
        "harvest_date": date(2026, 2, 15).isoformat(),
        "birds_harvested": 950,
        "total_weight_kg": "1800.00",
    }
    body.update(overrides)
    return body


def test_harvest_sets_status_and_frees_the_house(auth, owner, staffed_farm, house, batch):
    response = auth(owner).post(_url(staffed_farm, batch), _payload(), format="json")
    assert response.status_code == 201, response.data

    batch.refresh_from_db()
    assert batch.status == Batch.Status.HARVESTED

    # The house is free: the partial unique index only blocks a second
    # ACTIVE batch, so a new one can now be placed in the same shed.
    replacement = Batch.objects.create(
        house=house,
        batch_code="H-2",
        initial_bird_count=1000,
        start_date=date(2026, 2, 20),
        created_by=owner,
    )
    assert replacement.status == Batch.Status.ACTIVE


def test_manager_can_harvest(auth, manager, staffed_farm, batch):
    response = auth(manager).post(_url(staffed_farm, batch), _payload(), format="json")
    assert response.status_code == 201, response.data


def test_birds_harvested_above_remaining_is_rejected(auth, owner, staffed_farm, batch):
    # 960 remain (1000 placed, 40 lost).
    response = auth(owner).post(
        _url(staffed_farm, batch), _payload(birds_harvested=1200), format="json"
    )
    assert response.status_code == 400
    assert "birds_harvested" in response.data


def test_harvested_batch_rejects_new_daily_records(auth, owner, staffed_farm, batch):
    assert (
        auth(owner)
        .post(_url(staffed_farm, batch), _payload(), format="json")
        .status_code
        == 201
    )

    response = auth(owner).post(
        f"/api/farms/{staffed_farm.id}/batches/{batch.id}/daily-records/",
        {
            "record_date": date(2026, 2, 16).isoformat(),
            "mortality_disease": 1,
            "feed_kg": "10.00",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "closed" in str(response.data).lower()


def test_worker_cannot_harvest(auth, worker, staffed_farm, batch):
    response = auth(worker).post(_url(staffed_farm, batch), _payload(), format="json")
    assert response.status_code == 403
    assert not Harvest.objects.filter(batch=batch).exists()


def test_future_harvest_date_is_rejected(auth, owner, staffed_farm, batch):
    future = (date.today() + timedelta(days=3)).isoformat()
    response = auth(owner).post(
        _url(staffed_farm, batch), _payload(harvest_date=future), format="json"
    )
    assert response.status_code == 400
