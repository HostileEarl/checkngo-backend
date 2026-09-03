# production/tests/test_harvest.py
"""
Closing a batch out.

Harvest is no longer a single typed sale — it is the record that CLOSES a
batch whose birds left across many SaleEvent rows. Creation moved to
POST /batches/<b>/close/, which takes only closing_notes and an optional
buyer_link and populates the totals from the summed sale events.

These pin the closing contract the sales screen depends on: it needs at
least one sale event, it sets the batch to HARVESTED, it shuts the door on
further daily records, and it stays owner/manager only.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from production.models import Batch, DailyRecord, Harvest, House, SaleEvent

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


def _sell(batch, birds=900, weight="1800.00", revenue="120000.00"):
    return SaleEvent.objects.create(
        batch=batch,
        sale_date=date(2026, 1, 10),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=birds,
        total_weight_kg=Decimal(weight),
        revenue=Decimal(revenue) if revenue is not None else None,
    )


def _close_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/close/"


def test_close_sets_status_and_frees_the_house(auth, owner, staffed_farm, house, batch):
    _sell(batch)
    response = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
    assert response.status_code == 201, response.data

    batch.refresh_from_db()
    assert batch.status == Batch.Status.HARVESTED

    replacement = Batch.objects.create(
        house=house,
        batch_code="H-2",
        initial_bird_count=1000,
        start_date=date(2026, 2, 20),
        created_by=owner,
    )
    assert replacement.status == Batch.Status.ACTIVE


def test_manager_can_close(auth, manager, staffed_farm, batch):
    _sell(batch)
    response = auth(manager).post(_close_url(staffed_farm, batch), {}, format="json")
    assert response.status_code == 201, response.data


def test_close_with_no_sale_events_is_refused(auth, owner, staffed_farm, batch):
    response = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
    assert response.status_code == 400
    assert not Harvest.objects.filter(batch=batch).exists()


def test_close_records_a_buyer_link_and_closing_notes(
    auth, owner, staffed_farm, batch
):
    from partners.models import FarmPartnerLink

    buyer = FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=owner,  # any user; only the link_type/farm matter here
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        business_name="Bautista Dealers",
        linked_by=owner,
    )
    _sell(batch, birds=960, weight="1900.00")

    response = auth(owner).post(
        _close_url(staffed_farm, batch),
        {"buyer_link": buyer.id, "closing_notes": "Sold to the usual dealer."},
        format="json",
    )
    assert response.status_code == 201, response.data

    harvest = Harvest.objects.get(batch=batch)
    assert harvest.buyer_link_id == buyer.id
    assert "Sold to the usual dealer." in harvest.closing_notes


def test_closed_batch_rejects_new_daily_records(auth, owner, staffed_farm, batch):
    _sell(batch)
    assert (
        auth(owner)
        .post(_close_url(staffed_farm, batch), {}, format="json")
        .status_code
        == 201
    )

    response = auth(owner).post(
        f"/api/farms/{staffed_farm.id}/batches/{batch.id}/daily-records/",
        {
            "record_date": date(2026, 1, 6).isoformat(),
            "mortality_disease": 1,
            "feed_kg": "10.00",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "closed" in str(response.data).lower()


def test_worker_cannot_close(auth, worker, staffed_farm, batch):
    _sell(batch)
    response = auth(worker).post(_close_url(staffed_farm, batch), {}, format="json")
    assert response.status_code == 403
    assert not Harvest.objects.filter(batch=batch).exists()


def test_double_close_is_refused(auth, owner, staffed_farm, batch):
    _sell(batch)
    auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
    again = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
    assert again.status_code == 400
    assert Harvest.objects.filter(batch=batch).count() == 1
