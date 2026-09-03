# production/tests/test_feed_sacks.py
"""
Feed recorded in sacks.

Workers count physical sacks; the system stores and computes in kilograms.
The design stores BOTH — the sack count and the computed weight — so the
trail reads "5 sacks x 50 kg = 250 kg" permanently and editing an item's
sack weight cannot rewrite history.

The server never recomputes feed_kg from sacks (the item's kg_per_unit may
have changed between an offline entry and its sync). It only checks the
supplied feed_kg agrees with sacks x kg_per_unit within a small tolerance,
so a client bug cannot write a nonsense weight.

The last test is the point of the whole design: FCR must be identical
whether a batch's records were entered in kg or via sacks.
"""
import uuid
from datetime import date

import pytest
from decimal import Decimal

from analytics.services import fcr_by_batch
from production.models import Batch, DailyRecord, Harvest, House, InventoryItem

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="DR-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


@pytest.fixture
def feed_item(farm, owner):
    return InventoryItem.objects.create(
        farm=farm,
        name="Broiler feed",
        unit="sack",
        kg_per_unit=Decimal("50"),
        created_by=owner,
    )


@pytest.fixture
def weightless_item(farm, owner):
    return InventoryItem.objects.create(
        farm=farm,
        name="Disinfectant",
        unit="litre",
        kg_per_unit=None,
        created_by=owner,
    )


def _url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/"


def _bulk_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/bulk-sync/"


def _payload(record_date, **overrides):
    body = {
        "record_date": record_date,
        "mortality_disease": 0,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": "10.00",
        "notes": "",
    }
    body.update(overrides)
    return body


class TestSackEntry:
    def test_sacks_with_valid_item_stores_all_three(
        self, auth, owner, farm, batch, feed_item
    ):
        response = auth(owner).post(
            _url(farm, batch),
            _payload(
                "2026-01-05",
                feed_sacks="5",
                feed_item=feed_item.id,
                feed_kg="250.00",
            ),
            format="json",
        )
        assert response.status_code == 201, response.data

        record = DailyRecord.objects.get(pk=response.data["id"])
        assert str(record.feed_kg) == "250.00"
        assert str(record.feed_sacks) == "5.00"
        assert record.feed_item_id == feed_item.id
        assert response.data["feed_item_name"] == "Broiler feed"

    def test_sacks_without_item_is_rejected(self, auth, owner, farm, batch):
        response = auth(owner).post(
            _url(farm, batch),
            _payload("2026-01-05", feed_sacks="5", feed_kg="250.00"),
            format="json",
        )
        assert response.status_code == 400
        assert "feed_item" in response.data

    def test_item_without_kg_per_unit_is_rejected(
        self, auth, owner, farm, batch, weightless_item
    ):
        response = auth(owner).post(
            _url(farm, batch),
            _payload(
                "2026-01-05",
                feed_sacks="5",
                feed_item=weightless_item.id,
                feed_kg="250.00",
            ),
            format="json",
        )
        assert response.status_code == 400
        assert "sack weight" in str(response.data["feed_item"]).lower()

    def test_item_from_another_farm_is_rejected(
        self, auth, owner, farm, batch, rival_farm, rival_owner
    ):
        other = InventoryItem.objects.create(
            farm=rival_farm,
            name="Their feed",
            unit="sack",
            kg_per_unit=Decimal("50"),
            created_by=rival_owner,
        )
        response = auth(owner).post(
            _url(farm, batch),
            _payload(
                "2026-01-05",
                feed_sacks="5",
                feed_item=other.id,
                feed_kg="250.00",
            ),
            format="json",
        )
        assert response.status_code == 400
        assert "another farm" in str(response.data["feed_item"]).lower()

    def test_feed_kg_not_matching_sacks_is_rejected(
        self, auth, owner, farm, batch, feed_item
    ):
        # 5 x 50 = 250, not 300.
        response = auth(owner).post(
            _url(farm, batch),
            _payload(
                "2026-01-05",
                feed_sacks="5",
                feed_item=feed_item.id,
                feed_kg="300.00",
            ),
            format="json",
        )
        assert response.status_code == 400
        assert "does not add up" in str(response.data["feed_kg"]).lower()

    def test_feed_kg_within_tolerance_is_accepted(
        self, auth, owner, farm, batch, feed_item
    ):
        # 5 x 50 = 250; 250.40 is inside the tolerance (max of 0.5 kg / 1%).
        response = auth(owner).post(
            _url(farm, batch),
            _payload(
                "2026-01-06",
                feed_sacks="5",
                feed_item=feed_item.id,
                feed_kg="250.40",
            ),
            format="json",
        )
        assert response.status_code == 201, response.data

    def test_feed_kg_alone_with_no_sacks_still_works(
        self, auth, owner, farm, batch
    ):
        response = auth(owner).post(
            _url(farm, batch),
            _payload("2026-01-05", feed_kg="123.45"),
            format="json",
        )
        assert response.status_code == 201, response.data
        record = DailyRecord.objects.get(pk=response.data["id"])
        assert str(record.feed_kg) == "123.45"
        assert record.feed_sacks is None
        assert record.feed_item_id is None

    def test_bulk_sync_reports_a_mismatch_and_keeps_good_rows(
        self, auth, owner, farm, batch, feed_item
    ):
        good_id, bad_id = str(uuid.uuid4()), str(uuid.uuid4())
        response = auth(owner).post(
            _bulk_url(farm, batch),
            {
                "records": [
                    {
                        "id": good_id,
                        **_payload(
                            "2026-01-05",
                            feed_sacks="4",
                            feed_item=feed_item.id,
                            feed_kg="200.00",
                        ),
                    },
                    {
                        "id": bad_id,
                        **_payload(
                            "2026-01-06",
                            feed_sacks="5",
                            feed_item=feed_item.id,
                            feed_kg="999.00",
                        ),
                    },
                ]
            },
            format="json",
        )
        # Eager child validation: the whole request is a 400 naming the bad
        # row, and neither row is written.
        assert response.status_code == 400, response.data
        body = str(response.data)
        assert "does not add up" in body.lower()
        assert not DailyRecord.objects.filter(pk__in=[good_id, bad_id]).exists()


class TestFcrUnchangedBySacks:
    """The whole reason for storing both values."""

    def _harvested_batch(self, auth, owner, farm, code, house_name, use_sacks, feed_item):
        house = House.objects.create(farm=farm, name=house_name, capacity=5000)
        batch = Batch.objects.create(
            house=house,
            batch_code=code,
            initial_bird_count=1000,
            start_date=date(2026, 1, 1),
            created_by=owner,
        )
        body = _payload("2026-01-02", feed_kg="250.00")
        if use_sacks:
            body.update(feed_sacks="5", feed_item=feed_item.id)

        response = auth(owner).post(
            f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/",
            body,
            format="json",
        )
        assert response.status_code == 201, response.data

        Harvest.objects.create(
            batch=batch,
            harvest_date=date(2026, 2, 12),
            birds_harvested=950,
            total_weight_kg=Decimal("150.00"),
            recorded_by=owner,
        )
        batch.status = Batch.Status.HARVESTED
        batch.save(update_fields=["status"])
        return batch

    def test_fcr_identical_kg_vs_sacks(self, auth, owner, farm, feed_item):
        self._harvested_batch(
            auth, owner, farm, "KG-1", "House KG", use_sacks=False, feed_item=feed_item
        )
        self._harvested_batch(
            auth, owner, farm, "SACK-1", "House SACK", use_sacks=True, feed_item=feed_item
        )

        rows = {r["batch_code"]: r for r in fcr_by_batch(farm)["rows"]}
        # 250 kg feed / 150 kg live weight = 1.667, whichever way it was entered.
        assert rows["KG-1"]["total_feed_kg"] == "250.00"
        assert rows["SACK-1"]["total_feed_kg"] == "250.00"
        assert rows["KG-1"]["fcr"] == rows["SACK-1"]["fcr"] == "1.667"
