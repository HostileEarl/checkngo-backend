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
from partners.models import FarmPartnerLink
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
        "price_per_kg": "75.00",
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


class TestPriceAndRevenue:
    def test_price_per_kg_is_required_on_create(
        self, auth, owner, staffed_farm, batch
    ):
        body = _payload()
        del body["price_per_kg"]
        resp = auth(owner).post(_sales_url(staffed_farm, batch), body, format="json")
        assert resp.status_code == 400
        assert "price_per_kg" in resp.data

    def test_revenue_is_derived_from_price_times_weight_and_client_value_ignored(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                total_weight_kg="160.00",
                price_per_kg="75.00",
                revenue="1.00",  # ignored — revenue is read-only, server-derived
            ),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["revenue"] == "12000.00"

        sale = SaleEvent.objects.get(pk=resp.data["id"])
        assert sale.revenue == Decimal("12000.00")

    def test_legacy_rows_with_no_price_are_left_alone(self, batch):
        sale = SaleEvent.objects.create(
            batch=batch,
            sale_date=date(2026, 1, 10),
            sale_type=SaleEvent.SaleType.DRESSED,
            bird_count=80,
            total_weight_kg=Decimal("160.00"),
        )
        assert sale.price_per_kg is None
        assert sale.revenue is None


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
            _payload(bird_count=500, total_weight_kg="1000.00", price_per_kg="70.00"),
            format="json",
        )
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 12).isoformat(),
                sale_type=SaleEvent.SaleType.LIVE,
                bird_count=400,
                total_weight_kg="760.00",
                price_per_kg="45.00",
            ),
            format="json",
        )

        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 201, resp.data

        harvest = Harvest.objects.get(batch=batch)
        assert harvest.birds_harvested == 900
        assert harvest.total_weight_kg == Decimal("1760.00")
        # 1000kg @ 70/kg = 70000, 760kg @ 45/kg = 34200.
        assert harvest.revenue == Decimal("104200.00")
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
            _payload(bird_count=950, total_weight_kg="800.00", price_per_kg="125.00"),
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


# ─────────────────────────────────────────────────────────────
# Part B: buyer_link on SaleEvent
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def link(db, staffed_farm, consumer, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


@pytest.fixture
def rival_link(db, rival_farm, consumer, rival_owner):
    """Same buyer, but linked to a different farm."""
    return FarmPartnerLink.objects.create(
        farm=rival_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=rival_owner,
    )


class TestSaleBuyerLink:
    def test_owner_can_assign_a_buyer(self, auth, owner, staffed_farm, batch, link):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_link"] == link.pk

    def test_manager_can_assign_a_buyer(
        self, auth, manager, staffed_farm, batch, link
    ):
        resp = auth(manager).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 201, resp.data

    def test_worker_cannot_assign_a_buyer(
        self, auth, worker, staffed_farm, batch, link
    ):
        # A worker cannot record a sale at all (CanManageBatches), so this
        # is refused before buyer_link is ever considered.
        resp = auth(worker).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 403

    def test_a_sale_with_no_buyer_is_accepted(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["buyer_link"] is None

    def test_a_buyer_linked_to_another_farm_is_rejected(
        self, auth, owner, staffed_farm, batch, rival_link
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(buyer_link=rival_link.pk),
            format="json",
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_an_inactive_link_is_rejected(
        self, auth, owner, staffed_farm, batch, link
    ):
        link.deactivate()
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(buyer_link=link.pk), format="json"
        )
        assert resp.status_code == 400
        assert "buyer_link" in resp.data

    def test_owner_can_change_buyer_link_within_the_24_hour_window(
        self, auth, owner, staffed_farm, batch, link
    ):
        created = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        assert created.data["buyer_link"] is None

        resp = auth(owner).patch(
            f"{_sales_url(staffed_farm, batch)}{created.data['id']}/",
            {"buyer_link": link.pk},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["buyer_link"] == link.pk

    def test_changing_buyer_link_past_24_hours_is_locked_same_as_any_other_edit(
        self, auth, owner, staffed_farm, batch, link
    ):
        created = auth(owner).post(
            _sales_url(staffed_farm, batch), _payload(), format="json"
        )
        sale = SaleEvent.objects.get(pk=created.data["id"])
        _age_out(sale)

        resp = auth(owner).patch(
            f"{_sales_url(staffed_farm, batch)}{sale.pk}/",
            {"buyer_link": link.pk},
            format="json",
        )
        assert resp.status_code == 400
        assert "locked" in str(resp.data).lower()
        sale.refresh_from_db()
        assert sale.buyer_link is None

    def test_a_worker_cannot_set_buyer_link_via_bulk_sync(
        self, auth, worker, staffed_farm, batch, link
    ):
        """
        A worker's offline sale payload is the bulk-sync endpoint — same
        CanManageBatches permission as the plain create/update views, so a
        buyer_link in that payload is refused along with the entire
        request, never silently dropped and never written.
        """
        rid = str(uuid.uuid4())
        resp = auth(worker).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **_payload(buyer_link=link.pk)}]},
            format="json",
        )
        assert resp.status_code == 403
        assert not SaleEvent.objects.filter(pk=rid).exists()

    def test_owner_can_set_buyer_link_via_bulk_sync(
        self, auth, owner, staffed_farm, batch, link
    ):
        rid = str(uuid.uuid4())
        resp = auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **_payload(buyer_link=link.pk)}]},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["created"] == [rid]
        assert SaleEvent.objects.get(pk=rid).buyer_link_id == link.pk

    def test_bulk_sync_cannot_change_buyer_link_on_a_locked_row(
        self, auth, owner, staffed_farm, batch, link
    ):
        rid = str(uuid.uuid4())
        auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **_payload()}]},
            format="json",
        )
        sale = SaleEvent.objects.get(pk=rid)
        _age_out(sale)

        resp = auth(owner).post(
            _bulk_url(staffed_farm, batch),
            {"records": [{"id": rid, **_payload(buyer_link=link.pk)}]},
            format="json",
        )
        # 207: the one record in this batch failed (locked).
        assert resp.status_code == 207, resp.data
        assert resp.data["failed"] and resp.data["failed"][0]["id"] == rid
        sale.refresh_from_db()
        assert sale.buyer_link is None


# ─────────────────────────────────────────────────────────────
# Part C: OTHERS sale type
# ─────────────────────────────────────────────────────────────


class TestOthersSaleType:
    def test_others_requires_part_name(self, auth, owner, staffed_farm, batch):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_type=SaleEvent.SaleType.OTHERS,
                total_weight_kg="5.00",
                price_per_kg="120.00",
            ),
            format="json",
        )
        assert resp.status_code == 400
        assert "part_name" in resp.data

    def test_others_part_name_is_trimmed(self, auth, owner, staffed_farm, batch):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_type=SaleEvent.SaleType.OTHERS,
                part_name="  Feet  ",
                total_weight_kg="5.00",
                price_per_kg="120.00",
            ),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["part_name"] == "Feet"

    def test_others_bird_count_defaults_to_zero(
        self, auth, owner, staffed_farm, batch
    ):
        body = _payload(
            sale_type=SaleEvent.SaleType.OTHERS,
            part_name="Gizzards",
            total_weight_kg="5.00",
            price_per_kg="120.00",
        )
        del body["bird_count"]  # omitted entirely, as a real client would
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch), body, format="json"
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["bird_count"] == 0

    def test_part_name_rejected_for_non_others_sale(
        self, auth, owner, staffed_farm, batch
    ):
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(part_name="Feet"),
            format="json",
        )
        assert resp.status_code == 400
        assert "part_name" in resp.data

    def test_others_does_not_count_toward_birds_sold_or_alive(
        self, auth, owner, staffed_farm, batch
    ):
        assert batch.current_bird_count == 960

        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_type=SaleEvent.SaleType.OTHERS,
                part_name="Feet",
                bird_count=50,  # supplied anyway — must still be excluded
                total_weight_kg="5.00",
                price_per_kg="120.00",
            ),
            format="json",
        )
        assert resp.status_code == 201, resp.data

        batch.refresh_from_db()
        assert batch.total_birds_sold == 0
        assert batch.current_bird_count == 960

    def test_others_exempt_from_more_than_flock_check(
        self, auth, owner, staffed_farm, batch
    ):
        """An OTHERS sale with an absurd bird_count still succeeds — the
        'more than flock' check only applies to DRESSED/LIVE sales."""
        resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_type=SaleEvent.SaleType.OTHERS,
                part_name="Feet",
                bird_count=999999,
                total_weight_kg="5.00",
                price_per_kg="120.00",
            ),
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_others_counts_in_revenue_and_sales_history_not_fcr(
        self, auth, owner, staffed_farm, batch
    ):
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=950, total_weight_kg="1900.00", price_per_kg="70.00"),
            format="json",
        )
        others_resp = auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 11).isoformat(),
                sale_type=SaleEvent.SaleType.OTHERS,
                part_name="Feet",
                total_weight_kg="20.00",
                price_per_kg="50.00",
            ),
            format="json",
        )
        assert others_resp.status_code == 201, others_resp.data

        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 201, resp.data

        harvest = Harvest.objects.get(batch=batch)
        # Live weight / birds exclude the OTHERS sale (FCR denominator).
        assert harvest.birds_harvested == 950
        assert harvest.total_weight_kg == Decimal("1900.00")
        # Revenue includes it: 950*70 (66500*... ) — 1900*70=133000, +20*50=1000.
        assert harvest.revenue == Decimal("134000.00")

    def test_tc25_fcr_and_survival_unaffected_by_an_others_sale(
        self, auth, owner, staffed_farm, house, batch
    ):
        """
        TC-25: 1000 placed, 50 lost (5.00% mortality), 900 live-weight
        birds sold (90.00% survival), 1500 kg feed / 900 kg weight sold =
        FCR 1.67 — unchanged by an extra OTHERS (parts) sale on the batch.
        """
        # batch already has 40 mortality / 400kg feed from days 1-4; add
        # one more day to reach 50 total mortality.
        DailyRecord.objects.create(
            batch=batch, record_date=date(2026, 1, 5),
            mortality_disease=10, feed_kg=Decimal("1100.00"), recorded_by=owner,
        )
        batch.refresh_from_db()
        assert batch.total_mortality == 50
        assert batch.total_feed_kg == Decimal("1500.00")

        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(bird_count=900, total_weight_kg="900.00", price_per_kg="70.00"),
            format="json",
        )
        auth(owner).post(
            _sales_url(staffed_farm, batch),
            _payload(
                sale_date=date(2026, 1, 11).isoformat(),
                sale_type=SaleEvent.SaleType.OTHERS,
                part_name="Gizzards",
                total_weight_kg="10.00",
                price_per_kg="50.00",
            ),
            format="json",
        )

        resp = auth(owner).post(_close_url(staffed_farm, batch), {}, format="json")
        assert resp.status_code == 201, resp.data
        batch.refresh_from_db()

        row = next(r for r in fcr_by_batch(staffed_farm)["rows"] if r["batch_code"] == "S-1")
        assert row["survival_rate_pct"] == "90.00"
        assert row["fcr"] == "1.667"
