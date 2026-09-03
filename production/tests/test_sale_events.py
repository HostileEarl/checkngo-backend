# production/tests/test_sale_events.py
"""
Incremental sale events.

A batch of thousands does not leave in one harvest. A buyer orders 60-100
dressed birds culled overnight; individuals buy single live birds at the
gate; the cohort empties over weeks across dozens of SaleEvent rows. Each
carries a count, a weight, and an optional revenue figure — nothing else.

The last test is the point of the redesign: a batch closed with a single
sale event equal to the old harvest must produce the identical FCR the
single-harvest model produced.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from analytics.services import fcr_by_batch
from production.models import Batch, DailyRecord, Harvest, House, SaleEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="Shed 1", capacity=5000)


@pytest.fixture
def batch(house, owner):
    b = Batch.objects.create(
        house=house,
        batch_code="S-1",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    # 40 birds lost over 4 days -> 960 remaining, 400 kg feed.
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


def _sales_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/"


def _bulk_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/sales/bulk-sync/"


def _close_url(farm, batch):
    return f"/api/farms/{farm.id}/batches/{batch.id}/close/"


def _payload(**overrides):
    body = {
        "sale_date": date(2026, 1, 10).isoformat(),
        "sale_type": SaleEvent.SaleType.DRESSED,
        "bird_count": 80,
        "total_weight_kg": "160.00",
        "revenue": "12000.00",
        "notes": "",
    }
    body.update(overrides)
    return body


def _age_out(sale):
    SaleEvent.objects.filter(pk=sale.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    sale.refresh_from_db()


class TestRecordingSales:
    def test_recording_a_sale_reduces_current_bird_count(
        self, auth, owner, staffed_farm, batch
    ):
        assert batch.current_bird_count == 960

        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(bird_count=100), format="json"
        )
        assert resp.status_code == 201, resp.data

        batch.refresh_from_db()
        assert batch.total_birds_sold == 100
        assert batch.current_bird_count == 860

    def test_selling_more_birds_than_remain_is_rejected(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=961, total_weight_kg="1900.00"),
            format="json",
        )
        assert resp.status_code == 400
        assert "bird_count" in resp.data

    def test_mortality_plus_sales_cannot_exceed_the_placed_count(
        self, auth, owner, staffed_farm, batch
    ):
        # 960 remain. Sell 900, leaving 60.
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=900, total_weight_kg="1800.00"),
            format="json",
        )
        batch.refresh_from_db()
        assert batch.current_bird_count == 60

        # A further sale of 61 would push mortality (40) + sales (961) past
        # the 1000 placed.
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 11).isoformat(),
                bird_count=61,
                total_weight_kg="120.00",
            ),
            format="json",
        )
        assert resp.status_code == 400
        assert "bird_count" in resp.data

    def test_several_sales_on_the_same_date_are_allowed(
        self, auth, owner, staffed_farm, batch
    ):
        for _ in range(3):
            resp = auth(owner).post(
                _sales_url(staffed_farm, batch),
                _payload(bird_count=50, total_weight_kg="100.00"),
                format="json",
            )
            assert resp.status_code == 201, resp.data

        assert SaleEvent.objects.filter(batch=batch).count() == 3
        batch.refresh_from_db()
        assert batch.total_birds_sold == 150

    def test_a_worker_gets_403_on_create(self, auth, worker, staffed_farm, batch):
        resp = auth(worker).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        assert resp.status_code == 403
        assert not SaleEvent.objects.filter(batch=batch).exists()

    def test_manager_can_record_a_sale(self, auth, manager, staffed_farm, batch):
        resp = auth(manager).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        assert resp.status_code == 201, resp.data


class TestIdempotencyAndLock:
    def test_a_duplicate_id_updates_rather_than_erroring(
        self, auth, owner, staffed_farm, batch
    ):
        rid = str(uuid.uuid4())
        first = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(id=rid, bird_count=80),
            format="json",
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(id=rid, bird_count=90, total_weight_kg="180.00"),
            format="json",
        )
        assert second.status_code == 200, second.data
        assert SaleEvent.objects.filter(batch=batch).count() == 1
        sale = SaleEvent.objects.get(pk=rid)
        assert sale.bird_count == 90
        batch.refresh_from_db()
        assert batch.total_birds_sold == 90

    def test_a_sale_older_than_24_hours_cannot_be_edited(
        self, auth, owner, staffed_farm, batch
    ):
        create = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        sale = SaleEvent.objects.get(pk=create.data["id"])
        _age_out(sale)

        resp = auth(owner).patch(
            f"{_sales_url(staffed_farm, batch)}{sale.pk}/",
            {"bird_count": 5},
            format="json",
        )
        assert resp.status_code == 400
        assert "locked" in str(resp.data).lower()


class TestBulkSync:
    def test_bulk_sync_reports_per_record_outcomes(
        self, auth, owner, staffed_farm, batch
    ):
        good_id, bad_id = str(uuid.uuid4()), str(uuid.uuid4())
        resp = auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {
                "records": [
                    {"id": good_id, **_payload(bird_count=100, total_weight_kg="200.00")},
                    {
                        "id": bad_id,
                        **_payload(
                            sale_date=date(2026, 1, 11).isoformat(),
                            bird_count=5000,
                            total_weight_kg="9000.00",
                        ),
                    },
                ]
            },
            format="json",
        )
        # Eager child validation: bird_count 5000 > remaining fails the whole
        # request with a 400 naming the bad row.
        assert resp.status_code == 400, resp.data
        assert "bird_count" in str(resp.data)
        assert not SaleEvent.objects.filter(pk__in=[good_id, bad_id]).exists()

    def test_bulk_sync_is_idempotent(self, auth, owner, staffed_farm, batch):
        rid = str(uuid.uuid4())
        rec = {"id": rid, **_payload(bird_count=120, total_weight_kg="240.00")}

        first = auth(owner).post(
            _bulk_url(staffed_farm, batch), {"records": [rec]}, format="json"
        )
        assert first.status_code == 200, first.data
        assert first.data["created"] == [rid]

        second = auth(owner).post(
            _bulk_url(staffed_farm, batch), {"records": [rec]}, format="json"
        )
        assert second.status_code == 200, second.data
        assert second.data["updated"] == [rid]

        assert SaleEvent.objects.filter(batch=batch).count() == 1
        batch.refresh_from_db()
        assert batch.total_birds_sold == 120


class TestClosing:
    def test_closing_a_batch_with_no_sale_events_is_refused(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 400
        assert "no sale events" in str(resp.data).lower()
        batch.refresh_from_db()
        assert batch.status == Batch.Status.ACTIVE

    def test_closing_populates_harvest_totals_from_the_summed_sales(
        self, auth, owner, staffed_farm, batch
    ):
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=500, total_weight_kg="1000.00", revenue="70000.00"),
            format="json",
        )
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 12).isoformat(),
                sale_type=SaleEvent.SaleType.LIVE,
                bird_count=400,
                total_weight_kg="760.00",
                revenue="45000.00",
            ),
            format="json",
        )

        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 201, resp.data

        harvest = Harvest.objects.get(batch=batch)
        assert harvest.birds_harvested == 900
        assert harvest.total_weight_kg == Decimal("1760.00")
        assert harvest.revenue == Decimal("115000.00")
        # 60 birds never sold -> recorded, not blocked.
        assert "60 birds unaccounted for at closing." in harvest.closing_notes

    def test_closing_frees_the_house(self, auth, owner, staffed_farm, house, batch):
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=900, total_weight_kg="1800.00"),
            format="json",
        )
        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 201, resp.data

        batch.refresh_from_db()
        assert batch.status == Batch.Status.HARVESTED

        replacement = Batch.objects.create(
            house=house,
            batch_code="S-2",
            initial_bird_count=1000,
            start_date=date(2026, 2, 1),
            created_by=owner,
        )
        assert replacement.status == Batch.Status.ACTIVE

    def test_a_closed_batch_refuses_new_sales(
        self, auth, owner, staffed_farm, batch
    ):
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=900, total_weight_kg="1800.00"),
            format="json",
        )
        auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")

        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 20).isoformat(),
                bird_count=10,
                total_weight_kg="20.00",
            ),
            format="json",
        )
        assert resp.status_code == 400
        assert "closed" in str(resp.data).lower()

    def test_a_worker_cannot_close(self, auth, worker, staffed_farm, batch):
        SaleEvent.objects.create(
            batch=batch,
            sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED,
            bird_count=900,
            total_weight_kg=Decimal("1800.00"),
        )
        resp = auth(worker).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 403
        assert not Harvest.objects.filter(batch=batch).exists()


class TestFcrEquivalence:
    def test_fcr_for_one_sale_equals_the_old_single_harvest_model(
        self, auth, owner, staffed_farm, house, batch
    ):
        """
        The migration-equivalence guarantee: a batch closed with a single
        sale event whose count and weight equal the old harvest figures
        must yield the identical FCR the single-harvest model produced.

        batch has 400 kg feed. Old model: Harvest(total_weight_kg=800) ->
        FCR 400 / 800 = 0.500.
        """
        # --- New model: one sale event, then close ---
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=950, total_weight_kg="800.00", revenue="100000.00"),
            format="json",
        )
        auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")

        new_rows = {r["batch_code"]: r for r in fcr_by_batch(staffed_farm)["rows"]}
        assert new_rows["S-1"]["fcr"] == "0.500"
        assert new_rows["S-1"]["total_weight_kg"] == "800.00"
        assert new_rows["S-1"]["birds_harvested"] == 950

        # --- Old model, reconstructed directly on a parallel batch ---
        house2 = House.objects.create(
            farm=staffed_farm, name="Shed OLD", capacity=5000
        )
        old = Batch.objects.create(
            house=house2,
            batch_code="OLD-1",
            initial_bird_count=1000,
            start_date=date(2026, 1, 1),
            created_by=owner,
        )
        for day in range(1, 5):
            DailyRecord.objects.create(
                batch=old,
                record_date=date(2026, 1, day),
                mortality_disease=10,
                feed_kg=Decimal("100.00"),
                recorded_by=owner,
            )
        old.refresh_from_db()
        Harvest.objects.create(
            batch=old,
            harvest_date=date(2026, 2, 10),
            birds_harvested=950,
            total_weight_kg=Decimal("800.00"),
            revenue=Decimal("100000.00"),
            recorded_by=owner,
        )
        old.status = Batch.Status.HARVESTED
        old.save(update_fields=["status"])

        old_rows = {r["batch_code"]: r for r in fcr_by_batch(staffed_farm)["rows"]}
        assert old_rows["OLD-1"]["fcr"] == new_rows["S-1"]["fcr"] == "0.500"
