# production/tests/test_evaluation_farms.py
"""
Evaluation farms (production/evaluation.py): one clean farm per guided
test session, created against the production database.

What is proved here:

- one call creates exactly one farm, four users, two houses, three
  batches, and the records, sales, payments, stock and routine described
  in the service docstring — and nothing else
- the four PINs log in through the real API and reach each role's
  screens with no PIN-change gate; the worker records today for House 1
  and is refused for House 2; the buyer sees the two sales recorded
  against them, without prices, plus their payments; the FCR chart
  carries the closed batch at 1.6-1.8
- no SMS, no Invitation; existing farms and users are untouched
- a taken session number or phone number creates nothing and says why
- the whole call is one transaction
- archiving touches only evaluation farms and their accounts, deletes
  nothing, and locks those accounts out
- one farm is created in under five seconds
"""
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import identify_hasher
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils import timezone

from accounts.models import Invitation, User
from analytics.alerts import compute_alerts
from analytics.services import feed_balance
from farms.models import Farm, FarmMembership, FarmOwnershipHistory
from notifications.models import SmsLog
from partners.models import FarmPartnerLink
from production.evaluation import (
    EVALUATION_FARM_PREFIX,
    EvaluationFarmError,
    archive_evaluation_farms,
    create_evaluation_farm,
    create_evaluation_farms,
    demo_phone_number,
    evaluation_farms,
    next_free_session_numbers,
    session_number_of,
)
from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    Payment,
    SaleEvent,
    TaskCompletion,
    TaskTemplate,
    WeightSample,
)

pytestmark = pytest.mark.django_db

# Every table an evaluation farm could write to, or must never write to.
COUNTED_MODELS = [
    Farm, User, FarmMembership, FarmOwnershipHistory, FarmPartnerLink, House,
    Batch, DailyRecord, WeightSample, SaleEvent, Payment, Harvest, FeedDelivery,
    InventoryItem, InventoryStockIn, InventoryUsageLog, TaskTemplate,
    TaskCompletion, Invitation, SmsLog,
]


def _counts():
    return {model.__name__: model.objects.count() for model in COUNTED_MODELS}


@pytest.fixture(autouse=True)
def _clear_throttles():
    # The login throttles count in the cache, which outlives each test.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def created():
    return create_evaluation_farm(3)


@pytest.fixture
def accounts(created):
    return {account.role: account for account in created.accounts}


def _login(api, account):
    response = api.post(
        reverse("accounts:login"),
        {"phone_number": account.phone_number, "pin": account.pin},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response


def _client_for(api, account):
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(api, account).data['access']}")
    return api


def _batches(farm):
    active = Batch.objects.filter(house__farm=farm, status=Batch.Status.ACTIVE)
    return (
        Batch.objects.get(house__farm=farm, status=Batch.Status.HARVESTED),
        active.get(house__name="House 1"),
        active.get(house__name="House 2"),
    )


class TestWhatOneCallCreates:
    def test_exact_counts(self, owner, staffed_farm):
        before = _counts()
        result = create_evaluation_farm(3)
        after = _counts()
        delta = {name: after[name] - before[name] for name in after}

        farm = result.farm
        records = 21 + 21 + 35
        assert delta == {
            "Farm": 1,
            "User": 4,
            "FarmMembership": 3,
            "FarmOwnershipHistory": 1,
            "FarmPartnerLink": 1,
            "House": 2,
            "Batch": 3,
            "DailyRecord": records,
            "WeightSample": 5 + 2 + 2,
            "SaleEvent": 4,
            "Payment": 3,
            "Harvest": 1,
            "FeedDelivery": 8,
            "InventoryItem": 5,
            # One per feed delivery, plus the disinfectant and vitamins.
            "InventoryStockIn": 8 + 2,
            # One per daily record's feed line, plus 8 supplies logs.
            "InventoryUsageLog": records + 8,
            "TaskTemplate": 4,
            "TaskCompletion": TaskCompletion.objects.filter(template__farm=farm).count(),
            "Invitation": 0,
            "SmsLog": 0,
        }
        assert delta["TaskCompletion"] > 100

    def test_farm_identity(self, created):
        farm = created.farm
        assert farm.name == "Demo Farm – Session 03"
        assert farm.name.startswith(EVALUATION_FARM_PREFIX)
        assert farm.is_active
        assert (farm.municipality, farm.province) == ("Atimonan", "Quezon")
        assert created.session_number == 3

    def test_four_accounts(self, created):
        expected = [
            ("Owner", "Owner – Session 03", "+639990300001", User.Role.OWNER),
            ("Manager", "Manager – Session 03", "+639990300002", User.Role.MANAGER),
            ("Worker", "Worker – Session 03", "+639990300003", User.Role.WORKER),
            ("Buyer", "Buyer – Session 03", "+639990300004", User.Role.CONSUMER),
        ]
        for account, (role, name, phone, user_role) in zip(created.accounts, expected):
            assert (account.role, account.full_name, account.phone_number) == (role, name, phone)
            user = User.objects.get(phone_number=phone)
            assert user.full_name == name
            assert user.role == user_role
            assert user.is_active and not user.is_staff and not user.is_superuser
            assert user.must_change_credential is False
            assert user.credential_changed_at is not None
            # A 6-digit PIN, stored only as a normal password hash.
            assert len(account.pin) == 6 and account.pin.isdigit()
            assert account.pin not in user.password
            assert identify_hasher(user.password).algorithm == "pbkdf2_sha256"
            assert user.check_password(account.pin)

    def test_pins_differ_between_accounts_and_farms(self):
        first, second = create_evaluation_farms(2)
        pins = [a.pin for a in first.accounts + second.accounts]
        # Random 6-digit PINs: eight of them colliding is astronomically
        # unlikely, so a repeat means they are not being drawn per account.
        assert len(set(pins)) == len(pins)

    def test_pins_come_from_generate_pin(self):
        with patch("production.evaluation.generate_pin", side_effect=lambda: "246810"):
            result = create_evaluation_farm(4)
        assert {a.pin for a in result.accounts} == {"246810"}

    def test_memberships_and_buyer_link(self, created, accounts):
        farm = created.farm
        roles = {
            m.user.phone_number: m.role
            for m in FarmMembership.objects.filter(farm=farm, is_active=True)
        }
        assert roles == {
            accounts["Owner"].phone_number: FarmMembership.Role.OWNER,
            accounts["Manager"].phone_number: FarmMembership.Role.MANAGER,
            accounts["Worker"].phone_number: FarmMembership.Role.WORKER,
        }
        worker_membership = FarmMembership.objects.get(
            farm=farm, user__phone_number=accounts["Worker"].phone_number
        )
        assert list(worker_membership.houses.values_list("name", flat=True)) == ["House 1"]

        link = FarmPartnerLink.objects.get(farm=farm)
        assert link.partner.phone_number == accounts["Buyer"].phone_number
        assert link.link_type == FarmPartnerLink.LinkType.CONSUMER
        assert link.is_active
        assert link.business_name == "Session 03 Buyer"

    def test_houses_and_batches(self, created):
        farm = created.farm
        assert set(House.objects.filter(farm=farm).values_list("name", flat=True)) == {
            "House 1", "House 2",
        }
        harvested, active1, active2 = _batches(farm)
        today = timezone.localdate()

        for batch, birds in ((active1, 1000), (active2, 800)):
            assert batch.initial_bird_count == birds
            assert batch.start_date == today - timedelta(days=21)
            assert batch.breed == "Ross 308"

        assert harvested.house.name == "House 1"
        assert harvested.initial_bird_count == 1000
        assert 55 <= (today - harvested.start_date).days <= 70
        assert harvested.harvest.harvest_date == harvested.start_date + timedelta(days=35)

    def test_daily_records_run_from_placement_to_yesterday(self, created):
        today = timezone.localdate()
        harvested, active1, active2 = _batches(created.farm)
        for batch in (active1, active2):
            dates = list(batch.daily_records.order_by("record_date").values_list("record_date", flat=True))
            expected = [batch.start_date + timedelta(days=n) for n in range(21)]
            assert dates == expected
            assert dates[-1] == today - timedelta(days=1)
        assert not DailyRecord.objects.filter(batch__house__farm=created.farm, record_date=today).exists()
        assert harvested.daily_records.count() == 35

    def test_mortality_is_two_to_four_percent(self, created):
        for batch in _batches(created.farm):
            assert Decimal("2") <= batch.mortality_rate <= Decimal("4"), batch.batch_code
            # Week one carries more losses than week three: the chick spike.
            week = lambda first: sum(
                r.mortality for r in batch.daily_records.filter(
                    record_date__gte=batch.start_date + timedelta(days=first),
                    record_date__lt=batch.start_date + timedelta(days=first + 7),
                )
            )
            assert week(0) > week(14)

    def test_feed_rises_with_age_and_follows_the_feed_stages(self, created):
        for batch in _batches(created.farm):
            records = list(batch.daily_records.order_by("record_date"))
            first_week = sum(r.feed_kg for r in records[:7])
            third_week = sum(r.feed_kg for r in records[14:21])
            assert third_week > first_week * 3
            for record in records:
                age = (record.record_date - batch.start_date).days
                expected = "STARTER" if age <= 10 else "BOOSTER" if age <= 23 else "FINISHER"
                assert record.feed_item.feed_type == expected
                # Recorded in sacks; kg derived from them, as the API does.
                assert record.feed_kg == record.feed_sacks * record.feed_item.kg_per_unit
                assert record.feed_sacks % Decimal("0.25") == 0

    def test_who_recorded_what(self, created, accounts):
        harvested, active1, active2 = _batches(created.farm)

        def share(batch, account):
            records = batch.daily_records.all()
            mine = records.filter(recorded_by__phone_number=account.phone_number).count()
            return mine / records.count()

        assert share(active1, accounts["Worker"]) > 0.7
        assert share(active2, accounts["Manager"]) > 0.7
        assert not active2.daily_records.filter(
            recorded_by__phone_number=accounts["Worker"].phone_number
        ).exists()
        noted = DailyRecord.objects.filter(batch__house__farm=created.farm).exclude(notes="")
        assert 5 <= noted.count() <= 20

    def test_records_are_dated_and_older_ones_locked(self, created):
        now = timezone.now()
        records = DailyRecord.objects.filter(batch__house__farm=created.farm)
        for record in records:
            assert record.created_at <= now
            assert record.created_at >= record.recorded_at
            assert record.recorded_at.date() >= record.record_date - timedelta(days=1)
            if record.created_at < now - timedelta(hours=24):
                assert not record.is_editable
        two_days_ago = timezone.localdate() - timedelta(days=2)
        assert not any(r.is_editable for r in records.filter(record_date__lte=two_days_ago))
        # The derived feed drawdowns carry their record's dates too.
        for log in InventoryUsageLog.objects.filter(daily_record__batch__house__farm=created.farm)[:10]:
            assert log.created_at == log.daily_record.created_at

    def test_harvested_batch_sales_payments_and_close(self, created, accounts):
        harvested, _, _ = _batches(created.farm)
        link = FarmPartnerLink.objects.get(farm=created.farm)
        sales = list(harvested.sale_events.order_by("sale_date", "sale_type"))
        assert len(sales) == 4

        buyer_sales = [s for s in sales if s.buyer_link_id == link.pk]
        assert len(buyer_sales) == 2
        walk_in = [s for s in sales if s.buyer_name == "Walk-in customer"]
        assert len(walk_in) == 1 and walk_in[0].buyer_link is None
        others = [s for s in sales if s.sale_type == SaleEvent.SaleType.OTHERS]
        assert len(others) == 1
        assert others[0].part_name == "Feet" and others[0].bird_count == 0
        for sale in sales:
            assert sale.price_per_kg and sale.revenue == (sale.price_per_kg * sale.total_weight_kg).quantize(Decimal("0.01"))

        payments = Payment.objects.filter(sale__batch=harvested)
        assert payments.count() == 3
        for sale in buyer_sales:
            assert payments.filter(sale=sale, reverses__isnull=True).count() == 1
        reversal = payments.get(reverses__isnull=False)
        assert reversal.amount == -reversal.reverses.amount
        assert all(p.buyer_link_id == link.pk for p in payments)

        harvest = harvested.harvest
        live = [s for s in sales if s.sale_type != SaleEvent.SaleType.OTHERS]
        assert harvested.status == Batch.Status.HARVESTED
        assert harvested.current_bird_count == 0
        assert harvest.birds_harvested == sum(s.bird_count for s in live)
        assert harvest.total_weight_kg == sum(s.total_weight_kg for s in live)
        assert harvest.revenue == sum(s.revenue for s in sales)
        assert Decimal("1.6") <= harvested.feed_conversion_ratio <= Decimal("1.8")

    def test_inventory_and_feed_stock(self, created):
        farm = created.farm
        feed = {i.feed_type: i for i in InventoryItem.objects.filter(farm=farm, is_feed=True)}
        assert set(feed) == {"STARTER", "BOOSTER", "FINISHER"}
        for item in feed.values():
            assert item.unit == "sack" and item.kg_per_unit == Decimal("50")
            assert item.current_quantity > item.low_stock_threshold

        deliveries = FeedDelivery.objects.filter(farm=farm)
        three_weeks_ago = timezone.localdate() - timedelta(days=23)
        assert deliveries.filter(delivery_date__gte=three_weeks_ago).count() >= 3
        for delivery in deliveries:
            assert delivery.inventory_item is not None
            assert delivery.feed_type == delivery.inventory_item.feed_type
            sacks = delivery.quantity_kg / Decimal("50")
            assert sacks == sacks.to_integral_value()  # whole sacks
            assert delivery.unit_cost and delivery.total_cost

        assert feed_balance(farm)["balance_kg"] > 0

        disinfectant = InventoryItem.objects.get(farm=farm, name="Disinfectant")
        vitamins = InventoryItem.objects.get(farm=farm, name="Vitamins")
        assert disinfectant.usage_logs.count() >= 3 and vitamins.usage_logs.count() >= 3
        assert not disinfectant.is_low
        assert vitamins.current_quantity < vitamins.low_stock_threshold
        assert vitamins.is_low

        low = [a["id"] for a in compute_alerts(farm) if a["id"].startswith("low-stock:")]
        assert low == [f"low-stock:{vitamins.pk}"]

    def test_routine_tasks(self, created):
        farm = created.farm
        templates = {t.name: t.suggested_time for t in TaskTemplate.objects.filter(farm=farm)}
        assert templates == {
            "Check water lines": "6:00 AM",
            "Clean feeders": "7:00 AM",
            "Check ventilation": "12:00 PM",
            "Evening headcount": "5:00 PM",
        }
        completions = TaskCompletion.objects.filter(template__farm=farm)
        assert completions.exists()
        assert not completions.filter(completion_date__gte=timezone.localdate()).exists()
        assert set(completions.values_list("house__name", flat=True)) == {"House 1", "House 2"}

    def test_the_today_alert_asks_for_todays_records(self, created):
        _, active1, active2 = _batches(created.farm)
        ids = {a["id"] for a in compute_alerts(created.farm)}
        assert {f"today:{active1.id}", f"today:{active2.id}"} <= ids

    def test_creates_in_under_five_seconds(self):
        started = time.monotonic()
        create_evaluation_farm(9)
        assert time.monotonic() - started < 5


class TestNoSmsNoInvitation:
    def test_no_sms_log_and_no_invitation(self, settings, fake_semaphore):
        # Even with SMS switched on, nothing is sent: accounts are direct.
        create_evaluation_farm(5)
        assert SmsLog.objects.count() == 0
        assert Invitation.objects.count() == 0
        assert fake_semaphore == {}


class TestExistingDataUntouched:
    def test_existing_farm_and_users_are_untouched(self, staffed_farm, owner, manager, worker, consumer):
        house = House.objects.create(farm=staffed_farm, name="House 1", capacity=500)
        batch = Batch.objects.create(
            house=house, batch_code="B-1", initial_bird_count=500,
            start_date=timezone.localdate() - timedelta(days=5), created_by=owner,
        )
        record = DailyRecord.objects.create(
            batch=batch, record_date=timezone.localdate(), mortality_disease=2,
            feed_kg=Decimal("20"), recorded_by=worker,
        )
        link = FarmPartnerLink.objects.create(
            farm=staffed_farm, partner=consumer,
            link_type=FarmPartnerLink.LinkType.CONSUMER, linked_by=owner,
        )

        def snapshot():
            staffed_farm.refresh_from_db()
            return {
                "farm": model_to_dict(staffed_farm),
                "farm_updated": staffed_farm.updated_at,
                "users": [
                    model_to_dict(User.objects.get(pk=u.pk), exclude=["groups", "user_permissions"])
                    for u in (owner, manager, worker, consumer)
                ],
                "memberships": sorted(
                    (m.user_id, m.role, m.is_active, list(m.houses.values_list("pk", flat=True)))
                    for m in staffed_farm.memberships.all()
                ),
                "house": model_to_dict(House.objects.get(pk=house.pk)),
                "batch": model_to_dict(Batch.objects.get(pk=batch.pk)),
                "record": model_to_dict(DailyRecord.objects.get(pk=record.pk)),
                "record_created": DailyRecord.objects.get(pk=record.pk).created_at,
                "link": model_to_dict(FarmPartnerLink.objects.get(pk=link.pk)),
            }

        before = snapshot()
        counts_before = _counts()

        result = create_evaluation_farm(1)

        assert snapshot() == before
        farm = result.farm
        own_rows = {
            "Farm": Farm.objects.filter(pk=farm.pk).count(),
            "User": 4,
            "House": House.objects.filter(farm=farm).count(),
            "Batch": Batch.objects.filter(house__farm=farm).count(),
            "DailyRecord": DailyRecord.objects.filter(batch__house__farm=farm).count(),
            "SaleEvent": SaleEvent.objects.filter(batch__house__farm=farm).count(),
        }
        counts_after = _counts()
        for name, own in own_rows.items():
            assert counts_after[name] - own == counts_before[name], name


class TestRefusals:
    def test_existing_session_number_creates_nothing_and_says_why(self):
        create_evaluation_farm(6)
        before = _counts()
        with pytest.raises(EvaluationFarmError) as exc:
            create_evaluation_farm(6)
        assert "Demo Farm – Session 06" in str(exc.value)
        assert "already exists" in str(exc.value)
        assert _counts() == before

    def test_existing_phone_number_creates_nothing_and_says_why(self):
        User.objects.create_user(
            "+639990700002", "135790", full_name="Someone Real", role=User.Role.WORKER,
        )
        before = _counts()
        with pytest.raises(EvaluationFarmError) as exc:
            create_evaluation_farm(7)
        assert "+639990700002" in str(exc.value)
        assert _counts() == before
        assert not Farm.objects.filter(name="Demo Farm – Session 07").exists()

    def test_a_farm_with_the_name_but_another_owner_still_blocks_the_number(self, owner):
        Farm.objects.create(name="Demo Farm – Session 08", owner=owner)
        with pytest.raises(EvaluationFarmError):
            create_evaluation_farm(8)
        assert User.objects.filter(phone_number__startswith="+6399908").count() == 0

    @pytest.mark.parametrize("number", [0, 100, -1, "3"])
    def test_out_of_range_session_numbers(self, number):
        with pytest.raises(EvaluationFarmError):
            create_evaluation_farm(number)
        assert Farm.objects.count() == 0

    @pytest.mark.parametrize("count", [0, 11])
    def test_count_must_be_one_to_ten(self, count):
        with pytest.raises(EvaluationFarmError):
            create_evaluation_farms(count)
        assert Farm.objects.count() == 0

    def test_one_transaction_a_failure_midway_leaves_nothing(self, staffed_farm):
        before = _counts()
        with patch(
            "production.evaluation._FarmHistory._routine_completions",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                create_evaluation_farms(2)
        assert _counts() == before

    def test_two_farms_in_one_call_are_one_transaction(self):
        calls = []
        real = create_evaluation_farms.__globals__["_create_one"]

        def second_fails(number, credentials, created_by):
            calls.append(number)
            if len(calls) == 2:
                raise RuntimeError("second farm failed")
            return real(number, credentials, created_by)

        with patch("production.evaluation._create_one", side_effect=second_fails):
            with pytest.raises(RuntimeError):
                create_evaluation_farms(2)
        assert calls == [1, 2]
        assert Farm.objects.count() == 0
        assert User.objects.count() == 0


class TestSessionNumbers:
    def test_next_free_skips_taken_names_and_numbers(self, owner):
        create_evaluation_farm(1)
        User.objects.create_user("+639990300004", "975310", full_name="Taken", role=User.Role.CONSUMER)
        assert next_free_session_numbers(3) == [2, 4, 5]

    def test_create_farms_uses_the_next_free_numbers(self):
        create_evaluation_farm(1)
        create_evaluation_farm(3)
        results = create_evaluation_farms(2)
        assert [r.session_number for r in results] == [2, 4]
        assert [r.farm.name for r in results] == ["Demo Farm – Session 02", "Demo Farm – Session 04"]

    def test_session_number_needs_the_reserved_owner(self, owner, created):
        lookalike = Farm.objects.create(name="Demo Farm – Session 12", owner=owner)
        assert session_number_of(lookalike) is None
        assert session_number_of(created.farm) == 3
        assert [f.pk for f in evaluation_farms()] == [created.farm.pk]


class TestRolesThroughTheApi:
    def test_every_pin_logs_in_without_the_gate(self, api, created):
        for account in created.accounts:
            response = _login(api, account)
            assert response.data["must_change_credential"] is False
            assert response.data["user"]["phone_number"] == account.phone_number

    def test_owner_screens(self, api, created, accounts):
        client = _client_for(api, accounts["Owner"])
        farm = created.farm.pk
        for url in (
            reverse("farms:farm-detail", args=[farm]),
            reverse("farms:farm-members", args=[farm]),
            reverse("analytics:dashboard", args=[farm]),
            reverse("analytics:profitability", args=[farm]),
            reverse("partners:farm-partners", args=[farm]),
            reverse("production:feed-stock", args=[farm]),
        ):
            assert client.get(url).status_code == 200, url

    def test_manager_screens(self, api, created, accounts):
        client = _client_for(api, accounts["Manager"])
        farm = created.farm.pk
        for url in (
            reverse("production:batch-list", args=[farm]),
            reverse("production:inventory-item-list", args=[farm]),
            reverse("production:task-template-list", args=[farm]),
            reverse("analytics:alerts", args=[farm]),
            reverse("analytics:report-fcr", args=[farm]),
        ):
            assert client.get(url).status_code == 200, url

    def test_worker_screens(self, api, created, accounts):
        client = _client_for(api, accounts["Worker"])
        farm = created.farm.pk
        for url in (
            reverse("production:batch-list", args=[farm]),
            reverse("production:house-list", args=[farm]),
            reverse("production:task-completion-list", args=[farm]),
            reverse("analytics:alerts", args=[farm]),
        ):
            assert client.get(url).status_code == 200, url
        # Money stays with the owner and manager.
        assert client.get(reverse("analytics:profitability", args=[farm])).status_code == 403

    def test_buyer_screens(self, api, created, accounts):
        client = _client_for(api, accounts["Buyer"])
        response = client.get(reverse("partners:my-partner-farms"))
        assert response.status_code == 200
        assert [row["farm"] for row in response.data] == [created.farm.pk]
        # A buyer holds no farm membership.
        assert client.get(reverse("production:batch-list", args=[created.farm.pk])).status_code == 403

    def test_worker_records_today_for_house_1_and_is_refused_for_house_2(self, api, created, accounts):
        client = _client_for(api, accounts["Worker"])
        farm = created.farm
        _, active1, active2 = _batches(farm)
        booster = InventoryItem.objects.get(farm=farm, feed_type="BOOSTER")
        today = timezone.localdate().isoformat()
        payload = {
            "record_date": today,
            "mortality_disease": 1,
            "mortality_heat": 0,
            "mortality_culled": 0,
            "mortality_unknown": 0,
            "feed_sacks": "2.75",
            "feed_item": booster.pk,
        }

        ok = client.post(
            reverse("production:daily-list", args=[farm.pk, active1.pk]), payload, format="json"
        )
        assert ok.status_code == 201, ok.data
        assert ok.data["feed_kg"] == "137.50"

        refused = client.post(
            reverse("production:daily-list", args=[farm.pk, active2.pk]), payload, format="json"
        )
        assert refused.status_code == 403
        assert "House 2" in str(refused.data)
        assert not active2.daily_records.filter(record_date=timezone.localdate()).exists()

    def test_buyer_sees_two_sales_without_prices_and_their_payments(self, api, created, accounts):
        client = _client_for(api, accounts["Buyer"])

        purchases = client.get(reverse("partners:my-purchases"))
        assert purchases.status_code == 200
        assert len(purchases.data) == 2
        for row in purchases.data:
            assert row["source"] == "sale"
            assert row["farm_name"] == "Demo Farm – Session 03"
            assert "revenue" not in row and "price_per_kg" not in row
            assert row["sale_type"] == SaleEvent.SaleType.LIVE

        payments = client.get(reverse("partners:my-payments"))
        assert payments.status_code == 200
        rows = payments.data["results"] if isinstance(payments.data, dict) else payments.data
        assert len(rows) == 3
        assert sorted(row["is_reversal"] for row in rows) == [False, False, True]
        assert all("revenue" not in row and "outstanding" not in row for row in rows)

    def test_fcr_report_includes_the_closed_batch(self, api, created, accounts):
        client = _client_for(api, accounts["Owner"])
        response = client.get(reverse("analytics:fcr", args=[created.farm.pk]))
        assert response.status_code == 200
        rows = response.data["rows"]
        assert len(rows) == 1
        assert rows[0]["cycle_days"] == 35
        assert Decimal("1.6") <= Decimal(rows[0]["fcr"]) <= Decimal("1.8")
        assert response.data["excluded"]["active_batches"] == 2

        csv = client.get(reverse("analytics:report-fcr", args=[created.farm.pk]))
        assert csv.status_code == 200
        assert rows[0]["batch_code"] in b"".join(csv.streaming_content if csv.streaming else [csv.content]).decode()


class TestArchive:
    def test_archives_only_evaluation_farms_and_their_accounts(self, api, staffed_farm, owner, manager):
        first, second = create_evaluation_farms(2)
        lookalike = Farm.objects.create(name="Demo Farm – Session 40", owner=manager)
        before = _counts()

        result = archive_evaluation_farms([first.farm, staffed_farm, lookalike])

        assert result.archived == ["Demo Farm – Session 01"]
        assert sorted(result.skipped) == sorted([staffed_farm.name, lookalike.name])
        assert result.accounts_deactivated == 4
        assert _counts() == before  # nothing deleted, nothing added

        first.farm.refresh_from_db()
        assert first.farm.is_active is False
        for account in first.accounts:
            assert User.objects.get(phone_number=account.phone_number).is_active is False

        for farm in (second.farm, staffed_farm, lookalike):
            farm.refresh_from_db()
            assert farm.is_active
        for account in second.accounts:
            assert User.objects.get(phone_number=account.phone_number).is_active
        for user in (owner, manager):
            user.refresh_from_db()
            assert user.is_active

        # Its history is still there, just closed.
        assert Batch.objects.filter(house__farm=first.farm).count() == 3

    def test_archived_accounts_cannot_log_in(self, api):
        result = create_evaluation_farm(2)
        archive_evaluation_farms([result.farm])
        for account in result.accounts:
            response = api.post(
                reverse("accounts:login"),
                {"phone_number": account.phone_number, "pin": account.pin},
                format="json",
            )
            assert response.status_code == 401

    def test_archiving_twice_is_harmless(self):
        result = create_evaluation_farm(2)
        archive_evaluation_farms([result.farm])
        again = archive_evaluation_farms([result.farm])
        assert again.archived == [result.farm.name]
        assert again.accounts_deactivated == 0

    def test_demo_number_not_linked_to_the_farm_is_left_alone(self):
        result = create_evaluation_farm(2)
        # Session 2's buyer link is the only thing tying the buyer to the
        # farm; take it away and the buyer is no longer one of its accounts.
        buyer_phone = demo_phone_number(2, 4)
        FarmPartnerLink.objects.filter(farm=result.farm).update(partner=User.objects.create_user(
            "+639170001111", "112233", full_name="Other Buyer", role=User.Role.CONSUMER,
        ))
        archive_evaluation_farms([result.farm])
        assert User.objects.get(phone_number=buyer_phone).is_active


class TestCommand:
    def test_creates_farms_and_prints_the_pins_with_debug_off(self, settings, capsys):
        settings.DEBUG = False
        call_command("create_evaluation_farms", "--count", "2")
        out = capsys.readouterr().out
        assert "Demo Farm – Session 01" in out and "Demo Farm – Session 02" in out
        assert Farm.objects.filter(name__startswith=EVALUATION_FARM_PREFIX).count() == 2
        assert out.count("PIN ") == 8
        assert "+639990100001" in out
        assert SmsLog.objects.count() == 0 and Invitation.objects.count() == 0

    def test_refuses_a_bad_count(self):
        with pytest.raises(CommandError):
            call_command("create_evaluation_farms", "--count", "11")
        assert Farm.objects.count() == 0
