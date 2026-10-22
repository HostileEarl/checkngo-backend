# production/evaluation.py
"""
Evaluation farms: one clean, pre-filled farm per guided test session.

Each user-acceptance session (an owner, a manager, a worker, a buyer and an
IT expert, each testing the LIVE app) needs a farm of its own with about
three weeks of history behind it and four accounts that can log in straight
away. The live site has no shell, so this is reached from the Django admin
(and, locally, from ``manage.py create_evaluation_farms``).

It runs against the production database, so it only ever ADDS rows:

- Everything for one call lands in one transaction, or nothing does.
- It refuses up front if the farm name or any of the four phone numbers is
  already taken. It never reuses, renames or edits an existing farm, user,
  batch or record.
- No SMS, no Invitation, no Semaphore call: the four accounts are created
  directly, each with a random 6-digit PIN (``accounts.models.generate_pin``,
  i.e. ``secrets``) stored hashed like any other credential, and with the
  PIN-change gate already passed (must_change_credential=False).
- Nothing texts them later either: their numbers are reserved
  (accounts/demo_numbers.py) and notifications/sms.py refuses to send to
  them, and send_stock_alerts skips evaluation farms altogether.
- An evaluation farm is identified by its name, "Demo Farm – Session NN",
  and by its owner holding that session's reserved number. There is no
  extra column, so there is no migration.

Archiving (after the session) sets the farm inactive and deactivates its
four demo accounts. It never deletes anything.

The sample records are SYNTHETIC. Placement 21 days ago, records for every
day up to yesterday (today is left for the respondent to record), and each
row's timestamps dated to the day it describes — so the 24-hour edit lock
applies to them exactly as it would to real records.

The first three helpers below are shared with seed_demo_data (moved here
from that command unchanged, so its output is unchanged).
"""
import os
import random
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.demo_numbers import (
    DEMO_NUMBER_PREFIX,
    MAX_SESSION_NUMBER,
    demo_phone_number,
    is_reserved_demo_number,
)
from accounts.models import User, generate_pin
from farms.models import Farm, FarmMembership
from partners.models import FarmPartnerLink

from .models import (
    Batch,
    DailyRecord,
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
    record_feed_delivery,
)
from .serializers import sync_feed_usage_log


# ─────────────────────────────────────────────────────────────
# Shared with seed_demo_data
# ─────────────────────────────────────────────────────────────


def mortality_day_weights(days):
    """
    The realistic broiler mortality shape, one weight per day of the cycle
    (day 1 first): a spike in week 1 (chick mortality), a low plateau, then
    a slight rise near harvest as birds get heavy.
    """
    weights = []
    for day in range(1, days + 1):
        if day <= 7:
            w = 3.5 - (day * 0.3)       # early chick losses
        elif day <= 28:
            w = 0.5                      # healthy plateau
        else:
            w = 0.8 + ((day - 28) * 0.05)  # heavier birds, more culls
        weights.append(max(w, 0.2))
    return weights


def feed_in_sacks(feed_kg, feed_item):
    """
    Feed is converted once: sacks is derived from the target kg, then
    feed_kg is re-derived FROM that sack count — not kept as the
    independently-rounded figure — so the record's contribution to the feed
    ledger and to the item's usage log always agree exactly.

    Returns (feed_sacks, feed_kg).
    """
    feed_sacks = (feed_kg / feed_item.kg_per_unit).quantize(Decimal("0.01"))
    feed_kg = (feed_sacks * feed_item.kg_per_unit).quantize(Decimal("0.01"))
    return feed_sacks, feed_kg


def save_daily_records(batch, records, user=None):
    """
    Bulk-insert one batch's daily records the way the API would leave them.

    bulk_create skips signals, so refresh the denormalized totals and — same
    as the API's create()/bulk-sync — mirror each feed line into its linked
    InventoryUsageLog. The usage log is attributed to `user`, or to each
    record's own recorded_by when no user is given.
    """
    DailyRecord.objects.bulk_create(records)
    batch.recalculate_totals()
    for record in records:
        if record.feed_item_id is not None:
            sync_feed_usage_log(record, user=user or record.recorded_by)


# ─────────────────────────────────────────────────────────────
# Identity: names, numbers, session parsing
# ─────────────────────────────────────────────────────────────

EVALUATION_FARM_PREFIX = "Demo Farm – Session "
MAX_FARMS_PER_REQUEST = 10

# (key, label, User.Role, digit at the end of the phone number). The numbers
# themselves — and the rule that nothing ever texts them — are defined once
# in accounts/demo_numbers.py, shared with notifications/sms.py.
DEMO_ROLES = [
    ("owner", "Owner", User.Role.OWNER, 1),
    ("manager", "Manager", User.Role.MANAGER, 2),
    ("worker", "Worker", User.Role.WORKER, 3),
    ("buyer", "Buyer", User.Role.CONSUMER, 4),
]

_NAME_RE = re.compile(r"^" + re.escape(EVALUATION_FARM_PREFIX) + r"(\d{2})$")


class EvaluationFarmError(Exception):
    """Refused before anything was written. The message says why."""


def evaluation_farm_name(session_number):
    return f"{EVALUATION_FARM_PREFIX}{session_number:02d}"


def demo_phone_numbers(session_number):
    return [demo_phone_number(session_number, digit) for *_, digit in DEMO_ROLES]


def session_number_of(farm):
    """
    The session number of an evaluation farm, or None for any other farm.

    The name alone is not proof: a real owner could type the same name. The
    farm must also be owned by that session's reserved owner number, which
    only this module ever creates.
    """
    match = _NAME_RE.match(farm.name or "")
    if not match:
        return None
    number = int(match.group(1))
    if not 1 <= number <= MAX_SESSION_NUMBER:
        return None
    if farm.owner.phone_number != demo_phone_number(number, DEMO_ROLES[0][3]):
        return None
    return number


def evaluation_farms():
    """Every evaluation farm, oldest session first."""
    candidates = (
        Farm.objects.filter(name__startswith=EVALUATION_FARM_PREFIX)
        .select_related("owner")
        .order_by("name")
    )
    return [farm for farm in candidates if session_number_of(farm) is not None]


def demo_accounts_of(farm, session_number):
    """
    The demo accounts of one evaluation farm: a reserved number for that
    session AND a link to this farm (owner, member or buyer). Both, so a
    real account can never be caught.
    """
    return (
        User.objects.filter(phone_number__in=demo_phone_numbers(session_number))
        .filter(
            Q(owned_farms=farm)
            | Q(farm_memberships__farm=farm)
            | Q(farm_links__farm=farm)
        )
        .distinct()
    )


def session_conflicts(session_number):
    """Why this session number cannot be used, or an empty list."""
    reasons = []
    name = evaluation_farm_name(session_number)
    if Farm.objects.filter(name=name).exists():
        reasons.append(f'a farm named "{name}" already exists')
    taken = User.objects.filter(
        phone_number__in=demo_phone_numbers(session_number)
    ).values_list("phone_number", flat=True)
    for phone in sorted(taken):
        reasons.append(f"an account with the number {phone} already exists")
    return reasons


def next_free_session_numbers(count):
    """The `count` lowest session numbers whose name and numbers are all free."""
    taken_names = set(
        Farm.objects.filter(name__startswith=EVALUATION_FARM_PREFIX).values_list(
            "name", flat=True
        )
    )
    taken_phones = set(
        User.objects.filter(phone_number__startswith=DEMO_NUMBER_PREFIX).values_list(
            "phone_number", flat=True
        )
    )
    free = [
        number
        for number in range(1, MAX_SESSION_NUMBER + 1)
        if evaluation_farm_name(number) not in taken_names
        and not taken_phones.intersection(demo_phone_numbers(number))
    ]
    if len(free) < count:
        raise EvaluationFarmError(
            f"Only {len(free)} session number(s) are still free; "
            f"{count} were asked for."
        )
    return free[:count]


# ─────────────────────────────────────────────────────────────
# Creation
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DemoAccount:
    role: str
    full_name: str
    phone_number: str
    pin: str = field(repr=False)


@dataclass
class EvaluationFarm:
    farm: Farm
    session_number: int
    accounts: list


def create_evaluation_farm(session_number, created_by=None):
    """
    Create one evaluation farm, its four accounts and its sample history,
    in one transaction. Returns an EvaluationFarm whose `accounts` carry the
    plaintext PINs — the only place they ever exist.

    Raises EvaluationFarmError, having written nothing, when the session
    number is out of range or its farm name or any of its numbers is taken.
    """
    return create_evaluation_farms(session_numbers=[session_number], created_by=created_by)[0]


def create_evaluation_farms(count=None, *, session_numbers=None, created_by=None):
    """
    Create `count` evaluation farms on the next free session numbers (or on
    the given `session_numbers`), all in ONE transaction: either every farm
    is created or none is.

    `created_by`, when given, is recorded in the admin log against each farm.
    """
    if session_numbers is None:
        if not isinstance(count, int) or not 1 <= count <= MAX_FARMS_PER_REQUEST:
            raise EvaluationFarmError(
                f"Choose between 1 and {MAX_FARMS_PER_REQUEST} farms."
            )
        session_numbers = next_free_session_numbers(count)

    for number in session_numbers:
        if not isinstance(number, int) or not 1 <= number <= MAX_SESSION_NUMBER:
            raise EvaluationFarmError(
                f"Session numbers run from 1 to {MAX_SESSION_NUMBER}; got {number!r}."
            )
        reasons = session_conflicts(number)
        if reasons:
            raise EvaluationFarmError(
                f"Session {number:02d} cannot be created: {'; '.join(reasons)}. "
                "Nothing was created."
            )

    credentials = _issue_credentials(session_numbers)

    try:
        with transaction.atomic():
            results = [
                _create_one(number, credentials[number], created_by)
                for number in session_numbers
            ]
    except IntegrityError as exc:
        # Two creations racing for the same numbers: the unique phone number
        # stops the second one, and the transaction leaves nothing behind.
        raise EvaluationFarmError(
            "Another creation took these session numbers at the same moment. "
            "Nothing was created; try again."
        ) from exc
    return results


def _issue_credentials(session_numbers):
    """
    A fresh random PIN per account, hashed with the project's normal password
    hasher. Hashing is deliberately slow (about a second per PIN), so the
    hashes are computed in parallel threads — hashlib releases the GIL — and
    before the transaction opens, so no lock is held while they run.
    """
    pins = {
        (number, key): generate_pin()
        for number in session_numbers
        for key, *_ in DEMO_ROLES
    }
    keys = list(pins)
    workers = min(len(keys), max(4, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        hashes = dict(zip(keys, pool.map(make_password, [pins[k] for k in keys])))

    credentials = defaultdict(dict)
    for (number, key), pin in pins.items():
        credentials[number][key] = (pin, hashes[(number, key)])
    return credentials


def _create_one(session_number, credentials, created_by):
    now = timezone.now()
    nn = f"{session_number:02d}"

    users = {}
    accounts = []
    for key, label, role, digit in DEMO_ROLES:
        pin, password_hash = credentials[key]
        phone = demo_phone_number(session_number, digit)
        if not is_reserved_demo_number(phone):
            # send_sms would text it: refuse rather than mint a textable account.
            raise RuntimeError(f"{phone} is not a reserved demo number.")
        user = User(
            phone_number=phone,
            full_name=f"{label} – Session {nn}",
            role=role,
            password=password_hash,
            must_change_credential=False,
            credential_changed_at=now,
        )
        user.full_clean(exclude=["password"])
        user.save()
        users[key] = user
        accounts.append(DemoAccount(label, user.full_name, user.phone_number, pin))

    farm = Farm.objects.create(
        name=evaluation_farm_name(session_number),
        owner=users["owner"],
        municipality="Atimonan",
        province="Quezon",
    )
    # The post_save signals have already given the owner their OWNER
    # membership and opened the ownership history.

    _FarmHistory(
        farm=farm,
        owner=users["owner"],
        manager=users["manager"],
        worker=users["worker"],
        buyer=users["buyer"],
        rng=random.Random(f"evaluation-farm-{session_number}"),
    ).build()

    if created_by is not None:
        from django.contrib.admin.models import ADDITION, LogEntry

        LogEntry.objects.log_actions(
            user_id=created_by.pk,
            queryset=[farm],
            action_flag=ADDITION,
            change_message="Created evaluation farm with four demo accounts.",
            single_object=True,
        )

    return EvaluationFarm(farm=farm, session_number=session_number, accounts=accounts)


# ─────────────────────────────────────────────────────────────
# Archiving
# ─────────────────────────────────────────────────────────────


@dataclass
class ArchiveResult:
    archived: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    accounts_deactivated: int = 0


def archive_evaluation_farms(farms, archived_by=None):
    """
    Set each evaluation farm inactive and deactivate its four demo accounts
    (and revoke their sessions). Any farm that is not an evaluation farm is
    left untouched and reported in `skipped`. Nothing is deleted.
    """
    result = ArchiveResult()
    with transaction.atomic():
        for farm in farms:
            number = session_number_of(farm)
            if number is None:
                result.skipped.append(farm.name)
                continue

            farm.archive()
            deactivated = 0
            for user in demo_accounts_of(farm, number).filter(is_active=True):
                user.is_active = False
                user.save(update_fields=["is_active"])
                user.revoke_all_sessions()
                deactivated += 1
            result.archived.append(farm.name)
            result.accounts_deactivated += deactivated

            if archived_by is not None:
                from django.contrib.admin.models import CHANGE, LogEntry

                LogEntry.objects.log_actions(
                    user_id=archived_by.pk,
                    queryset=[farm],
                    action_flag=CHANGE,
                    change_message=(
                        "Archived evaluation farm and deactivated "
                        f"{deactivated} demo account(s)."
                    ),
                    single_object=True,
                )
    return result


# ─────────────────────────────────────────────────────────────
# The sample history
# ─────────────────────────────────────────────────────────────

# Ross 308 reference curves, scaled to an open-sided house in Quezon rather
# than to the breeder's controlled-environment objective: ~2.0 kg at 35
# days, ~3.6 kg feed per bird, FCR 1.6-1.8. (age in days, value), linearly
# interpolated. seed_demo_data keeps its own 42-day curves, so its output
# does not move.
FEED_INTAKE_G = [(0, 12), (7, 38), (14, 82), (21, 128), (28, 168), (35, 200)]
LIVE_WEIGHT_G = [(0, 42), (7, 175), (14, 460), (21, 900), (28, 1450), (35, 2050)]

# Which feed a bird of a given age eats (last age inclusive).
FEED_STAGES = [
    (10, InventoryItem.FeedType.STARTER),
    (23, InventoryItem.FeedType.BOOSTER),
    (999, InventoryItem.FeedType.FINISHER),
]

CAUSES_FIRST_WEEK = {"disease": 45, "heat": 0, "culled": 30, "unknown": 25}
CAUSES_LATER = {"disease": 35, "heat": 15, "culled": 30, "unknown": 20}

ROUTINE = [
    ("Check water lines", "6:00 AM", 6),
    ("Clean feeders", "7:00 AM", 7),
    ("Check ventilation", "12:00 PM", 12),
    ("Evening headcount", "5:00 PM", 17),
]

FEED_SUPPLIER = "Atimonan Agri Supply"
CYCLE_DAYS = 35
ACTIVE_AGE_DAYS = 21          # active batches were placed this many days ago
PAST_PLACED_DAYS_AGO = 63     # the harvested batch: placed ~2 months ago
SETUP_DAYS_AGO = 70           # houses, items and staff "joined" here
QUARTER = Decimal("0.25")
TWO_PLACES = Decimal("0.01")


def _interpolate(points, age):
    for (a0, v0), (a1, v1) in zip(points, points[1:]):
        if a0 <= age <= a1:
            return v0 + (v1 - v0) * (age - a0) / (a1 - a0)
    return points[-1][1]


def _allocate(total, weights):
    """Split an integer total across weights exactly (largest remainder)."""
    scale = sum(weights)
    raw = [total * w / scale for w in weights]
    shares = [int(r) for r in raw]
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i] - shares[i], reverse=True)
    for i in by_remainder[: total - sum(shares)]:
        shares[i] += 1
    return shares


def _ceil(value):
    return int(Decimal(value).to_integral_value(rounding=ROUND_CEILING))


class _FarmHistory:
    """
    Writes one evaluation farm's houses, batches, records, sales, stock and
    routine, every row dated as if entered on the day it describes.
    """

    def __init__(self, *, farm, owner, manager, worker, buyer, rng):
        self.farm = farm
        self.owner = owner
        self.manager = manager
        self.worker = worker
        self.buyer = buyer
        self.rng = rng
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.setup_day = self.today - timedelta(days=SETUP_DAYS_AGO)
        self.past_start = self.today - timedelta(days=PAST_PLACED_DAYS_AGO)
        self.active_start = self.today - timedelta(days=ACTIVE_AGE_DAYS)
        self.ref = farm.name[-2:]
        # Sacks of each feed eaten per cycle, to size the deliveries.
        self.feed_used = defaultdict(Decimal)

    def build(self):
        self._staff_and_buyer()
        self._houses()
        self._inventory_items()
        self._routine_templates()
        self._harvested_batch()
        self._active_batches()
        self._feed_deliveries()
        self._supplies()
        self._routine_completions()

    # -- time ------------------------------------------------

    def _at(self, day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)))

    def _stamp(self, rows, delay_seconds=(20, 240), offline=False):
        """
        created_at/updated_at a short sync delay after recorded_at, never in
        the future. With `offline`, about one older record in eight syncs the
        next morning instead — the offline window the app tracks.
        """
        for row in rows:
            delay = timedelta(seconds=self.rng.randint(*delay_seconds))
            if offline and row.recorded_at < self.now - timedelta(days=3):
                if self.rng.random() < 0.125:
                    delay = timedelta(hours=self.rng.randint(12, 15))
            row.created_at = row.updated_at = min(row.recorded_at + delay, self.now)
        if rows:
            type(rows[0]).objects.bulk_update(rows, ["created_at", "updated_at"])

    # -- people and places -----------------------------------

    def _staff_and_buyer(self):
        joined = self._at(self.setup_day, 8)
        FarmMembership.objects.filter(farm=self.farm, user=self.owner).update(joined_at=joined)
        FarmMembership.objects.create(
            farm=self.farm, user=self.manager, role=FarmMembership.Role.MANAGER,
            invited_by=self.owner, joined_at=joined,
        )
        self.worker_membership = FarmMembership.objects.create(
            farm=self.farm, user=self.worker, role=FarmMembership.Role.WORKER,
            invited_by=self.owner, joined_at=joined,
        )
        self.buyer_link = FarmPartnerLink.objects.create(
            farm=self.farm,
            partner=self.buyer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            business_name=f"Session {self.ref} Buyer",
            linked_by=self.owner,
            linked_at=joined,
        )

    def _houses(self):
        self.house1 = House.objects.create(farm=self.farm, name="House 1", capacity=1200)
        self.house2 = House.objects.create(farm=self.farm, name="House 2", capacity=1000)
        House.objects.filter(farm=self.farm).update(created_at=self._at(self.setup_day, 9))
        # The worker records for House 1 only; the manager and owner, anywhere.
        self.worker_membership.houses.set([self.house1])

    def _inventory_items(self):
        self.feed_items = {}
        specs = [
            ("Starter feed", InventoryItem.FeedType.STARTER, "5"),
            ("Booster feed", InventoryItem.FeedType.BOOSTER, "10"),
            ("Finisher feed", InventoryItem.FeedType.FINISHER, "10"),
        ]
        for name, feed_type, threshold in specs:
            self.feed_items[feed_type] = InventoryItem.objects.create(
                farm=self.farm, name=name, feed_type=feed_type, unit="sack",
                kg_per_unit=Decimal("50"), is_feed=True,
                low_stock_threshold=Decimal(threshold), created_by=self.owner,
            )
        self.disinfectant = InventoryItem.objects.create(
            farm=self.farm, name="Disinfectant", unit="litre",
            low_stock_threshold=Decimal("5"), created_by=self.manager,
        )
        self.vitamins = InventoryItem.objects.create(
            farm=self.farm, name="Vitamins", unit="pack",
            low_stock_threshold=Decimal("5"), created_by=self.manager,
        )
        InventoryItem.objects.filter(farm=self.farm).update(
            created_at=self._at(self.setup_day, 10)
        )

    def _routine_templates(self):
        self.templates = [
            (
                TaskTemplate.objects.create(
                    farm=self.farm, name=name, suggested_time=when, order=order,
                    created_by=self.manager,
                ),
                hour,
            )
            for order, (name, when, hour) in enumerate(ROUTINE, start=1)
        ]
        TaskTemplate.objects.filter(farm=self.farm).update(
            created_at=self._at(self.setup_day, 11)
        )

    # -- batches ---------------------------------------------

    def _new_batch(self, house, code, birds, start):
        batch = Batch.objects.create(
            house=house,
            batch_code=f"{code}-{start:%Y-%m}",
            breed="Ross 308",
            initial_bird_count=birds,
            start_date=start,
            expected_harvest_date=start + timedelta(days=CYCLE_DAYS),
            created_by=self.owner,
        )
        Batch.objects.filter(pk=batch.pk).update(created_at=self._at(start, 7))
        return batch

    def _house1_recorder(self, day):
        # The worker records House 1; the manager covers the worker's Sunday off.
        return self.manager if day.weekday() == 6 else self.worker

    def _house2_recorder(self, day):
        # The manager records House 2; the owner covers on Sundays.
        return self.owner if day.weekday() == 6 else self.manager

    def _mortality_plan(self, birds, days, low_pct, high_pct):
        """Deaths per day by cause, totalling exactly low_pct-high_pct of the flock."""
        total = round(birds * self.rng.uniform(low_pct, high_pct) / 100)
        weights = [w * self.rng.uniform(0.5, 1.5) for w in mortality_day_weights(days)]
        plan = []
        for age, deaths in enumerate(_allocate(total, weights)):
            causes = CAUSES_FIRST_WEEK if age <= 7 else CAUSES_LATER
            counts = Counter(
                self.rng.choices(list(causes), weights=list(causes.values()), k=deaths)
            )
            plan.append((counts["disease"], counts["heat"], counts["culled"], counts["unknown"]))
        return plan

    def _feed_item_for(self, age):
        for last_age, feed_type in FEED_STAGES:
            if age <= last_age:
                return self.feed_items[feed_type]
        raise AssertionError("unreachable")

    def _grow(self, batch, days, deaths, recorder_for, notes, cycle, sold_by_age=None):
        """
        One daily record per day, age 0 (placement day) to `days - 1`, with
        feed in quarter sacks of the feed for that age. Birds sold on a day
        leave before that day's feeding.
        """
        sold_by_age = sold_by_age or {}
        alive = batch.initial_bird_count
        records = []
        for age in range(days):
            alive -= sold_by_age.get(age, 0)
            day = batch.start_date + timedelta(days=age)
            item = self._feed_item_for(age)

            grams = _interpolate(FEED_INTAKE_G, age) * self.rng.uniform(0.96, 1.04)
            target_sacks = Decimal(alive * grams / 1000) / item.kg_per_unit
            sacks = max(
                (target_sacks / QUARTER).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * QUARTER,
                QUARTER,
            )
            feed_sacks, feed_kg = feed_in_sacks(sacks * item.kg_per_unit, item)
            self.feed_used[(cycle, item.feed_type)] += feed_sacks

            disease, heat, culled, unknown = deaths[age]
            alive -= disease + heat + culled + unknown

            records.append(
                DailyRecord(
                    batch=batch,
                    record_date=day,
                    mortality_disease=disease,
                    mortality_heat=heat,
                    mortality_culled=culled,
                    mortality_unknown=unknown,
                    feed_kg=feed_kg,
                    feed_sacks=feed_sacks,
                    feed_item=item,
                    notes=notes.get(age, ""),
                    recorded_by=recorder_for(day),
                    recorded_at=self._at(day, 16, 30)
                    + timedelta(minutes=self.rng.randint(0, 120)),
                )
            )

        save_daily_records(batch, records)
        self._stamp(records, offline=True)

        # The derived feed drawdowns were written just now; date them with
        # the record they mirror.
        logs = list(
            InventoryUsageLog.objects.filter(daily_record__batch=batch).select_related(
                "daily_record"
            )
        )
        for log in logs:
            log.recorded_at = log.daily_record.recorded_at
            log.created_at = log.updated_at = log.daily_record.created_at
        InventoryUsageLog.objects.bulk_update(logs, ["recorded_at", "created_at", "updated_at"])

        batch.refresh_from_db()
        return records

    def _weigh(self, batch, ages, factor, recorder_for):
        samples = []
        for age in ages:
            day = batch.start_date + timedelta(days=age)
            grams = _interpolate(LIVE_WEIGHT_G, age) * factor * self.rng.uniform(0.97, 1.03)
            samples.append(
                WeightSample(
                    batch=batch,
                    sample_date=day,
                    birds_weighed=self.rng.choice([20, 25, 30]),
                    average_grams=Decimal(grams).quantize(TWO_PLACES),
                    recorded_by=recorder_for(day),
                    recorded_at=self._at(day, 9) + timedelta(minutes=self.rng.randint(0, 45)),
                )
            )
        WeightSample.objects.bulk_create(samples)
        self._stamp(samples)

    def _harvested_batch(self):
        """
        House 1's previous flock: 1,000 birds, 35 days, sold over the last
        three days (two loads to the linked buyer, a walk-in sale, the feet
        from the dressed birds), paid for, one cheque reversed, then closed.
        """
        start = self.past_start
        birds = 1000
        batch = self._new_batch(self.house1, "H1", birds, start)
        deaths = self._mortality_plan(birds, CYCLE_DAYS, 2.6, 3.6)

        alive_day33 = birds - sum(sum(d) for d in deaths[:33])
        first_load = round(alive_day33 * self.rng.uniform(0.52, 0.58))
        walk_in = 40
        notes = {
            0: "Chicks arrived early morning, active and alert.",
            7: "Newcastle-IB vaccine given by eye drop.",
            11: "Switched to Booster feed.",
            14: "Gumboro vaccine in drinking water.",
            24: "Switched to Finisher feed.",
            33: "First load collected by the buyer.",
        }
        self._grow(
            batch, CYCLE_DAYS, deaths, self._house1_recorder, notes, "past",
            sold_by_age={33: first_load, 34: walk_in},
        )
        last_load = birds - batch.total_mortality - first_load - walk_in

        # Live weight from the feed actually eaten and a target FCR, shared
        # across the three loads by each sale day's weight on the curve.
        target_fcr = self.rng.uniform(1.66, 1.74)
        weight_total = float(batch.total_feed_kg) / target_fcr
        loads = [(33, first_load), (34, walk_in), (35, last_load)]
        scale = weight_total * 1000 / sum(
            n * _interpolate(LIVE_WEIGHT_G, age) for age, n in loads
        )
        kg = {
            age: Decimal(n * _interpolate(LIVE_WEIGHT_G, age) * scale / 1000).quantize(TWO_PLACES)
            for age, n in loads
        }

        def day(age):
            return start + timedelta(days=age)

        first = SaleEvent.objects.create(
            batch=batch, sale_date=day(33), sale_type=SaleEvent.SaleType.LIVE,
            bird_count=first_load, total_weight_kg=kg[33], price_per_kg=Decimal("132.00"),
            buyer_link=self.buyer_link, notes="First load, buyer's truck.",
            recorded_by=self.owner, recorded_at=self._at(day(33), 8, 30),
        )
        walk = SaleEvent.objects.create(
            batch=batch, sale_date=day(34), sale_type=SaleEvent.SaleType.DRESSED,
            bird_count=walk_in, total_weight_kg=kg[34], price_per_kg=Decimal("150.00"),
            buyer_name="Walk-in customer", recorded_by=self.manager,
            recorded_at=self._at(day(34), 15, 10),
        )
        last = SaleEvent.objects.create(
            batch=batch, sale_date=day(35), sale_type=SaleEvent.SaleType.LIVE,
            bird_count=last_load, total_weight_kg=kg[35], price_per_kg=Decimal("130.00"),
            buyer_link=self.buyer_link, notes="Last load.",
            recorded_by=self.owner, recorded_at=self._at(day(35), 7, 45),
        )
        feet = SaleEvent.objects.create(
            batch=batch, sale_date=day(35), sale_type=SaleEvent.SaleType.OTHERS,
            part_name="Feet", total_weight_kg=Decimal("3.40"), price_per_kg=Decimal("90.00"),
            recorded_by=self.manager, recorded_at=self._at(day(35), 10, 0),
        )
        self._stamp([first, walk, last, feet])

        paid_first = Payment.objects.create(
            sale=first, amount=first.revenue, received_date=day(34),
            note="Cash on pickup.", buyer_link=self.buyer_link, recorded_by=self.owner,
        )
        paid_last = Payment.objects.create(
            sale=last, amount=last.revenue, received_date=day(36),
            note="Cheque.", buyer_link=self.buyer_link, recorded_by=self.owner,
        )
        reversal = Payment.objects.create(
            sale=last, amount=-paid_last.amount, received_date=day(40),
            note="Cheque returned by the bank.", buyer_link=self.buyer_link,
            reverses=paid_last, recorded_by=self.owner,
        )
        for payment, hour in ((paid_first, 9), (paid_last, 14), (reversal, 11)):
            Payment.objects.filter(pk=payment.pk).update(
                recorded_at=self._at(payment.received_date, hour)
            )

        # Close it the way BatchCloseSerializer does: totals summed from the
        # sale events, parts excluded from birds and weight.
        batch.refresh_from_db()
        live = [first, walk, last]
        remaining = batch.current_bird_count
        harvest = Harvest.objects.create(
            batch=batch,
            harvest_date=day(35),
            birds_harvested=sum(s.bird_count for s in live),
            total_weight_kg=sum(s.total_weight_kg for s in live),
            revenue=sum(s.revenue for s in (first, walk, last, feet)),
            closing_notes=f"{remaining} birds unaccounted for at closing." if remaining else "",
            recorded_by=self.owner,
        )
        Harvest.objects.filter(pk=harvest.pk).update(created_at=self._at(day(35), 16))
        batch.status = Batch.Status.HARVESTED
        batch.save(update_fields=["status"])

        fcr = batch.feed_conversion_ratio
        if fcr is None or not Decimal("1.6") <= fcr <= Decimal("1.8"):
            raise RuntimeError(f"Evaluation farm FCR came out at {fcr}; expected 1.6-1.8.")

        self._weigh(batch, [7, 14, 21, 28, 32], scale, self._house1_recorder)

    def _active_batches(self):
        start = self.active_start
        flocks = [
            (self.house1, "H1", 1000, self._house1_recorder, {
                0: "Chicks arrived early morning, active and alert.",
                7: "Newcastle-IB vaccine given by eye drop.",
                11: "Switched to Booster feed.",
                14: "Gumboro vaccine in drinking water.",
                17: "Heavy rain in the afternoon, curtains lowered.",
            }),
            (self.house2, "H2", 800, self._house2_recorder, {
                0: "Chicks arrived mid-morning.",
                7: "Newcastle-IB vaccine given by eye drop.",
                11: "Switched to Booster feed.",
                15: "Litter turned and topped up with rice hull.",
            }),
        ]
        for house, code, birds, recorder_for, notes in flocks:
            batch = self._new_batch(house, code, birds, start)
            deaths = self._mortality_plan(birds, ACTIVE_AGE_DAYS, 2.2, 3.2)
            self._grow(batch, ACTIVE_AGE_DAYS, deaths, recorder_for, notes, "current")
            self._weigh(batch, [7, 14], self.rng.uniform(0.98, 1.04), recorder_for)

    # -- stock -----------------------------------------------

    def _feed_deliveries(self):
        """
        Deliveries in whole sacks, sized to what each cycle ate plus a few
        sacks spare, so every feed item and the farm's feed balance stay
        positive and above their reorder levels.
        """
        S, A = self.past_start, self.active_start
        starter, booster, finisher = (
            self.feed_items[t] for t in (
                InventoryItem.FeedType.STARTER,
                InventoryItem.FeedType.BOOSTER,
                InventoryItem.FeedType.FINISHER,
            )
        )

        def used(cycle, item):
            return self.feed_used[(cycle, item.feed_type)]

        plan = []  # (day, item, sacks, price per kg, recorded_by)
        past_starter = _ceil(used("past", starter)) + 2
        past_booster = _ceil(used("past", booster)) + 1
        past_finisher = _ceil(used("past", finisher)) + 2
        first_finisher = _ceil(past_finisher * Decimal("0.6"))
        plan += [
            (S - timedelta(days=2), starter, past_starter, "37.00", self.manager),
            (S + timedelta(days=9), booster, past_booster, "35.50", self.manager),
            (S + timedelta(days=22), finisher, first_finisher, "34.00", self.owner),
            (S + timedelta(days=29), finisher, past_finisher - first_finisher, "34.00", self.manager),
        ]

        left_starter = past_starter - used("past", starter)
        left_booster = past_booster - used("past", booster)
        now_starter = max(_ceil(used("current", starter) - left_starter), 0) + 8
        now_booster = max(_ceil(used("current", booster) - left_booster), 0) + 16
        first_booster = _ceil(Decimal(now_booster) / 2)
        plan += [
            (A - timedelta(days=2), starter, now_starter, "37.50", self.manager),
            (A + timedelta(days=9), booster, first_booster, "36.00", self.manager),
            (A + timedelta(days=15), booster, now_booster - first_booster, "36.00", self.owner),
            (A + timedelta(days=19), finisher, 40, "34.50", self.manager),
        ]

        deliveries, stock_ins = [], []
        for seq, (day, item, sacks, price, recorded_by) in enumerate(plan, start=1):
            delivery, stock_in = record_feed_delivery(
                farm=self.farm,
                recorded_by=recorded_by,
                inventory_item=item,
                other_source=FEED_SUPPLIER,
                delivery_date=day,
                feed_type=item.feed_type,
                quantity_kg=Decimal(sacks) * item.kg_per_unit,
                unit_cost=Decimal(price),
                invoice_ref=f"DR-{self.ref}{seq:02d}",
                notes=f"{sacks} sacks",
                recorded_at=self._at(day, 10) + timedelta(minutes=self.rng.randint(0, 50)),
            )
            deliveries.append(delivery)
            stock_ins.append(stock_in)
        self._stamp(deliveries)
        for delivery, stock_in in zip(deliveries, stock_ins):
            stock_in.created_at = delivery.created_at
        InventoryStockIn.objects.bulk_update(stock_ins, ["created_at"])

    def _supplies(self):
        """Disinfectant comfortably stocked; vitamins one pack under the reorder level."""
        A = self.active_start

        def day(offset):
            return A + timedelta(days=offset)

        stock_ins = [
            InventoryStockIn.objects.create(
                item=self.disinfectant, quantity=Decimal("20"), stock_in_date=day(-4),
                note="Bought in town, 4 x 5 L.", recorded_by=self.manager,
            ),
            InventoryStockIn.objects.create(
                item=self.vitamins, quantity=Decimal("12"), stock_in_date=day(-2),
                note="Bought with the starter feed.", recorded_by=self.manager,
            ),
        ]
        for stock_in in stock_ins:
            InventoryStockIn.objects.filter(pk=stock_in.pk).update(
                created_at=self._at(stock_in.stock_in_date, 14)
            )

        uses = [
            (self.disinfectant, "6", -3, self.manager, "Cleaned and disinfected both houses before placement."),
            (self.disinfectant, "1", 6, self.worker, "Footbath refill."),
            (self.disinfectant, "1", 13, self.worker, "Footbath refill."),
            (self.disinfectant, "1", 20, self.worker, "Footbath refill."),
            (self.vitamins, "2", 1, self.manager, "Vitamins in drinking water, both houses."),
            (self.vitamins, "2", 4, self.worker, "Vitamins in drinking water, House 1."),
            (self.vitamins, "2", 8, self.manager, "After vaccination, both houses."),
            (self.vitamins, "2", 15, self.worker, "After vaccination, House 1."),
        ]
        logs = [
            InventoryUsageLog(
                item=item, quantity_used=Decimal(qty), usage_date=day(offset),
                notes=note, recorded_by=user,
                recorded_at=self._at(day(offset), 8) + timedelta(minutes=self.rng.randint(0, 90)),
            )
            for item, qty, offset, user, note in uses
        ]
        InventoryUsageLog.objects.bulk_create(logs)
        self._stamp(logs)

    # -- routine ---------------------------------------------

    def _routine_completions(self):
        """Most of the routine ticked off on past days; nothing yet today."""
        completions = []
        day = self.active_start
        while day < self.today:
            for house, recorder_for in (
                (self.house1, self._house1_recorder),
                (self.house2, self._house2_recorder),
            ):
                for template, hour in self.templates:
                    if self.rng.random() < 0.1:
                        continue  # missed that day
                    completions.append(
                        TaskCompletion(
                            template=template, house=house, completion_date=day,
                            recorded_by=recorder_for(day),
                            recorded_at=self._at(day, hour)
                            + timedelta(minutes=self.rng.randint(0, 40)),
                        )
                    )
            day += timedelta(days=1)
        TaskCompletion.objects.bulk_create(completions)
        self._stamp(completions)
