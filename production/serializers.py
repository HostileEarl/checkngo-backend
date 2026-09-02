# production/serializers.py
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from partners.models import FarmPartnerLink

from .models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    RecordCorrection,
    TaskCompletion,
    TaskTemplate,
    WeightSample,
)


# ─────────────────────────────────────────────────────────────
# Houses
# ─────────────────────────────────────────────────────────────


class HouseSerializer(serializers.ModelSerializer):
    is_occupied = serializers.BooleanField(read_only=True)
    current_batch_code = serializers.SerializerMethodField()

    class Meta:
        model = House
        fields = [
            "id",
            "name",
            "capacity",
            "notes",
            "is_active",
            "is_occupied",
            "current_batch_code",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_current_batch_code(self, obj):
        batch = obj.current_batch
        return batch.batch_code if batch else None

    def validate_name(self, value):
        farm = self.context["farm"]
        qs = House.objects.filter(farm=farm, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A house with this name already exists on this farm."
            )
        return value.strip()


# ─────────────────────────────────────────────────────────────
# Batches
# ─────────────────────────────────────────────────────────────


class BatchSerializer(serializers.ModelSerializer):
    """Read view. All derived metrics are computed, never stored."""

    house_name = serializers.CharField(source="house.name", read_only=True)
    current_bird_count = serializers.IntegerField(read_only=True)
    mortality_rate_pct = serializers.SerializerMethodField()
    feed_conversion_ratio = serializers.SerializerMethodField()
    age_days = serializers.IntegerField(read_only=True)
    latest_weight_grams = serializers.SerializerMethodField()
    
    # ADDED: Resolves the user who terminated the batch
    terminated_by_name = serializers.CharField(
        source="terminated_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = Batch
        fields = [
            "id",
            "house",
            "house_name",
            "batch_code",
            "breed",
            "initial_bird_count",
            "current_bird_count",
            "start_date",
            "expected_harvest_date",
            "status",
            "termination_reason",
            "terminated_at",
            "terminated_by_name",
            "total_mortality",
            "total_feed_kg",
            "mortality_rate_pct",
            "feed_conversion_ratio",
            "age_days",
            "latest_weight_grams",
            "created_at",
        ]
        
        # ADDED: termination_reason and terminated_at to read_only
        read_only_fields = [
            "id",
            "status",
            "termination_reason",
            "terminated_at",
            "total_mortality",
            "total_feed_kg",
            "created_at",
        ]

    def get_mortality_rate_pct(self, obj) -> str:
        return str(obj.mortality_rate.quantize(Decimal("0.01")))

    def get_feed_conversion_ratio(self, obj) -> str | None:
        fcr = obj.feed_conversion_ratio
        return str(fcr) if fcr is not None else None

    def get_latest_weight_grams(self, obj) -> str | None:
        sample = obj.weight_samples.first()
        return str(sample.average_grams) if sample else None


class BatchCreateSerializer(serializers.ModelSerializer):
    """
    Placement of a new cohort. Owner/manager only — bird counts and
    expected revenue are business decisions, not data entry.
    """

    class Meta:
        model = Batch
        fields = [
            "id",
            "house",
            "batch_code",
            "breed",
            "initial_bird_count",
            "start_date",
            "expected_harvest_date",
        ]
        read_only_fields = ["id"]

    def validate_house(self, value):
        farm = self.context["farm"]
        if value.farm_id != farm.pk:
            raise serializers.ValidationError("That house belongs to another farm.")
        if not value.is_active:
            raise serializers.ValidationError("That house is not in service.")
        return value

    def validate_start_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("A batch cannot start in the future.")
        return value

    def validate(self, attrs):
        house = attrs["house"]
        count = attrs["initial_bird_count"]

        if count > house.capacity:
            raise serializers.ValidationError(
                {
                    "initial_bird_count": (
                        f"{house.name} holds at most {house.capacity} birds."
                    )
                }
            )

        # The database enforces this too via a partial unique index; checking
        # here turns an IntegrityError into a readable 400.
        if house.is_occupied:
            raise serializers.ValidationError(
                {
                    "house": (
                        f"{house.name} is occupied by batch "
                        f"{house.current_batch.batch_code}. Harvest it first."
                    )
                }
            )

        if attrs.get("expected_harvest_date") and attrs["expected_harvest_date"] <= attrs["start_date"]:
            raise serializers.ValidationError(
                {"expected_harvest_date": "Must fall after the start date."}
            )

        return attrs

    def create(self, validated_data):
        return Batch.objects.create(
            created_by=self.context["request"].user, **validated_data
        )


# ─────────────────────────────────────────────────────────────
# Daily records
# ─────────────────────────────────────────────────────────────


class DailyRecordSerializer(serializers.ModelSerializer):
    """
    One entry per batch per day.

    The UUID primary key is client-supplied so an offline device can generate
    it before syncing — that is what makes a retry idempotent rather than
    duplicating the day's data.
    """

    # ModelSerializer forces a primary-key field to read_only=True by
    # default, regardless of extra_kwargs — that override did nothing, and
    # the client's UUID was silently discarded in favour of a server-
    # generated one. Declaring it explicitly here bypasses that inference.
    id = serializers.UUIDField(required=False)
    mortality = serializers.IntegerField(read_only=True)
    is_editable = serializers.BooleanField(read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )
    sync_delay_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = DailyRecord
        fields = [
            "id",
            "batch",
            "record_date",
            "mortality",
            "mortality_disease",
            "mortality_heat",
            "mortality_culled",
            "mortality_unknown",
            "feed_kg",
            "notes",
            "recorded_by",
            "recorded_by_name",
            "recorded_at",
            "created_at",
            "is_editable",
            "sync_delay_seconds",
            "was_corrected",
        ]
        read_only_fields = ["batch", "recorded_by", "created_at"]
        was_corrected = serializers.BooleanField(read_only=True)

    def validate_record_date(self, value):
        batch = self.context["batch"]
        if value < batch.start_date:
            raise serializers.ValidationError(
                "That date falls before the batch was placed."
            )
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future date.")
        return value

    def validate(self, attrs):
        batch = self.context["batch"]

        if batch.status != Batch.Status.ACTIVE:
            raise serializers.ValidationError(
                "This batch is closed and accepts no further records."
            )

        deaths = sum(
            attrs.get(field, getattr(self.instance, field, 0) or 0)
            for field in (
                "mortality_disease",
                "mortality_heat",
                "mortality_culled",
                "mortality_unknown",
            )
        )

        # A day cannot kill more birds than are alive. Exclude this record's
        # own prior contribution when editing, or a correction looks like an
        # addition.
        alive = batch.current_bird_count
        if self.instance:
            alive += self.instance.mortality
        if deaths > alive:
            raise serializers.ValidationError(
                {"mortality_unknown": f"Only {alive} birds remain in this batch."}
            )

        return attrs

    def update(self, instance, validated_data):
        # The 24-hour window has teeth: after it closes, the record is
        # evidence, and correcting it is an administrative act.
        if not instance.is_editable:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "This record is older than 24 hours and is locked. "
                        "Ask a manager to submit a correction."
                    )
                }
            )
        validated_data.pop("record_date", None)  # the day itself is fixed
        return super().update(instance, validated_data)

    def create(self, validated_data):
        return DailyRecord.objects.create(
            batch=self.context["batch"],
            recorded_by=self.context["request"].user,
            **validated_data,
        )


class DailyRecordBulkSyncSerializer(serializers.Serializer):
    """
    Offline sync: a week of backlog in one round trip.

    Per-record outcomes rather than all-or-nothing — one bad row from a
    field device should not reject six good days. Each record is written in
    its own savepoint so a failure rolls back only itself.
    """

    records = DailyRecordSerializer(many=True)

    def validate_records(self, value):
        if not value:
            raise serializers.ValidationError("No records supplied.")
        if len(value) > 60:
            raise serializers.ValidationError(
                "Sync at most 60 records per request."
            )
        dates = [r["record_date"] for r in value]
        if len(dates) != len(set(dates)):
            raise serializers.ValidationError(
                "The payload contains two entries for the same date."
            )
        return value

    def save(self, **kwargs):
        batch = self.context["batch"]
        request = self.context["request"]
        user = request.user
        membership = getattr(request, "membership", None)

        created, updated, failed = [], [], []

        for payload in self.validated_data["records"]:
            record_date = payload["record_date"]
            supplied_id = payload.get("id")

            # House-level write scoping. Every record in one payload is for
            # the same batch, so this rejects the whole backlog per record
            # rather than 403-ing the request.
            if membership is not None and not membership.may_write_to_house(
                batch.house
            ):
                failed.append(
                    {
                        "record_date": str(record_date),
                        "error": (
                            f"You are not assigned to {batch.house.name}. "
                            "Ask your manager to assign you."
                        ),
                    }
                )
                continue

            try:
                with transaction.atomic():
                    # Identity is the client-generated id, not the date — that
                    # is what makes a retry idempotent. (batch, record_date)
                    # is a fallback only, for a caller that never supplied one.
                    if supplied_id is not None:
                        existing = DailyRecord.objects.filter(
                            pk=supplied_id, batch=batch
                        ).first()
                    else:
                        existing = DailyRecord.objects.filter(
                            batch=batch, record_date=record_date
                        ).first()

                    if existing is None:
                        # An id was supplied and doesn't exist yet, but a
                        # DIFFERENT record may already occupy this date —
                        # two devices, or two ids, both claiming the same
                        # day. "One entry per batch per day" is a business
                        # rule, not just an idempotency mechanism, so this
                        # is a genuine conflict, not a record to merge into:
                        # reject it with a message worth reading, rather
                        # than letting the unique constraint surface a raw
                        # IntegrityError below.
                        date_conflict = (
                            DailyRecord.objects.filter(
                                batch=batch, record_date=record_date
                            ).first()
                            if supplied_id is not None
                            else None
                        )
                        if date_conflict is not None:
                            failed.append(
                                {
                                    "record_date": str(record_date),
                                    "error": (
                                        "A record already exists for this "
                                        "date under a different id."
                                    ),
                                }
                            )
                            continue

                        record = DailyRecord.objects.create(
                            batch=batch, recorded_by=user, **payload
                        )
                        created.append(str(record.id))
                    elif existing.is_editable:
                        # Overwrite on re-sync — the device is the source of
                        # truth for a day it already reported.
                        for field, val in payload.items():
                            if field != "id":
                                setattr(existing, field, val)
                        existing.recorded_by = user
                        existing.save()
                        updated.append(str(existing.id))
                    else:
                        failed.append(
                            {
                                "record_date": str(record_date),
                                "error": "Locked — older than 24 hours.",
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                failed.append({"record_date": str(record_date), "error": str(exc)})

        batch.recalculate_totals()

        return {
            "created": created,
            "updated": updated,
            "failed": failed,
            "batch_totals": {
                "total_mortality": batch.total_mortality,
                "total_feed_kg": str(batch.total_feed_kg),
                "current_bird_count": batch.current_bird_count,
            },
        }


# ─────────────────────────────────────────────────────────────
# Weight samples
# ─────────────────────────────────────────────────────────────


class WeightSampleSerializer(serializers.ModelSerializer):
    # Same fix as DailyRecordSerializer: a primary-key field is forced
    # read_only=True by ModelSerializer's field-building regardless of
    # extra_kwargs, which silently discarded the client-supplied id.
    id = serializers.UUIDField(required=False)
    age_days = serializers.IntegerField(read_only=True)
    is_editable = serializers.BooleanField(read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = WeightSample
        fields = [
            "id",
            "batch",
            "sample_date",
            "birds_weighed",
            "average_grams",
            "age_days",
            "notes",
            "recorded_by",
            "recorded_by_name",
            "recorded_at",
            "created_at",
            "is_editable",
        ]
        read_only_fields = ["batch", "recorded_by", "created_at"]

    def validate_sample_date(self, value):
        batch = self.context["batch"]
        if value < batch.start_date:
            raise serializers.ValidationError("Falls before the batch was placed.")
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future date.")
        return value

    def validate_birds_weighed(self, value):
        batch = self.context["batch"]
        if value > batch.current_bird_count:
            raise serializers.ValidationError(
                f"Only {batch.current_bird_count} birds remain in this batch."
            )
        return value

    def update(self, instance, validated_data):
        if not instance.is_editable:
            raise serializers.ValidationError(
                {"detail": "This sample is locked. Ask a manager for a correction."}
            )
        return super().update(instance, validated_data)

    def create(self, validated_data):
        return WeightSample.objects.create(
            batch=self.context["batch"],
            recorded_by=self.context["request"].user,
            **validated_data,
        )


# ─────────────────────────────────────────────────────────────
# Harvest
# ─────────────────────────────────────────────────────────────


class HarvestSerializer(serializers.ModelSerializer):
    """
    Closes the batch and unlocks FCR. Enforces the bird-count ceiling the
    model's clean() declares but the ORM never runs.
    """

    average_weight_kg = serializers.SerializerMethodField()
    revenue_per_kg = serializers.SerializerMethodField()
    feed_conversion_ratio = serializers.SerializerMethodField()
    batch_code = serializers.CharField(source="batch.batch_code", read_only=True)

    class Meta:
        model = Harvest
        fields = [
            "id",
            "batch",
            "batch_code",
            "harvest_date",
            "birds_harvested",
            "total_weight_kg",
            "revenue",
            "buyer_link",
            "notes",
            "average_weight_kg",
            "revenue_per_kg",
            "feed_conversion_ratio",
            "recorded_by",
            "created_at",
        ]
        read_only_fields = ["id", "batch", "recorded_by", "created_at"]

    def get_average_weight_kg(self, obj):
        val = obj.average_weight_kg
        return str(val) if val is not None else None

    def get_revenue_per_kg(self, obj):
        val = obj.revenue_per_kg
        return str(val) if val is not None else None

    def get_feed_conversion_ratio(self, obj):
        fcr = obj.batch.feed_conversion_ratio
        return str(fcr) if fcr is not None else None

    def validate_buyer_link(self, value):
        if value is None:
            return value
        farm = self.context["farm"]
        if value.farm_id != farm.pk:
            raise serializers.ValidationError("That buyer is not linked to this farm.")
        if value.link_type != FarmPartnerLink.LinkType.CONSUMER:
            raise serializers.ValidationError("That partner is not registered as a buyer.")
        return value

    def validate_harvest_date(self, value):
        batch = self.context["batch"]
        if value < batch.start_date:
            raise serializers.ValidationError("Falls before the batch was placed.")
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot harvest in the future.")
        return value

    def validate(self, attrs):
        batch = self.context["batch"]

        if batch.status != Batch.Status.ACTIVE:
            raise serializers.ValidationError("This batch has already been closed.")

        # The gap the model's clean() leaves open, closed here.
        harvested = attrs["birds_harvested"]
        if harvested > batch.current_bird_count:
            raise serializers.ValidationError(
                {
                    "birds_harvested": (
                        f"Only {batch.current_bird_count} birds remain "
                        f"({batch.initial_bird_count} placed, "
                        f"{batch.total_mortality} lost)."
                    )
                }
            )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        batch = self.context["batch"]
        harvest = Harvest.objects.create(
            batch=batch,
            recorded_by=self.context["request"].user,
            **validated_data,
        )
        batch.status = Batch.Status.HARVESTED
        batch.save(update_fields=["status"])
        # The house is now free — the partial unique index permits a new batch.
        return harvest


# ─────────────────────────────────────────────────────────────
# Feed
# ─────────────────────────────────────────────────────────────


class FeedDeliverySerializer(serializers.ModelSerializer):
    supplier_name = serializers.SerializerMethodField()
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = FeedDelivery
        fields = [
            "id",
            "farm",
            "supplier_link",
            "supplier_name",
            "delivery_date",
            "feed_type",
            "quantity_kg",
            "unit_cost",
            "total_cost",
            "invoice_ref",
            "notes",
            "recorded_by",
            "recorded_by_name",
            "recorded_at",
            "created_at",
        ]
        read_only_fields = ["farm", "recorded_by", "created_at"]
        extra_kwargs = {"id": {"required": False}}

    def get_supplier_name(self, obj):
        if not obj.supplier_link:
            return None
        return obj.supplier_link.business_name or obj.supplier_link.partner.full_name

    def validate_supplier_link(self, value):
        if value is None:
            return value
        farm = self.context["farm"]
        if value.farm_id != farm.pk:
            raise serializers.ValidationError("That supplier is not linked to this farm.")
        if value.link_type != FarmPartnerLink.LinkType.SUPPLIER:
            raise serializers.ValidationError("That partner is not registered as a supplier.")
        if not value.is_active:
            raise serializers.ValidationError("That supplier link is inactive.")
        return value

    def validate_delivery_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future delivery.")
        return value

    def validate(self, attrs):
        if attrs.get("unit_cost") is None and attrs.get("total_cost") is None:
            # Not an error — cost is optional. The model derives whichever
            # is missing when only one is supplied.
            pass
        return attrs

    def create(self, validated_data):
        return FeedDelivery.objects.create(
            farm=self.context["farm"],
            recorded_by=self.context["request"].user,
            **validated_data,
        )
        
class RecordCorrectionSerializer(serializers.ModelSerializer):
    """Read view of the correction log."""

    batch_code = serializers.CharField(source="batch.batch_code", read_only=True)
    target_type = serializers.SerializerMethodField()

    class Meta:
        model = RecordCorrection
        fields = [
            "id",
            "batch",
            "batch_code",
            "record_date",
            "target_type",
            "object_id",
            "previous_values",
            "new_values",
            "changed_fields",
            "reason",
            "corrected_by_name",
            "corrected_at",
        ]
        read_only_fields = fields

    def get_target_type(self, obj) -> str:
        return obj.content_type.model


class DailyRecordCorrectionSerializer(serializers.Serializer):
    """
    Manager override of a locked daily record.

    Distinct from PATCH: PATCH serves the original worker inside the
    24-hour window and leaves no trace. This writes a permanent log entry.
    """

    mortality_disease = serializers.IntegerField(required=False, min_value=0)
    mortality_heat = serializers.IntegerField(required=False, min_value=0)
    mortality_culled = serializers.IntegerField(required=False, min_value=0)
    mortality_unknown = serializers.IntegerField(required=False, min_value=0)
    feed_kg = serializers.DecimalField(
        max_digits=8, decimal_places=2, required=False, min_value=Decimal("0")
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=255)
    reason = serializers.CharField()

    CORRECTABLE_FIELDS = [
        "mortality_disease",
        "mortality_heat",
        "mortality_culled",
        "mortality_unknown",
        "feed_kg",
        "notes",
    ]

    def validate_reason(self, value):
        cleaned = value.strip()
        if len(cleaned) < RecordCorrection.MIN_REASON_LENGTH:
            raise serializers.ValidationError(
                f"Give a real explanation — at least "
                f"{RecordCorrection.MIN_REASON_LENGTH} characters."
            )
        return cleaned

    def validate(self, attrs):
        record = self.context["record"]
        batch = record.batch

        # Ruling 2: the books close at harvest.
        if batch.status != Batch.Status.ACTIVE:
            raise serializers.ValidationError(
                {
                    "detail": (
                        f"Batch {batch.batch_code} is {batch.status.lower()}. "
                        "Its figures are final and cannot be corrected."
                    )
                }
            )

        supplied = {k: v for k, v in attrs.items() if k in self.CORRECTABLE_FIELDS}
        if not supplied:
            raise serializers.ValidationError(
                {"detail": "No fields to correct."}
            )

        # Mortality ceiling, excluding this record's own current contribution.
        new_deaths = sum(
            attrs.get(f, getattr(record, f))
            for f in (
                "mortality_disease",
                "mortality_heat",
                "mortality_culled",
                "mortality_unknown",
            )
        )
        available = batch.current_bird_count + record.mortality
        if new_deaths > available:
            raise serializers.ValidationError(
                {"detail": f"Only {available} birds are accounted for on this date."}
            )

        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        from django.contrib.contenttypes.models import ContentType

        record = self.context["record"]
        user = self.context["request"].user

        previous = {f: str(getattr(record, f)) for f in self.CORRECTABLE_FIELDS}

        changed = []
        for field in self.CORRECTABLE_FIELDS:
            if field in self.validated_data:
                new_value = self.validated_data[field]
                if str(getattr(record, field)) != str(new_value):
                    setattr(record, field, new_value)
                    changed.append(field)

        if not changed:
            raise serializers.ValidationError(
                {"detail": "The supplied values match the existing record."}
            )

        record.save()
        record.refresh_from_db()

        new_values = {f: str(getattr(record, f)) for f in self.CORRECTABLE_FIELDS}

        correction = RecordCorrection.objects.create(
            content_type=ContentType.objects.get_for_model(record),
            object_id=record.id,
            batch=record.batch,
            record_date=record.record_date,
            previous_values=previous,
            new_values=new_values,
            changed_fields=changed,
            reason=self.validated_data["reason"],
            corrected_by=user,
        )
        # The post_save signal on DailyRecord already refreshed batch totals.
        return correction


# ─────────────────────────────────────────────────────────────
# Inventory
# ─────────────────────────────────────────────────────────────


class InventoryItemSerializer(serializers.ModelSerializer):
    """
    Read + write for an item. The balance fields are derived, never stored —
    read from the queryset annotation `with_levels()` adds, falling back to
    the model properties for a bare instance (e.g. straight after create).
    """

    current_quantity = serializers.SerializerMethodField()
    stocked_in = serializers.SerializerMethodField()
    used_total = serializers.SerializerMethodField()
    is_low = serializers.SerializerMethodField()
    never_stocked = serializers.SerializerMethodField()

    class Meta:
        model = InventoryItem
        fields = [
            "id",
            "name",
            "unit",
            "low_stock_threshold",
            "is_active",
            "current_quantity",
            "stocked_in",
            "used_total",
            "is_low",
            "never_stocked",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def _level(self, obj, annotated_attr, prop_name):
        val = getattr(obj, annotated_attr, None)
        if val is None:
            val = getattr(obj, prop_name)
        return str(val)

    def get_current_quantity(self, obj) -> str:
        return self._level(obj, "qty_current", "current_quantity")

    def get_stocked_in(self, obj) -> str:
        return self._level(obj, "qty_stocked_in", "stocked_in")

    def get_used_total(self, obj) -> str:
        return self._level(obj, "qty_used", "used_total")

    def get_never_stocked(self, obj) -> bool:
        stock_in_count = getattr(obj, "stock_in_count", None)
        usage_count = getattr(obj, "usage_count", None)
        if stock_in_count is not None and usage_count is not None:
            return stock_in_count == 0 and usage_count == 0
        return obj.never_stocked

    def get_is_low(self, obj) -> bool:
        if self.get_never_stocked(obj):
            return False
        current = getattr(obj, "qty_current", None)
        if current is None:
            current = obj.current_quantity
        return current <= obj.low_stock_threshold

    def validate_name(self, value):
        farm = self.context["farm"]
        qs = InventoryItem.objects.filter(farm=farm, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "An item with this name already exists on this farm."
            )
        return value.strip()

    def create(self, validated_data):
        return InventoryItem.objects.create(
            farm=self.context["farm"],
            created_by=self.context["request"].user,
            **validated_data,
        )


class InventoryStockInSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = InventoryStockIn
        fields = [
            "id",
            "item",
            "quantity",
            "stock_in_date",
            "note",
            "recorded_by",
            "recorded_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "item", "recorded_by", "created_at"]

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_stock_in_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future delivery.")
        return value

    def create(self, validated_data):
        return InventoryStockIn.objects.create(
            item=self.context["item"],
            recorded_by=self.context["request"].user,
            **validated_data,
        )


class InventoryUsageLogSerializer(serializers.ModelSerializer):
    """
    One worker drawing an item down.

    `id` is declared explicitly for the same reason DailyRecordSerializer
    does it: ModelSerializer forces a primary-key field to read_only, which
    would discard the client-generated UUID and break idempotency. The
    identity of a usage log is that UUID alone — it is an event, not a
    one-per-day record, so there is no date fallback and a worker may log
    the same item any number of times in a day.
    """

    id = serializers.UUIDField(required=False)
    item_name = serializers.CharField(source="item.name", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)
    is_editable = serializers.BooleanField(read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )
    sync_delay_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = InventoryUsageLog
        fields = [
            "id",
            "item",
            "item_name",
            "unit",
            "quantity_used",
            "usage_date",
            "notes",
            "recorded_by",
            "recorded_by_name",
            "recorded_at",
            "created_at",
            "is_editable",
            "sync_delay_seconds",
        ]
        read_only_fields = ["recorded_by", "created_at"]

    def validate_item(self, value):
        farm = self.context["farm"]
        if value.farm_id != farm.pk:
            raise serializers.ValidationError("That item belongs to another farm.")
        if not value.is_active:
            raise serializers.ValidationError("That item is no longer in use.")
        return value

    def validate_quantity_used(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_usage_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future date.")
        return value

    def update(self, instance, validated_data):
        # The 24-hour lock has teeth here exactly as it does on a daily
        # mortality record: after it closes the entry is evidence.
        if not instance.is_editable:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "This record is older than 24 hours and is locked. "
                        "Ask a manager to make the change."
                    )
                }
            )
        validated_data.pop("item", None)  # the item is fixed once logged
        validated_data.pop("usage_date", None)  # so is the day
        return super().update(instance, validated_data)

    def create(self, validated_data):
        return InventoryUsageLog.objects.create(
            recorded_by=self.context["request"].user, **validated_data
        )


class InventoryUsageBulkSyncSerializer(serializers.Serializer):
    """
    Offline sync for usage logs — a backlog of events in one round trip.

    Simpler than the daily-record equivalent: identity is purely the
    client UUID, so there is no (batch, date) fallback and no
    same-date-different-id conflict to resolve. Each record is written in
    its own savepoint; one bad row does not reject the rest.
    """

    records = InventoryUsageLogSerializer(many=True)

    def validate_records(self, value):
        if not value:
            raise serializers.ValidationError("No records supplied.")
        if len(value) > 60:
            raise serializers.ValidationError(
                "Sync at most 60 records per request."
            )
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        created, updated, failed = [], [], []

        for payload in self.validated_data["records"]:
            supplied_id = payload.get("id")
            try:
                with transaction.atomic():
                    existing = None
                    if supplied_id is not None:
                        existing = (
                            InventoryUsageLog.objects.select_related("item")
                            .filter(pk=supplied_id)
                            .first()
                        )

                    if existing is None:
                        record = InventoryUsageLog.objects.create(
                            recorded_by=user, **payload
                        )
                        created.append(str(record.id))
                    elif existing.is_editable:
                        # The device is the source of truth for an event it
                        # already reported — overwrite on re-sync.
                        for field, val in payload.items():
                            if field in ("id", "item", "usage_date"):
                                continue
                            setattr(existing, field, val)
                        existing.recorded_by = user
                        existing.save()
                        updated.append(str(existing.id))
                    else:
                        failed.append(
                            {
                                "id": str(supplied_id),
                                "error": "Locked — older than 24 hours.",
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "id": str(supplied_id) if supplied_id else None,
                        "error": str(exc),
                    }
                )

        return {"created": created, "updated": updated, "failed": failed}


# ─────────────────────────────────────────────────────────────
# Daily routine
# ─────────────────────────────────────────────────────────────


class TaskTemplateSerializer(serializers.ModelSerializer):
    """One item in the farm's daily routine. Owner/manager writes."""

    class Meta:
        model = TaskTemplate
        fields = [
            "id",
            "name",
            "suggested_time",
            "order",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        farm = self.context["farm"]
        qs = TaskTemplate.objects.filter(farm=farm, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A routine item with this name already exists on this farm."
            )
        return value.strip()

    def create(self, validated_data):
        return TaskTemplate.objects.create(
            farm=self.context["farm"],
            created_by=self.context["request"].user,
            **validated_data,
        )


class TaskCompletionSerializer(serializers.ModelSerializer):
    """
    A routine item ticked off for a day.

    `id` is declared explicitly for the same reason DailyRecordSerializer
    does it: ModelSerializer forces a primary-key field to read_only, which
    would discard the client-generated UUID and break offline idempotency.
    A completion has no editable content — it exists or it does not — so
    the only write after creation is the 24-hour-locked delete (un-tick).
    """

    id = serializers.UUIDField(required=False)
    template_name = serializers.CharField(source="template.name", read_only=True)
    suggested_time = serializers.CharField(
        source="template.suggested_time", read_only=True
    )
    is_editable = serializers.BooleanField(read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = TaskCompletion
        fields = [
            "id",
            "template",
            "template_name",
            "suggested_time",
            "completion_date",
            "recorded_by",
            "recorded_by_name",
            "recorded_at",
            "created_at",
            "is_editable",
        ]
        read_only_fields = ["recorded_by", "created_at"]
        # `template` is a writable field, so ModelSerializer would auto-add a
        # UniqueTogetherValidator for unique(template, completion_date) and
        # reject a second device ticking the same item the same day with a
        # 400. That conflict is handled deliberately upstream — absorbed in
        # the view and the bulk-sync serializer — so the auto validator is
        # dropped. The DB constraint still guards integrity.
        validators = []

    def validate_template(self, value):
        farm = self.context["farm"]
        if value.farm_id != farm.pk:
            raise serializers.ValidationError("That routine item belongs to another farm.")
        if not value.is_active:
            raise serializers.ValidationError("That routine item is no longer in use.")
        return value

    def validate_completion_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Cannot record a future date.")
        return value

    def update(self, instance, validated_data):
        # The 24-hour lock has teeth here exactly as it does on a daily
        # mortality record or an inventory usage log.
        if not instance.is_editable:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "This task was ticked more than 24 hours ago and is "
                        "locked. Ask your manager."
                    )
                }
            )
        validated_data.pop("template", None)  # the item is fixed once ticked
        validated_data.pop("completion_date", None)  # so is the day
        return super().update(instance, validated_data)

    def create(self, validated_data):
        return TaskCompletion.objects.create(
            recorded_by=self.context["request"].user, **validated_data
        )


class TaskCompletionBulkSyncSerializer(serializers.Serializer):
    """
    Offline sync for routine ticks — a backlog in one round trip.

    Identity is the client UUID. The wrinkle is the (template,
    completion_date) unique constraint: two devices can generate different
    UUIDs for the same item on the same day. A completion carries no
    content, so the second one is ABSORBED — its client id is reported in
    `updated` so the device drops it, and the existing row stands. This is
    the one place the routine deliberately diverges from
    DailyRecordBulkSyncSerializer, which rejects a same-day conflict
    because a daily record's numbers would be lost.
    """

    records = TaskCompletionSerializer(many=True)

    def validate_records(self, value):
        if not value:
            raise serializers.ValidationError("No records supplied.")
        if len(value) > 60:
            raise serializers.ValidationError(
                "Sync at most 60 records per request."
            )
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        created, updated, failed = [], [], []

        for payload in self.validated_data["records"]:
            supplied_id = payload.get("id")
            try:
                with transaction.atomic():
                    existing = None
                    if supplied_id is not None:
                        existing = TaskCompletion.objects.filter(
                            pk=supplied_id
                        ).first()

                    if existing is None:
                        # A different device may already have ticked this
                        # item today under another id. Absorb it: the task
                        # is done, and there is no content to merge.
                        dupe = TaskCompletion.objects.filter(
                            template=payload["template"],
                            completion_date=payload["completion_date"],
                        ).first()
                        if dupe is not None:
                            updated.append(
                                str(supplied_id) if supplied_id else str(dupe.id)
                            )
                            continue

                        record = TaskCompletion.objects.create(
                            recorded_by=user, **payload
                        )
                        created.append(str(record.id))
                    elif existing.is_editable:
                        existing.recorded_by = user
                        existing.save()
                        updated.append(str(existing.id))
                    else:
                        failed.append(
                            {
                                "id": str(supplied_id),
                                "error": "Locked — older than 24 hours.",
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "id": str(supplied_id) if supplied_id else None,
                        "error": str(exc),
                    }
                )

        return {"created": created, "updated": updated, "failed": failed}