# farms/tests/two_farms.py
"""
Two fully populated farms for the isolation evidence tests.

conftest.py's farm/rival_farm fixtures are deliberately thin. The farm ID
substitution sweep needs the opposite: every kind of farm-owned object, on
both farms, so that every URL kwarg and every id-bearing body field has a
real object of the OTHER farm to point at. build_farm() makes one farm's
worth; snapshot() captures every row that farm owns so a test can prove a
request left it untouched.

Every human-readable value on a farm carries its label ("ALPHA", "BRAVO")
so a response can be searched for the other farm's data by name as well
as by id.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import Invitation, User
from analytics.models import AlertLog
from farms.models import Farm, FarmMembership, FarmOwnershipHistory
from partners.models import FarmPartnerLink
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
    RecordCorrection,
    SaleEvent,
    TaskCompletion,
    TaskTemplate,
    WeightSample,
    record_feed_delivery,
)
from production.serializers import sync_feed_usage_log

# create_user hashes the PIN with the production hasher, about a second a
# user. These tests are about authorization, not hashing, so modules that
# build farms swap in the fast hasher through this fixture-friendly value.
FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def make_user(phone, name, role):
    return User.objects.create_user(
        phone, "135790", full_name=name, role=role, must_change_credential=False
    )


@dataclass
class FarmWorld:
    label: str
    farm: Farm
    owner: User
    manager: User
    worker: User
    manager_membership: FarmMembership
    worker_membership: FarmMembership
    house: House
    house2: House
    batch: Batch
    closed_batch: Batch
    harvest: Harvest
    daily_record: DailyRecord
    weight_sample: WeightSample
    feed_item: InventoryItem
    feed_delivery: FeedDelivery
    item: InventoryItem
    stock_in: InventoryStockIn
    usage_log: InventoryUsageLog
    template: TaskTemplate
    completion: TaskCompletion
    invitation: Invitation
    buyer: User
    buyer_link: FarmPartnerLink
    sale: SaleEvent
    closed_sale: SaleEvent
    payment: Payment
    extra_users: list = field(default_factory=list)

    @property
    def user_ids(self):
        users = [self.owner, self.manager, self.worker, self.buyer, *self.extra_users]
        return [u.pk for u in users]

    @property
    def markers(self):
        """Strings that only this farm's data contains."""
        uuids = [
            self.batch.pk, self.closed_batch.pk, self.daily_record.pk,
            self.weight_sample.pk, self.feed_delivery.pk, self.usage_log.pk,
            self.completion.pk, self.sale.pk, self.closed_sale.pk,
        ]
        return [self.label, *(str(u) for u in uuids)]


def build_farm(label, phone_prefix, *, buyer=None):
    """
    One farm with an owner, manager and worker (assigned to House 1), a
    live batch in House 1 and a closed one in House 2, and one of every
    record the API can read or write.

    `phone_prefix` is the first ten characters of an E.164 number, e.g.
    "+639171000"; the builder appends three digits per user. Pass `buyer` to
    link an existing consumer instead of creating one (a buyer on two
    farms).
    """
    today = timezone.localdate()

    def phone(n):
        return f"{phone_prefix}{n:03d}"

    owner = make_user(phone(1), f"{label} Owner", User.Role.OWNER)
    manager = make_user(phone(2), f"{label} Manager", User.Role.MANAGER)
    worker = make_user(phone(3), f"{label} Worker", User.Role.WORKER)
    if buyer is None:
        buyer = make_user(phone(4), f"{label} Buyer", User.Role.CONSUMER)

    farm = Farm.objects.create(name=f"{label} Farm", owner=owner)
    manager_membership = FarmMembership.objects.create(
        farm=farm, user=manager, role=FarmMembership.Role.MANAGER, invited_by=owner
    )
    worker_membership = FarmMembership.objects.create(
        farm=farm, user=worker, role=FarmMembership.Role.WORKER, invited_by=owner
    )

    house = House.objects.create(farm=farm, name=f"{label} House 1", capacity=5000)
    house2 = House.objects.create(farm=farm, name=f"{label} House 2", capacity=5000)
    worker_membership.houses.add(house)

    batch = Batch.objects.create(
        house=house,
        batch_code=f"{label}-B1",
        initial_bird_count=1000,
        start_date=today - timedelta(days=20),
        created_by=owner,
    )
    closed_batch = Batch.objects.create(
        house=house2,
        batch_code=f"{label}-B0",
        initial_bird_count=500,
        start_date=today - timedelta(days=80),
        created_by=owner,
    )

    buyer_link = FarmPartnerLink.objects.create(
        farm=farm,
        partner=buyer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        business_name=f"{label} Buyer Co",
        linked_by=owner,
    )

    # The closed batch: sold out, then closed, the way BatchCloseView does it.
    closed_sale = SaleEvent.objects.create(
        batch=closed_batch,
        sale_date=today - timedelta(days=40),
        sale_type=SaleEvent.SaleType.LIVE,
        bird_count=480,
        total_weight_kg=Decimal("960.00"),
        price_per_kg=Decimal("150.00"),
        buyer_link=buyer_link,
        notes=f"{label} closed sale",
        recorded_by=owner,
    )
    harvest = Harvest.objects.create(
        batch=closed_batch,
        harvest_date=today - timedelta(days=39),
        birds_harvested=480,
        total_weight_kg=Decimal("960.00"),
        revenue=closed_sale.revenue,
        buyer_link=buyer_link,
        closing_notes=f"{label} closed",
        recorded_by=owner,
    )
    closed_batch.status = Batch.Status.HARVESTED
    closed_batch.save(update_fields=["status"])

    feed_item = InventoryItem.objects.create(
        farm=farm,
        name=f"{label} Starter feed",
        unit="sack",
        kg_per_unit=Decimal("50.00"),
        feed_type=InventoryItem.FeedType.STARTER,
        is_feed=True,
        low_stock_threshold=Decimal("2"),
        created_by=owner,
    )
    feed_delivery, _ = record_feed_delivery(
        farm=farm,
        recorded_by=owner,
        inventory_item=feed_item,
        delivery_date=today - timedelta(days=15),
        feed_type=FeedDelivery.FeedType.STARTER,
        quantity_kg=Decimal("1000.00"),
        unit_cost=Decimal("30.00"),
        other_source=f"{label} Feeds Inc",
    )

    daily_record = DailyRecord.objects.create(
        batch=batch,
        record_date=today - timedelta(days=1),
        mortality_disease=2,
        feed_sacks=Decimal("2.00"),
        feed_item=feed_item,
        feed_kg=Decimal("100.00"),
        notes=f"{label} daily note",
        recorded_by=worker,
    )
    sync_feed_usage_log(daily_record, user=worker)

    weight_sample = WeightSample.objects.create(
        batch=batch,
        sample_date=today - timedelta(days=1),
        birds_weighed=20,
        average_grams=Decimal("850.00"),
        notes=f"{label} weigh-in",
        recorded_by=worker,
    )

    item = InventoryItem.objects.create(
        farm=farm,
        name=f"{label} Vaccine",
        unit="dose",
        low_stock_threshold=Decimal("10"),
        created_by=owner,
    )
    stock_in = InventoryStockIn.objects.create(
        item=item,
        quantity=Decimal("100.00"),
        stock_in_date=today - timedelta(days=10),
        note=f"{label} restock",
        recorded_by=owner,
    )
    usage_log = InventoryUsageLog.objects.create(
        item=item,
        quantity_used=Decimal("5.00"),
        usage_date=today - timedelta(days=1),
        notes=f"{label} usage",
        recorded_by=worker,
    )

    template = TaskTemplate.objects.create(
        farm=farm, name=f"{label} Morning feed", created_by=owner
    )
    completion = TaskCompletion.objects.create(
        template=template,
        house=house,
        completion_date=today - timedelta(days=1),
        recorded_by=worker,
    )

    invitation = Invitation.objects.create(
        farm=farm,
        invited_by=owner,
        phone_number=phone(9),
        full_name=f"{label} Invitee",
        account_role=User.Role.WORKER,
        membership_role=FarmMembership.Role.WORKER,
        pin_hash="x",
    )

    sale = SaleEvent.objects.create(
        batch=batch,
        sale_date=today - timedelta(days=1),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=50,
        total_weight_kg=Decimal("100.00"),
        price_per_kg=Decimal("180.00"),
        buyer_link=buyer_link,
        notes=f"{label} sale",
        recorded_by=owner,
    )
    payment = Payment.objects.create(
        sale=sale,
        amount=Decimal("5000.00"),
        received_date=today - timedelta(days=1),
        note=f"{label} payment",
        buyer_link=buyer_link,
        recorded_by=owner,
    )

    return FarmWorld(
        label=label,
        farm=farm,
        owner=owner,
        manager=manager,
        worker=worker,
        manager_membership=manager_membership,
        worker_membership=worker_membership,
        house=house,
        house2=house2,
        batch=batch,
        closed_batch=closed_batch,
        harvest=harvest,
        daily_record=daily_record,
        weight_sample=weight_sample,
        feed_item=feed_item,
        feed_delivery=feed_delivery,
        item=item,
        stock_in=stock_in,
        usage_log=usage_log,
        template=template,
        completion=completion,
        invitation=invitation,
        buyer=buyer,
        buyer_link=buyer_link,
        sale=sale,
        closed_sale=closed_sale,
        payment=payment,
    )


def _rows(queryset, exclude=()):
    rows = []
    for row in queryset.order_by("pk").values():
        for key in exclude:
            row.pop(key, None)
        rows.append(row)
    return rows


def snapshot(world):
    """
    Every row the farm owns, as plain values, keyed by table. Two equal
    snapshots mean no request changed, added, removed or moved any of them.
    A row moved to another farm drops out of this farm's filter, so a
    "steal" shows up as a missing row, not as an unchanged one.
    """
    farm = world.farm
    through = FarmMembership.houses.through
    return {
        "farm": _rows(Farm.objects.filter(pk=farm.pk)),
        "users": _rows(User.objects.filter(pk__in=world.user_ids), exclude=("last_login",)),
        "memberships": _rows(FarmMembership.objects.filter(farm=farm)),
        "membership_houses": _rows(through.objects.filter(farmmembership__farm=farm)),
        "ownership_history": _rows(FarmOwnershipHistory.objects.filter(farm=farm)),
        "houses": _rows(House.objects.filter(farm=farm)),
        "batches": _rows(Batch.objects.filter(house__farm=farm)),
        "daily_records": _rows(DailyRecord.objects.filter(batch__house__farm=farm)),
        "weight_samples": _rows(WeightSample.objects.filter(batch__house__farm=farm)),
        "sales": _rows(SaleEvent.objects.filter(batch__house__farm=farm)),
        "payments": _rows(Payment.objects.filter(sale__batch__house__farm=farm)),
        "harvests": _rows(Harvest.objects.filter(batch__house__farm=farm)),
        "corrections": _rows(RecordCorrection.objects.filter(batch__house__farm=farm)),
        "feed_deliveries": _rows(FeedDelivery.objects.filter(farm=farm)),
        "inventory_items": _rows(InventoryItem.objects.filter(farm=farm)),
        "stock_ins": _rows(InventoryStockIn.objects.filter(item__farm=farm)),
        "usage_logs": _rows(InventoryUsageLog.objects.filter(item__farm=farm)),
        "task_templates": _rows(TaskTemplate.objects.filter(farm=farm)),
        "task_completions": _rows(TaskCompletion.objects.filter(template__farm=farm)),
        "invitations": _rows(Invitation.objects.filter(farm=farm)),
        "partner_links": _rows(FarmPartnerLink.objects.filter(farm=farm)),
        "alert_logs": _rows(AlertLog.objects.filter(farm=farm)),
    }


def snapshot_diff(before, after):
    """Names of the tables whose rows differ, for a readable failure."""
    return sorted(table for table in before if before[table] != after[table])
