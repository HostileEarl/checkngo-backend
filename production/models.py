# production/models.py
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.contrib.contenttypes.fields import GenericForeignKey


class OfflineSyncModel(models.Model):
    """
    Base for anything a worker records in the field.

    Three timestamps, three jobs:
      record_date / sample_date — the day the data DESCRIBES
      recorded_at               — when the worker typed it (client clock)
      created_at                — when the server received it

    The gap between the last two is the offline window. The UUID primary key
    is generated on the device, so a sync retry updates rather than duplicates.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="%(class)s_records",
    )
    recorded_at = models.DateTimeField(
        default=timezone.now,
        help_text="Client-supplied. When the worker actually entered this.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    EDIT_WINDOW_HOURS = 24

    class Meta:
        abstract = True

    @property
    def is_editable(self):
        """Fat-finger fixes allowed for a day; after that the record is evidence."""
        return timezone.now() < self.created_at + timedelta(hours=self.EDIT_WINDOW_HOURS)

    @property
    def sync_delay_seconds(self):
        if not self.created_at or not self.recorded_at:
            return None
        return (self.created_at - self.recorded_at).total_seconds()


class House(models.Model):
    """
    A physical shed. Reusable across batches — this is what makes
    "how does House 3 perform over time?" answerable.
    """

    farm = models.ForeignKey(
        "farms.Farm", on_delete=models.CASCADE, related_name="houses"
    )
    name = models.CharField(max_length=100)
    capacity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Maximum birds this house can hold.",
    )
    notes = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "production_house"
        ordering = ["farm", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "name"], name="unique_house_name_per_farm"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.farm.name})"

    @property
    def current_batch(self):
        return self.batches.filter(status=Batch.Status.ACTIVE).first()

    @property
    def is_occupied(self):
        return self.current_batch is not None


class Batch(models.Model):
    """
    One cohort of broilers, placement to harvest. The analytics unit —
    every chart in the system is scoped to a batch.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        HARVESTED = "HARVESTED", "Harvested"
        TERMINATED = "TERMINATED", "Terminated"
        
        

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    house = models.ForeignKey(House, on_delete=models.PROTECT, related_name="batches")
    batch_code = models.CharField(max_length=50, help_text="Human-facing, e.g. B-2026-03")
    breed = models.CharField(max_length=100, blank=True)

    initial_bird_count = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    start_date = models.DateField()
    expected_harvest_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    # Termination is an abnormal close — disease wipeout, or a batch that
    # should never have been placed. The reason is required at the API layer
    # so a batch missing from the FCR averages always has a documented cause.
    termination_reason = models.CharField(max_length=255, blank=True)
    terminated_at = models.DateTimeField(null=True, blank=True)
    terminated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches_terminated",
    )
    
    def terminate(self, reason, user=None):
        """
        Close a batch without a harvest. Excluded from FCR averages, so the
        reason is mandatory — an unexplained gap in the analytics is worse
        than no gap at all.
        """
        if not reason or not reason.strip():
            raise ValidationError("A reason is required to terminate a batch.")
        self.status = self.Status.TERMINATED
        self.termination_reason = reason.strip()
        self.terminated_at = timezone.now()
        self.terminated_by = user
        self.save(
            update_fields=[
                "status",
                "termination_reason",
                "terminated_at",
                "terminated_by",
            ]
        )

    # Denormalized running totals. Maintained by signal on DailyRecord save.
    # Deliberate trade: instant chart loads, at the cost of possible drift.
    # The DailyRecord rows are the source of truth; recalculate_totals()
    # rebuilds these from them if they ever disagree.
    total_mortality = models.PositiveIntegerField(default=0)
    total_feed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="batches_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "production_batch"
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["house", "batch_code"], name="unique_batch_code_per_house"
            ),
            # A house holds one live batch at a time. Partial unique index —
            # harvested batches are excluded, so the house can be refilled.
            models.UniqueConstraint(
                fields=["house"],
                condition=models.Q(status="ACTIVE"),
                name="one_active_batch_per_house",
            ),
        ]
        indexes = [
            models.Index(fields=["house", "status"], name="batch_house_status_idx"),
            models.Index(fields=["start_date"], name="batch_start_date_idx"),
        ]

    def __str__(self):
        return f"{self.batch_code} @ {self.house.name}"

    @property
    def farm(self):
        return self.house.farm

    def clean(self):
        if self.initial_bird_count and self.house_id:
            if self.initial_bird_count > self.house.capacity:
                raise ValidationError(
                    {"initial_bird_count": "Exceeds the house's stated capacity."}
                )
    @property
    def was_corrected(self):
        return self.corrections_for_this().exists()

    def corrections_for_this(self):
        from django.contrib.contenttypes.models import ContentType

        return RecordCorrection.objects.filter(
            content_type=ContentType.objects.get_for_model(self),
            object_id=self.id,
        )

    # --- Derived metrics: computed, never stored ---

    @property
    def current_bird_count(self):
        return max(self.initial_bird_count - self.total_mortality, 0)

    @property
    def mortality_rate(self):
        """Percentage of the placed flock lost to date."""
        if not self.initial_bird_count:
            return Decimal("0")
        return (
            Decimal(self.total_mortality) / Decimal(self.initial_bird_count)
        ) * 100

    @property
    def age_days(self):
        end = self.harvest.harvest_date if hasattr(self, "harvest") else timezone.localdate()
        return (end - self.start_date).days

    @property
    def feed_conversion_ratio(self):
        """
        FCR = feed consumed / live weight produced. Lower is better.

        Only computable after harvest — without a harvest weight there is
        no denominator. Returns None for active batches by design.
        """
        harvest = getattr(self, "harvest", None)
        if harvest is None or not harvest.total_weight_kg:
            return None
        return (self.total_feed_kg / harvest.total_weight_kg).quantize(Decimal("0.001"))

    def recalculate_totals(self):
        """
        Rebuild the denormalized fields from the underlying records.

        The four mortality causes are summed in SQL and added together —
        `mortality` is a Python property, so the database cannot see it.
        """
        agg = self.daily_records.aggregate(
            disease=models.Sum("mortality_disease"),
            heat=models.Sum("mortality_heat"),
            culled=models.Sum("mortality_culled"),
            unknown=models.Sum("mortality_unknown"),
            feed=models.Sum("feed_kg"),
        )
        self.total_mortality = (
            (agg["disease"] or 0)
            + (agg["heat"] or 0)
            + (agg["culled"] or 0)
            + (agg["unknown"] or 0)
        )
        self.total_feed_kg = agg["feed"] or Decimal("0")
        self.save(update_fields=["total_mortality", "total_feed_kg"])


class DailyRecord(OfflineSyncModel):
    """
    One entry per batch per day. Mortality is broken out by cause so the
    chart can show WHY birds were lost, not just how many.
    """

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="daily_records")
    record_date = models.DateField(db_index=True)

    mortality_disease = models.PositiveIntegerField(default=0)
    mortality_heat = models.PositiveIntegerField(default=0)
    mortality_culled = models.PositiveIntegerField(default=0)
    mortality_unknown = models.PositiveIntegerField(default=0)

    feed_kg = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "production_daily_record"
        ordering = ["-record_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "record_date"], name="unique_daily_record_per_batch"
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "record_date"], name="daily_batch_date_idx"),
        ]

    def __str__(self):
        return f"{self.batch.batch_code} — {self.record_date}"

    @property
    def mortality(self):
        return (
            self.mortality_disease
            + self.mortality_heat
            + self.mortality_culled
            + self.mortality_unknown
        )

    def clean(self):
        if self.record_date and self.batch_id:
            if self.record_date < self.batch.start_date:
                raise ValidationError(
                    {"record_date": "Cannot record a date before the batch started."}
                )
            if self.record_date > timezone.localdate():
                raise ValidationError({"record_date": "Cannot record a future date."})
            
    def corrections_for_this(self):
        from django.contrib.contenttypes.models import ContentType

        return RecordCorrection.objects.filter(
            content_type=ContentType.objects.get_for_model(self),
            object_id=self.id,
        )

    @property
    def was_corrected(self):
        return self.corrections_for_this().exists()


class WeightSample(OfflineSyncModel):
    """
    Weekly sample weighing. birds_weighed is kept because a 10-bird sample
    and a 50-bird sample carry very different confidence.
    """

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="weight_samples")
    sample_date = models.DateField(db_index=True)
    birds_weighed = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    average_grams = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "production_weight_sample"
        ordering = ["-sample_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "sample_date"], name="unique_weight_sample_per_day"
            ),
        ]

    def __str__(self):
        return f"{self.batch.batch_code} — {self.average_grams}g on {self.sample_date}"
    
    def corrections_for_this(self):
        from django.contrib.contenttypes.models import ContentType

        return RecordCorrection.objects.filter(
            content_type=ContentType.objects.get_for_model(self),
            object_id=self.id,
        )

    @property
    def was_corrected(self):
        return self.corrections_for_this().exists()

    @property
    def age_days(self):
        return (self.sample_date - self.batch.start_date).days


class Harvest(models.Model):
    """
    Closes a batch. This is where FCR becomes computable — before harvest
    there is no weight denominator.
    """

    batch = models.OneToOneField(Batch, on_delete=models.CASCADE, related_name="harvest")
    harvest_date = models.DateField()
    birds_harvested = models.PositiveIntegerField()
    total_weight_kg = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    revenue = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Gross sale value. Recorded after the fact, not transacted here.",
    )
    buyer_link = models.ForeignKey(
        "partners.FarmPartnerLink",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="harvests_purchased",
        help_text="Optional: the consumer this batch went to.",
    )
    notes = models.CharField(max_length=255, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="harvests_recorded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "production_harvest"
        ordering = ["-harvest_date"]

    def __str__(self):
        return f"{self.batch.batch_code} harvested {self.harvest_date}"

    @property
    def average_weight_kg(self):
        if not self.birds_harvested:
            return None
        return (self.total_weight_kg / self.birds_harvested).quantize(Decimal("0.001"))

    @property
    def revenue_per_kg(self):
        if self.revenue is None or not self.total_weight_kg:
            return None
        return (self.revenue / self.total_weight_kg).quantize(Decimal("0.01"))

    def clean(self):
        if self.batch_id and self.birds_harvested:
            if self.birds_harvested > self.batch.current_bird_count:
                raise ValidationError(
                    {"birds_harvested": "More birds harvested than the batch contains."}
                )


class FeedDelivery(OfflineSyncModel):
    """
    Stock in, attributable to a supplier. Farm-level rather than batch-level:
    a delivery arrives at the farm and is split across houses, so per-batch
    cost uses the average feed price over the period rather than tracing
    individual sacks.
    """

    class FeedType(models.TextChoices):
        STARTER = "STARTER", "Starter"
        GROWER = "GROWER", "Grower"
        FINISHER = "FINISHER", "Finisher"
        OTHER = "OTHER", "Other"

    farm = models.ForeignKey(
        "farms.Farm", on_delete=models.CASCADE, related_name="feed_deliveries"
    )
    supplier_link = models.ForeignKey(
        "partners.FarmPartnerLink",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="feed_deliveries",
    )
    delivery_date = models.DateField(db_index=True)
    feed_type = models.CharField(max_length=20, choices=FeedType.choices)
    quantity_kg = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Cost per kg.",
    )
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    invoice_ref = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "production_feed_delivery"
        ordering = ["-delivery_date"]
        indexes = [
            models.Index(fields=["farm", "delivery_date"], name="feed_farm_date_idx"),
        ]

    def __str__(self):
        return f"{self.quantity_kg}kg {self.get_feed_type_display()} — {self.delivery_date}"

    def save(self, *args, **kwargs):
        # Derive whichever cost field is missing, so either entry style works.
        if self.total_cost is None and self.unit_cost is not None:
            self.total_cost = (self.unit_cost * self.quantity_kg).quantize(Decimal("0.01"))
        elif self.unit_cost is None and self.total_cost is not None and self.quantity_kg:
            self.unit_cost = (self.total_cost / self.quantity_kg).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)
        
class RecordCorrection(models.Model):
    """
    Permanent log of a manager overriding the 24-hour lock.

    Records are immutable to the person who created them. A manager may
    override that, but never silently: the before-state, the after-state,
    the author, and a written reason are all captured here, and this row
    can never be edited or deleted.

    Corrections stop when a batch is harvested. At that point the FCR and
    feed margin have been calculated and may already have been reported —
    the books are closed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Generic target so one model covers DailyRecord and WeightSample.
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.PROTECT
    )
    object_id = models.UUIDField()
    target = GenericForeignKey("content_type", "object_id")

    # Denormalized for filtering and for surviving a cascade delete.
    batch = models.ForeignKey(
        "Batch", on_delete=models.CASCADE, related_name="corrections"
    )
    record_date = models.DateField(db_index=True)

    previous_values = models.JSONField(help_text="Full snapshot before the change.")
    new_values = models.JSONField()
    changed_fields = models.JSONField(default=list)

    reason = models.TextField()
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="corrections_made",
    )
    # Permanent ink — survives the account being deleted.
    corrected_by_name = models.CharField(max_length=150, blank=True)
    corrected_at = models.DateTimeField(default=timezone.now, db_index=True)

    MIN_REASON_LENGTH = 15

    class Meta:
        db_table = "production_record_correction"
        ordering = ["-corrected_at"]
        indexes = [
            models.Index(fields=["batch", "-corrected_at"], name="correction_batch_time_idx"),
            models.Index(fields=["content_type", "object_id"], name="correction_target_idx"),
        ]

    def __str__(self):
        return f"{self.batch.batch_code} {self.record_date} corrected by {self.corrected_by_name}"

    def save(self, *args, **kwargs):
        if self.pk and RecordCorrection.objects.filter(pk=self.pk).exists():
            raise ValueError("Correction records are immutable.")
        if self.corrected_by and not self.corrected_by_name:
            self.corrected_by_name = self.corrected_by.full_name
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
# Inventory — a lightweight store for farm consumables
# ─────────────────────────────────────────────────────────────
#
# Modelled on feed, not on mortality. Feed has a stock-in stream
# (FeedDelivery), a stock-out stream (DailyRecord.feed_kg), and a balance
# computed on read and never stored (see analytics.services and
# FeedStockView). Inventory is the same shape for anything else a worker
# draws down day to day — vaccines, disinfectant, litter, LPG:
#
#   InventoryItem      the thing tracked, plus its reorder level
#   InventoryStockIn   deliveries / manual top-ups   (manager/owner, online)
#   InventoryUsageLog  a worker drawing some down     (worker, offline)
#
# There is no quantity column. current_quantity = Σ stock-in − Σ usage,
# derived when asked. A denormalised column maintained by signal (the
# Batch.total_mortality approach) buys instant reads at the cost of drift;
# inventory is read in two low-traffic places only — the usage form and
# the low-stock alert poll — so it does not earn that trade.


_QTY = models.DecimalField(max_digits=12, decimal_places=2)


def _item_sum_subquery(model, field):
    """Σ `field` over `model` rows belonging to the outer InventoryItem, or 0."""
    return Coalesce(
        models.Subquery(
            model.objects.filter(item=models.OuterRef("pk"))
            .values("item")
            .annotate(total=models.Sum(field))
            .values("total")[:1],
            output_field=_QTY,
        ),
        models.Value(Decimal("0")),
        output_field=_QTY,
    )


def _item_count_subquery(model):
    """How many `model` rows belong to the outer InventoryItem."""
    return Coalesce(
        models.Subquery(
            model.objects.filter(item=models.OuterRef("pk"))
            .values("item")
            .annotate(c=models.Count("pk"))
            .values("c")[:1],
            output_field=models.IntegerField(),
        ),
        models.Value(0),
        output_field=models.IntegerField(),
    )


class InventoryItemQuerySet(models.QuerySet):
    def with_levels(self):
        """
        Annotate the running balance without a fan-out join.

        Two reverse relations summed in one query would multiply their rows
        together — the Sum-with-multiple-joins trap that recalculate_totals
        sidesteps by staying on a single table. Independent subqueries keep
        each aggregate honest.

        The aliases are prefixed `qty_` / suffixed `_count` rather than
        reusing the property names: an annotation alias that clashes with a
        read-only property breaks row hydration (the ORM tries to setattr
        it). Callers that always run through with_levels() — the low-stock
        alert — read these directly; the serializer falls back to the
        properties for a bare instance.
        """
        stocked = _item_sum_subquery(InventoryStockIn, "quantity")
        used = _item_sum_subquery(InventoryUsageLog, "quantity_used")
        return self.annotate(
            qty_stocked_in=stocked,
            qty_used=used,
            qty_current=models.ExpressionWrapper(
                stocked - used, output_field=_QTY
            ),
            stock_in_count=_item_count_subquery(InventoryStockIn),
            usage_count=_item_count_subquery(InventoryUsageLog),
        )


class InventoryItem(models.Model):
    farm = models.ForeignKey(
        "farms.Farm", on_delete=models.CASCADE, related_name="inventory_items"
    )
    name = models.CharField(max_length=100)
    unit = models.CharField(
        max_length=20, help_text="How it is counted: kg, litre, sack, dose."
    )
    low_stock_threshold = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Warn once the balance falls to or below this.",
    )

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inventory_items_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InventoryItemQuerySet.as_manager()

    class Meta:
        db_table = "production_inventory_item"
        ordering = ["farm", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "name"], name="unique_inventory_item_name_per_farm"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.farm.name})"

    # --- Derived, never stored. Same treatment as the feed balance. ---

    @property
    def stocked_in(self):
        return self.stock_ins.aggregate(t=models.Sum("quantity"))["t"] or Decimal("0")

    @property
    def used_total(self):
        return (
            self.usage_logs.aggregate(t=models.Sum("quantity_used"))["t"]
            or Decimal("0")
        )

    @property
    def current_quantity(self):
        return self.stocked_in - self.used_total

    @property
    def never_stocked(self):
        """
        No stock-in and no usage — a brand-new item nobody has touched. It
        is not "low", it has simply never been stocked, and warning on it
        the moment a manager adds it would be noise.
        """
        return not self.stock_ins.exists() and not self.usage_logs.exists()

    @property
    def is_low(self):
        if self.never_stocked:
            return False
        return self.current_quantity <= self.low_stock_threshold


class InventoryStockIn(models.Model):
    """A delivery or manual top-up. Manager/owner only, recorded online at the office."""

    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, related_name="stock_ins"
    )
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    stock_in_date = models.DateField(db_index=True)
    note = models.CharField(max_length=255, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inventory_stock_ins",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "production_inventory_stock_in"
        ordering = ["-stock_in_date", "-created_at"]
        indexes = [
            models.Index(
                fields=["item", "stock_in_date"], name="inv_stockin_item_date_idx"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} {self.item.unit} of {self.item.name} on {self.stock_in_date}"


class InventoryUsageLog(OfflineSyncModel):
    """
    A worker drawing some of an item down.

    An event, not a one-per-day record: a worker can log the same item
    several times in a day, so the only identity is the client-generated
    UUID primary key it inherits from OfflineSyncModel — there is
    deliberately no (item, date, worker) uniqueness. A retried POST
    carrying the same UUID updates in place rather than raising an
    IntegrityError, and the inherited 24-hour edit lock applies exactly as
    it does to a daily mortality record.
    """

    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, related_name="usage_logs"
    )
    quantity_used = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    usage_date = models.DateField(db_index=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "production_inventory_usage_log"
        ordering = ["-usage_date", "-created_at"]
        indexes = [
            models.Index(
                fields=["item", "usage_date"], name="inv_usage_item_date_idx"
            ),
        ]

    def __str__(self):
        return (
            f"{self.quantity_used} {self.item.unit} of {self.item.name} "
            f"on {self.usage_date}"
        )

    def clean(self):
        if self.usage_date and self.usage_date > timezone.localdate():
            raise ValidationError({"usage_date": "Cannot record a future date."})


# ─────────────────────────────────────────────────────────────
# Daily routine — a fixed checklist, not task assignment
# ─────────────────────────────────────────────────────────────
#
# A poultry farm runs the same round every day: morning feed, health
# check, water system check, and so on. The manager defines that routine
# once (TaskTemplate); any worker ticks an item off for the day
# (TaskCompletion). There are deliberately no due dates, no per-worker
# assignment, and no house scoping — the task is done or it is not, farm
# wide.
#
# Same shape as inventory: a manager-configured parent record and an
# offline-capable child event. TaskCompletion extends OfflineSyncModel for
# the client UUID PK and the 24-hour edit lock, exactly like
# InventoryUsageLog. The one difference is the unique(template,
# completion_date) constraint — a completion carries no content, so a
# second device ticking the same item the same day is absorbed, not
# rejected.


class TaskTemplate(models.Model):
    """One item in a farm's daily routine. Defined once by an owner or manager."""

    farm = models.ForeignKey(
        "farms.Farm", on_delete=models.CASCADE, related_name="task_templates"
    )
    name = models.CharField(max_length=150)
    suggested_time = models.CharField(
        max_length=50,
        blank=True,
        help_text='Free text — "6:00 AM", or "after morning rounds".',
    )
    order = models.PositiveIntegerField(default=0, help_text="Display sequence.")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_templates_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "production_task_template"
        ordering = ["farm", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "name"], name="unique_task_template_name_per_farm"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.farm.name})"


class TaskCompletion(OfflineSyncModel):
    """
    A routine item ticked off on a given day.

    One per template per day, farm-wide: if two workers both tick "morning
    feed", the second submission updates the first rather than creating a
    duplicate — a shared routine item is simply done or not. The template
    FK is PROTECT so completion history survives a template being retired;
    that is what TaskTemplate.is_active is for.
    """

    template = models.ForeignKey(
        TaskTemplate, on_delete=models.PROTECT, related_name="completions"
    )
    completion_date = models.DateField(db_index=True)

    class Meta:
        db_table = "production_task_completion"
        ordering = ["-completion_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "completion_date"],
                name="unique_completion_per_template_day",
            ),
        ]

    def __str__(self):
        return f"{self.template.name} — {self.completion_date}"

    def clean(self):
        if self.completion_date and self.completion_date > timezone.localdate():
            raise ValidationError(
                {"completion_date": "Cannot record a future date."}
            )