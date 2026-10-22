# production/tests/test_sync_policy.py
"""
docs/SYNC_POLICY.md, the rules no other test proved.

Each class is one row of that document. Rules already proven elsewhere
(the client UUID, the 24-hour lock, closed batches, house scope at sync
time) are pointed to from the document instead of being repeated here.
The last class keeps the document's list of uniqueness constraints
complete: a constraint added to a model without a row in the document
fails it.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps
from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from farms.models import Farm
from farms.tests.two_farms import FAST_HASHERS, make_user
from partners.models import FarmPartnerLink
from production.models import (
    Batch,
    DailyRecord,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
)

pytestmark = pytest.mark.django_db

SYNC_POLICY = settings.BASE_DIR / "docs" / "SYNC_POLICY.md"


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def world():
    owner = make_user("+639177000001", "Policy Owner", User.Role.OWNER)
    farm = Farm.objects.create(name="Policy Farm", owner=owner)
    houses = [House.objects.create(farm=farm, name=f"House {n}", capacity=5000) for n in (1, 2)]
    batches = [
        Batch.objects.create(
            house=house,
            batch_code=f"POL-{n}",
            initial_bird_count=1000,
            start_date=timezone.localdate() - timedelta(days=10),
            created_by=owner,
        )
        for n, house in enumerate(houses, start=1)
    ]
    client = APIClient()
    client.force_authenticate(user=owner)
    return {"owner": owner, "farm": farm, "houses": houses, "batches": batches, "client": client}


def _sync_url(world, batch):
    return reverse("production:daily-bulk-sync", kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk})


def _day(n_back, deaths, record_id=None):
    return {
        "id": str(record_id or uuid.uuid4()),
        "record_date": (timezone.localdate() - timedelta(days=n_back)).isoformat(),
        "mortality_disease": deaths,
        "mortality_heat": 0,
        "mortality_culled": 0,
        "mortality_unknown": 0,
        "feed_kg": f"{30 + n_back}.00",
    }


class TestTheDayIsFixed:
    """
    Same id = the same record, about the same day. An update may change the
    numbers, never which day they describe (DailyRecordSerializer.update:
    "the day itself is fixed"), on every path a re-send can take.
    """

    def _record(self, world):
        batch = world["batches"][0]
        day = _day(3, 2)
        response = world["client"].post(_sync_url(world, batch), {"records": [day]}, format="json")
        assert response.status_code == 200, response.data
        return batch, day, DailyRecord.objects.get(pk=day["id"])

    def test_patch(self, world):
        batch, day, record = self._record(world)
        url = reverse("production:daily-detail", kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk, "pk": record.pk})
        world["client"].patch(url, {"record_date": _day(1, 0)["record_date"], "mortality_disease": 4}, format="json")
        record.refresh_from_db()
        assert record.record_date.isoformat() == day["record_date"]

    def test_retried_post(self, world):
        batch, day, record = self._record(world)
        url = reverse("production:daily-list", kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk})
        world["client"].post(url, day | {"record_date": _day(1, 0)["record_date"]}, format="json")
        record.refresh_from_db()
        assert record.record_date.isoformat() == day["record_date"]

    def test_bulk_resync(self, world):
        batch, day, record = self._record(world)
        world["client"].post(
            _sync_url(world, batch),
            {"records": [day | {"record_date": _day(1, 0)["record_date"]}]},
            format="json",
        )
        record.refresh_from_db()
        assert record.record_date.isoformat() == day["record_date"], "a re-sync moved the record to another day"

class TestBulkSyncLimits:
    def test_more_than_60_records_is_refused_whole(self, world):
        batch = world["batches"][0]
        days = [_day(0, 0) for _ in range(61)]
        for n, day in enumerate(days):
            day["record_date"] = (batch.start_date + timedelta(days=n % 10)).isoformat()
        response = world["client"].post(_sync_url(world, batch), {"records": days}, format="json")
        assert response.status_code == 400
        assert not DailyRecord.objects.exists()

    def test_two_entries_for_one_date_in_one_payload_is_refused_whole(self, world):
        batch = world["batches"][0]
        response = world["client"].post(
            _sync_url(world, batch), {"records": [_day(1, 1), _day(1, 2), _day(2, 1)]}, format="json"
        )
        assert response.status_code == 400
        assert "same date" in str(response.data)
        assert not DailyRecord.objects.exists()


class TestArrivalOrderDoesNotMatter:
    """record_date decides which day a record is about; arrival order decides nothing."""

    def _series(self, world, batch):
        url = reverse(
            "analytics:batch-mortality",
            kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk},
        )
        response = world["client"].get(url)
        assert response.status_code == 200
        return response.data

    def test_forward_and_reverse_arrival_give_the_same_batch(self, world):
        forward, backward = world["batches"]
        days = [(5, 3), (4, 1), (3, 4), (2, 0), (1, 2)]

        # One batch gets the week in one request, oldest first; the other
        # gets the same days one request each, newest first, as a phone
        # that synced its newest entries before the old backlog would.
        response = world["client"].post(
            _sync_url(world, forward), {"records": [_day(n, d) for n, d in days]}, format="json"
        )
        assert response.status_code == 200, response.data
        for n, d in reversed(days):
            response = world["client"].post(
                _sync_url(world, backward), {"records": [_day(n, d)]}, format="json"
            )
            assert response.status_code == 200, response.data

        forward.refresh_from_db()
        backward.refresh_from_db()
        assert (forward.total_mortality, forward.total_feed_kg) == (
            backward.total_mortality,
            backward.total_feed_kg,
        ) == (10, Decimal("165.00"))

        def chart(batch):
            data = self._series(world, batch)
            return data["points"], data["summary"]

        assert chart(forward) == chart(backward)

    def test_a_late_record_lands_on_its_own_day(self, world):
        batch = world["batches"][0]
        client = world["client"]
        client.post(_sync_url(world, batch), {"records": [_day(1, 2)]}, format="json")
        client.post(_sync_url(world, batch), {"records": [_day(4, 7)]}, format="json")

        dates = [r.record_date for r in DailyRecord.objects.filter(batch=batch).order_by("record_date")]
        assert dates == [
            timezone.localdate() - timedelta(days=4),
            timezone.localdate() - timedelta(days=1),
        ]
        list_url = reverse(
            "production:daily-list", kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk}
        )
        listed = [row["record_date"] for row in client.get(list_url).data]
        assert listed == sorted(listed, reverse=True)


class TestServerTimeIsRecorded:
    def test_receipt_time_typed_time_and_the_gap_are_kept_apart(self, world):
        batch = world["batches"][0]
        typed = timezone.now() - timedelta(days=2)
        day = _day(2, 1) | {
            "recorded_at": typed.isoformat(),
            # Read-only on the server: a client cannot pick its own receipt
            # time, or the lock window that is measured from it.
            "created_at": (timezone.now() + timedelta(days=30)).isoformat(),
        }
        before = timezone.now()
        response = world["client"].post(_sync_url(world, batch), {"records": [day]}, format="json")
        assert response.status_code == 200, response.data

        record = DailyRecord.objects.get(pk=day["id"])
        assert before <= record.created_at <= timezone.now()
        assert abs((record.recorded_at - typed).total_seconds()) < 1
        assert record.sync_delay_seconds == pytest.approx((record.created_at - typed).total_seconds(), abs=1)
        assert record.sync_delay_seconds > 2 * 24 * 3600 - 60

    def test_the_lock_runs_from_receipt_not_from_the_typed_time(self, world):
        """Typed three days ago, received now: still editable for a day."""
        batch = world["batches"][0]
        day = _day(3, 1) | {"recorded_at": (timezone.now() - timedelta(days=3)).isoformat()}
        world["client"].post(_sync_url(world, batch), {"records": [day]}, format="json")
        assert DailyRecord.objects.get(pk=day["id"]).is_editable


class TestNothingIsDeleted:
    def test_houses_and_batches_have_no_delete(self, world):
        client, farm = world["client"], world["farm"]
        house, batch = world["houses"][0], world["batches"][0]
        house_url = reverse("production:house-detail", kwargs={"farm_pk": farm.pk, "pk": house.pk})
        batch_url = reverse("production:batch-detail", kwargs={"farm_pk": farm.pk, "pk": batch.pk})
        assert client.delete(house_url).status_code == 405
        assert client.delete(batch_url).status_code == 405
        assert House.objects.filter(pk=house.pk).exists()
        assert Batch.objects.filter(pk=batch.pk).exists()

    def test_ending_a_batch_and_retiring_its_house_keep_both_rows(self, world):
        client, farm = world["client"], world["farm"]
        house, batch = world["houses"][0], world["batches"][0]
        response = client.post(
            reverse("production:batch-terminate", kwargs={"farm_pk": farm.pk, "pk": batch.pk}),
            {"reason": "Wiped out by disease"},
            format="json",
        )
        assert response.status_code == 200
        response = client.post(reverse("production:house-archive", kwargs={"farm_pk": farm.pk, "pk": house.pk}))
        assert response.status_code == 200
        batch.refresh_from_db()
        house.refresh_from_db()
        assert batch.status == Batch.Status.TERMINATED
        assert house.is_archived

    def test_a_house_with_batch_history_cannot_be_deleted_even_in_the_database(self, world):
        with pytest.raises(models.ProtectedError):
            world["houses"][0].delete()


class TestAlertsUseServerDataOnly:
    def _alerts(self, world):
        url = reverse("analytics:alerts", kwargs={"farm_pk": world["farm"].pk})
        return {a["id"]: a for a in world["client"].get(url).data["alerts"]}

    def test_low_stock_comes_from_the_ledger_not_from_client_figures(self, world):
        item = InventoryItem.objects.create(
            farm=world["farm"], name="Vitamins", unit="sachet", low_stock_threshold=Decimal("10")
        )
        InventoryStockIn.objects.create(item=item, quantity=Decimal("12"), stock_in_date=timezone.localdate())
        usage_url = reverse("production:inventory-usage-bulk-sync", kwargs={"farm_pk": world["farm"].pk})
        response = world["client"].post(
            usage_url,
            {"records": [{
                "item": item.pk,
                "quantity_used": "5.00",
                "usage_date": timezone.localdate().isoformat(),
                # Derived on the server; whatever a client sends is ignored.
                "current_quantity": "500.00",
                "is_low": False,
            }]},
            format="json",
        )
        assert response.status_code == 200, response.data
        item_url = reverse("production:inventory-item-detail", kwargs={"farm_pk": world["farm"].pk, "pk": item.pk})
        world["client"].patch(item_url, {"current_quantity": "500.00", "is_low": False}, format="json")

        alert = self._alerts(world)[f"low-stock:{item.pk}"]
        assert alert["severity"] == "warning"
        assert "7" in alert["detail"]
        assert InventoryUsageLog.objects.get().quantity_used == Decimal("5.00")

    def test_mortality_alert_comes_from_the_cause_counts_not_a_client_total(self, world):
        batch = world["batches"][0]
        day = _day(1, 90) | {"mortality": 0}
        world["client"].post(_sync_url(world, batch), {"records": [day]}, format="json")
        batch.refresh_from_db()
        assert batch.total_mortality == 90
        assert self._alerts(world)[f"mortality:{batch.pk}"]["severity"] == "danger"



class TestDuplicatesAreAnsweredCleanly:
    """
    A second row that would break a uniqueness rule is refused with a
    readable 400, never an IntegrityError (500). One test per rule that the
    API can reach and no other test covered; the rest are cited in
    docs/SYNC_POLICY.md.
    """

    def _post(self, world, url, body):
        client = world["client"]
        client.raise_request_exception = False
        with transaction.atomic():
            return client.post(url, body, format="json")

    def test_farm_name_per_owner(self, world):
        response = self._post(world, reverse("farms:farm-list"), {"name": world["farm"].name})
        assert response.status_code == 400, response.status_code
        assert Farm.objects.filter(owner=world["owner"]).count() == 1

    def test_house_name_per_farm(self, world):
        url = reverse("production:house-list", kwargs={"farm_pk": world["farm"].pk})
        response = self._post(world, url, {"name": "house 1", "capacity": 10})
        assert response.status_code == 400
        assert "already exists" in str(response.data)

    def test_batch_code_per_house(self, world):
        house, batch = world["houses"][0], world["batches"][0]
        Batch.objects.filter(pk=batch.pk).update(status=Batch.Status.HARVESTED)
        url = reverse("production:batch-list", kwargs={"farm_pk": world["farm"].pk})
        response = self._post(world, url, {
            "house": house.pk,
            "batch_code": batch.batch_code,
            "initial_bird_count": 100,
            "start_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
        })
        assert response.status_code == 400
        assert Batch.objects.filter(house=house).count() == 1

    def test_weight_sample_per_batch_and_day(self, world):
        batch = world["batches"][0]
        url = reverse("production:weight-list", kwargs={"farm_pk": world["farm"].pk, "batch_pk": batch.pk})
        body = {"sample_date": timezone.localdate().isoformat(), "birds_weighed": 5, "average_grams": "500.00"}
        assert self._post(world, url, body | {"id": str(uuid.uuid4())}).status_code == 201
        response = self._post(world, url, body | {"id": str(uuid.uuid4())})
        assert response.status_code == 400, response.status_code
        assert batch.weight_samples.count() == 1

    def test_routine_item_name_per_farm(self, world):
        url = reverse("production:task-template-list", kwargs={"farm_pk": world["farm"].pk})
        assert self._post(world, url, {"name": "Water check"}).status_code == 201
        response = self._post(world, url, {"name": "water check"})
        assert response.status_code == 400
        assert "already exists" in str(response.data)

    def test_buyer_link_per_farm(self, world):
        buyer = make_user("+639177000009", "Linked Buyer", User.Role.CONSUMER)
        FarmPartnerLink.objects.create(
            farm=world["farm"], partner=buyer, link_type=FarmPartnerLink.LinkType.CONSUMER
        )
        url = reverse("partners:farm-partners", kwargs={"farm_pk": world["farm"].pk})
        response = self._post(world, url, {
            "phone_number": buyer.phone_number,
            "full_name": buyer.full_name,
            "account_role": "CONSUMER",
        })
        assert response.status_code == 400
        assert "already linked" in str(response.data)

def _uniqueness_rules():
    """Every uniqueness rule in this project's models: constraints and unique fields."""
    local = {"accounts", "farms", "partners", "production", "analytics", "notifications"}
    rules = []
    for model in apps.get_models():
        if model._meta.app_label not in local or model._meta.proxy:
            continue
        for constraint in model._meta.constraints:
            if isinstance(constraint, models.UniqueConstraint):
                rules.append(constraint.name)
        for field in model._meta.local_fields:
            if field.unique and not field.primary_key:
                rules.append(f"{model.__name__}.{field.name}")
        for fields in model._meta.unique_together:
            rules.append(f"{model.__name__}.unique_together{tuple(fields)}")
    return rules


class TestUniquenessListIsComplete:
    def test_every_uniqueness_rule_is_in_the_sync_policy(self):
        text = SYNC_POLICY.read_text(encoding="utf-8")
        missing = [rule for rule in _uniqueness_rules() if f"`{rule}`" not in text]
        assert not missing, f"docs/SYNC_POLICY.md does not list: {missing}"

    def test_the_walk_found_the_constraints_it_should(self):
        rules = _uniqueness_rules()
        assert "unique_daily_record_per_batch" in rules
        assert "one_active_batch_per_house" in rules
        assert "Harvest.batch" in rules
