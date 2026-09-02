# production/views.py
import uuid

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .mixins import BatchScopedMixin
from .models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    WeightSample,
)
from .permissions import (
    CanManageBatches,
    CanManageFeed,
    CanRecordDaily,
    CanViewProduction,
)
from .serializers import (
    BatchCreateSerializer,
    BatchSerializer,
    DailyRecordBulkSyncSerializer,
    DailyRecordSerializer,
    FeedDeliverySerializer,
    HarvestSerializer,
    HouseSerializer,
    WeightSampleSerializer,
)

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
    WeightSample,
)
from .permissions import (
    CanCorrectLockedRecord,
    CanManageBatches,
    CanManageFeed,
    CanManageInventory,
    CanRecordDaily,
    CanRecordInventoryUsage,
    CanViewProduction,
)
from .serializers import (
    BatchCreateSerializer,
    BatchSerializer,
    DailyRecordBulkSyncSerializer,
    DailyRecordCorrectionSerializer,
    DailyRecordSerializer,
    FeedDeliverySerializer,
    HarvestSerializer,
    HouseSerializer,
    InventoryItemSerializer,
    InventoryStockInSerializer,
    InventoryUsageBulkSyncSerializer,
    InventoryUsageLogSerializer,
    RecordCorrectionSerializer,
    WeightSampleSerializer,
)
from drf_spectacular.utils import OpenApiExample, extend_schema

# ─────────────────────────────────────────────────────────────
# Houses
# ─────────────────────────────────────────────────────────────


class HouseListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/houses/
    POST /api/farms/<farm_pk>/houses/  — owner/manager only
    """

    serializer_class = HouseSerializer
    permission_classes = [CanManageBatches]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        return (
            House.objects.filter(farm=self.request.farm)
            .prefetch_related("batches")
        )

    def perform_create(self, serializer):
        serializer.save(farm=self.request.farm)


class HouseDetailView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/farms/<farm_pk>/houses/<pk>/"""

    serializer_class = HouseSerializer
    permission_classes = [CanManageBatches]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        return House.objects.filter(farm=self.request.farm)


# ─────────────────────────────────────────────────────────────
# Batches
# ─────────────────────────────────────────────────────────────


class BatchListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/batches/?status=ACTIVE&house=<id>
    POST /api/farms/<farm_pk>/batches/  — placement, owner/manager only
    """

    permission_classes = [CanManageBatches]

    def get_serializer_class(self):
        return (
            BatchCreateSerializer
            if self.request.method == "POST"
            else BatchSerializer
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        qs = (
            Batch.objects.filter(house__farm=self.request.farm)
            .select_related("house")
            .prefetch_related("weight_samples")
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        house_filter = self.request.query_params.get("house")
        if house_filter:
            qs = qs.filter(house_id=house_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = serializer.save()
        # Echo back the full read representation, not the thin create one.
        return Response(
            BatchSerializer(batch).data, status=status.HTTP_201_CREATED
        )


class BatchDetailView(generics.RetrieveAPIView):
    """GET /api/farms/<farm_pk>/batches/<pk>/ — readable by any member."""

    serializer_class = BatchSerializer
    permission_classes = [CanViewProduction]

    def get_queryset(self):
        return Batch.objects.filter(
            house__farm=self.request.farm
        ).select_related("house").prefetch_related("weight_samples")


class BatchTerminateView(APIView):
    """
    POST /api/farms/<farm_pk>/batches/<pk>/terminate/

    Closes a batch without a harvest — disease wipeout, or a data-entry
    batch that should never have existed. Distinct from HARVESTED so the
    analytics can exclude it from FCR averages.
    """

    permission_classes = [CanManageBatches]

    def post(self, request, farm_pk, pk):
        batch = get_object_or_404(
            Batch, pk=pk, house__farm=request.farm, status=Batch.Status.ACTIVE
        )
        reason = request.data.get("reason", "").strip()
        if not reason:
            return Response(
                {"reason": "A reason is required to terminate a batch."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.status = Batch.Status.TERMINATED
        batch.save(update_fields=["status"])
        return Response(
            {
                "detail": f"Batch {batch.batch_code} terminated.",
                "reason": reason,
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────
# Daily records
# ─────────────────────────────────────────────────────────────


class DailyRecordListCreateView(BatchScopedMixin, generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/batches/<batch_pk>/daily-records/
    POST /api/farms/<farm_pk>/batches/<batch_pk>/daily-records/

    The worker's primary write path.
    """

    serializer_class = DailyRecordSerializer
    permission_classes = [CanRecordDaily]

    def get_queryset(self):
        return DailyRecord.objects.filter(batch=self.batch).select_related(
            "recorded_by"
        )

    def create(self, request, *args, **kwargs):
        """
        A client-generated id existing already means this is a retry, not a
        new record — the whole point of naming it on the device before it
        ever reaches the server. Without this, a retried POST hit the
        primary key unique constraint directly and raised an unhandled
        IntegrityError (a raw 500) instead of updating in place the way
        the bulk-sync path already does.
        """
        supplied_id = request.data.get("id")
        existing = None
        if supplied_id:
            try:
                # A malformed id isn't a lookup miss, it's invalid input —
                # let the serializer's own UUIDField validation reject it
                # with a clean message instead of this query raising
                # Django's ValidationError, which DRF does not catch.
                uuid.UUID(str(supplied_id))
            except (ValueError, AttributeError, TypeError):
                pass
            else:
                existing = self.get_queryset().filter(pk=supplied_id).first()

        if existing is not None:
            # DailyRecordSerializer.update() already enforces the 24-hour
            # lock and refuses to move the record to a different date —
            # reusing it here means this retry path stays governed by
            # exactly the same rules as editing the record any other way.
            serializer = self.get_serializer(existing, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        # No existing record under this id (or none was supplied) — but a
        # DIFFERENT record may already occupy this date. "One entry per
        # batch per day" is a business rule, not just an idempotency
        # mechanism: reject that plainly instead of letting the unique
        # constraint raise the same unhandled IntegrityError.
        record_date = request.data.get("record_date")
        if record_date and self.get_queryset().filter(record_date=record_date).exists():
            return Response(
                {
                    "record_date": [
                        "A record already exists for this date."
                        if not supplied_id
                        else "A record already exists for this date under a different id."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return super().create(request, *args, **kwargs)


class DailyRecordDetailView(BatchScopedMixin, generics.RetrieveUpdateAPIView):
    """
    GET / PATCH /api/farms/<farm_pk>/batches/<batch_pk>/daily-records/<pk>/

    PATCH is refused by the serializer once the 24-hour window closes.
    No DELETE — a recorded day is not removable.
    """

    serializer_class = DailyRecordSerializer
    permission_classes = [CanRecordDaily]

    def get_queryset(self):
        return DailyRecord.objects.filter(batch=self.batch).select_related(
            "recorded_by"
        )

@extend_schema(
    tags=["Production"],
    summary="Offline bulk sync — daily records",
    request=DailyRecordBulkSyncSerializer,
    description=(
        "Upload a backlog of daily records in one request, for devices "
        "returning from a dead zone.\n\n"
        "**Idempotent.** Supply the client-generated UUID as `id`. Re-syncing "
        "a day that already exists overwrites it, provided it is still inside "
        "the 24-hour window.\n\n"
        "**Partial success is normal.** Each record is written in its own "
        "savepoint, so one bad row does not reject the rest. A `207 "
        "Multi-Status` means some records failed — read `failed[]` for which "
        "and why, and retry only those. `200` means all landed.\n\n"
        "**Limits.** Maximum 60 records per request. Duplicate dates within "
        "one payload are rejected outright with a 400."
    ),
    examples=[
        OpenApiExample(
            "Three days of backlog",
            value={
                "records": [
                    {
                        "id": "9f1c2e4a-1b3d-4f5a-8c9e-0a1b2c3d4e5f",
                        "record_date": "2026-08-19",
                        "mortality_disease": 4,
                        "mortality_heat": 0,
                        "mortality_culled": 2,
                        "mortality_unknown": 0,
                        "feed_kg": "312.50",
                        "recorded_at": "2026-08-19T06:15:00+08:00",
                    }
                ]
            },
            request_only=True,
        ),
        OpenApiExample(
            "207 — partial success",
            value={
                "created": ["9f1c2e4a-1b3d-4f5a-8c9e-0a1b2c3d4e5f"],
                "updated": [],
                "failed": [
                    {"record_date": "2026-08-15", "error": "Locked — older than 24 hours."}
                ],
                "batch_totals": {
                    "total_mortality": 198,
                    "total_feed_kg": "6240.75",
                    "current_bird_count": 3202,
                },
            },
            response_only=True,
        ),
    ],
)
class DailyRecordBulkSyncView(BatchScopedMixin, APIView):
    """
    POST /api/farms/<farm_pk>/batches/<batch_pk>/daily-records/bulk-sync/

    A week of offline backlog in one round trip. Returns per-record outcomes
    so the device knows exactly what to retry — a partial success is the
    normal case out here, not an error.
    """

    permission_classes = [CanRecordDaily]
    # The house-scoping check is done per record inside the serializer, so a
    # worker who lost a house assignment gets that day back in failed[]
    # rather than a 403 on the whole backlog.
    enforce_house_write_scope = False

    def post(self, request, farm_pk, batch_pk):
        serializer = DailyRecordBulkSyncSerializer(
            data=request.data,
            context={"request": request, "batch": self.batch, "farm": request.farm},
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        # 207 Multi-Status: some rows may have landed and others not.
        http_status = (
            status.HTTP_207_MULTI_STATUS
            if result["failed"]
            else status.HTTP_200_OK
        )
        return Response(result, status=http_status)


# ─────────────────────────────────────────────────────────────
# Weight samples
# ─────────────────────────────────────────────────────────────


class WeightSampleListCreateView(BatchScopedMixin, generics.ListCreateAPIView):
    """GET / POST /api/farms/<farm_pk>/batches/<batch_pk>/weights/"""

    serializer_class = WeightSampleSerializer
    permission_classes = [CanRecordDaily]

    def get_queryset(self):
        return WeightSample.objects.filter(batch=self.batch).select_related(
            "recorded_by"
        )


class WeightSampleDetailView(BatchScopedMixin, generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/farms/<farm_pk>/batches/<batch_pk>/weights/<pk>/"""

    serializer_class = WeightSampleSerializer
    permission_classes = [CanRecordDaily]

    def get_queryset(self):
        return WeightSample.objects.filter(batch=self.batch)


# ─────────────────────────────────────────────────────────────
# Harvest
# ─────────────────────────────────────────────────────────────


class HarvestCreateView(BatchScopedMixin, generics.CreateAPIView):
    """
    POST /api/farms/<farm_pk>/batches/<batch_pk>/harvest/

    Closes the batch, frees the house, and unlocks FCR for this cohort.
    """

    serializer_class = HarvestSerializer
    permission_classes = [CanManageBatches]

    def create(self, request, *args, **kwargs):
        if hasattr(self.batch, "harvest"):
            return Response(
                {"detail": "This batch has already been harvested."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().create(request, *args, **kwargs)
    
class HarvestDetailView(BatchScopedMixin, generics.RetrieveAPIView):
    """GET /api/farms/<farm_pk>/batches/<batch_pk>/harvest/detail/"""

    serializer_class = HarvestSerializer
    permission_classes = [CanViewProduction]

    def get_object(self):
        return get_object_or_404(Harvest, batch=self.batch)


class BatchTerminateView(APIView):
    """
    POST /api/farms/<farm_pk>/batches/<pk>/terminate/

    Closes a batch without a harvest — disease wipeout, or a batch that
    should never have existed. Distinct from HARVESTED so the analytics can
    exclude it from FCR averages, and documented so that exclusion is
    always accountable.
    """

    permission_classes = [CanManageBatches]

    def post(self, request, farm_pk, pk):
        batch = get_object_or_404(
            Batch, pk=pk, house__farm=request.farm, status=Batch.Status.ACTIVE
        )
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response(
                {"reason": "A reason is required to terminate a batch."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.terminate(reason, user=request.user)
        return Response(BatchSerializer(batch).data, status=status.HTTP_200_OK)

# ─────────────────────────────────────────────────────────────
# Feed
# ─────────────────────────────────────────────────────────────


class FeedDeliveryListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/feed-deliveries/
    POST /api/farms/<farm_pk>/feed-deliveries/  — owner/manager only
    """

    serializer_class = FeedDeliverySerializer
    permission_classes = [CanManageFeed]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        qs = FeedDelivery.objects.filter(farm=self.request.farm).select_related(
            "supplier_link__partner", "recorded_by"
        )
        feed_type = self.request.query_params.get("type")
        if feed_type:
            qs = qs.filter(feed_type=feed_type.upper())
        return qs


class FeedStockView(APIView):
    """
    GET /api/farms/<farm_pk>/feed-stock/

    Farm-level balance: everything delivered minus everything consumed
    across all batches. Per the earlier ruling we do not trace individual
    sacks to individual houses, so this is a running total, not a ledger.
    """

    permission_classes = [CanViewProduction]

    def get(self, request, farm_pk):
        from decimal import Decimal

        from django.db.models import Sum

        delivered = FeedDelivery.objects.filter(farm=request.farm).aggregate(
            kg=Sum("quantity_kg"), cost=Sum("total_cost")
        )
        consumed = DailyRecord.objects.filter(
            batch__house__farm=request.farm
        ).aggregate(kg=Sum("feed_kg"))

        delivered_kg = delivered["kg"] or Decimal("0")
        consumed_kg = consumed["kg"] or Decimal("0")

        return Response(
            {
                "delivered_kg": str(delivered_kg),
                "consumed_kg": str(consumed_kg),
                "balance_kg": str(delivered_kg - consumed_kg),
                "total_feed_cost": str(delivered["cost"] or Decimal("0")),
            }
        )
class DailyRecordCorrectView(BatchScopedMixin, APIView):
    """
    POST /api/farms/<farm_pk>/batches/<batch_pk>/daily-records/<pk>/correct/

    The legitimate front door around the 24-hour lock. Every use writes a
    permanent, immutable log entry naming who, what, and why.
    """

    permission_classes = [CanCorrectLockedRecord]

    def post(self, request, farm_pk, batch_pk, pk):
        record = get_object_or_404(DailyRecord, pk=pk, batch=self.batch)

        serializer = DailyRecordCorrectionSerializer(
            data=request.data,
            context={"request": request, "record": record, "batch": self.batch},
        )
        serializer.is_valid(raise_exception=True)
        correction = serializer.save()

        record.refresh_from_db()
        self.batch.refresh_from_db()

        return Response(
            {
                "detail": "Record corrected. The change has been logged.",
                "correction": RecordCorrectionSerializer(correction).data,
                "record": DailyRecordSerializer(record).data,
                "batch_totals": {
                    "total_mortality": self.batch.total_mortality,
                    "total_feed_kg": str(self.batch.total_feed_kg),
                    "current_bird_count": self.batch.current_bird_count,
                },
            },
            status=status.HTTP_200_OK,
        )


class BatchCorrectionListView(BatchScopedMixin, generics.ListAPIView):
    """
    GET /api/farms/<farm_pk>/batches/<batch_pk>/corrections/

    Readable by every member — a worker should know when their entry was
    overwritten, and by whom.
    """

    serializer_class = RecordCorrectionSerializer
    permission_classes = [CanViewProduction]

    def get_queryset(self):
        return RecordCorrection.objects.filter(batch=self.batch).select_related(
            "batch", "content_type"
        )


class FarmCorrectionListView(generics.ListAPIView):
    """GET /api/farms/<farm_pk>/corrections/ — farm-wide audit view."""

    serializer_class = RecordCorrectionSerializer
    permission_classes = [CanViewProduction]

    def get_queryset(self):
        return RecordCorrection.objects.filter(
            batch__house__farm=self.request.farm
        ).select_related("batch", "content_type")


# ─────────────────────────────────────────────────────────────
# Inventory
# ─────────────────────────────────────────────────────────────


class InventoryItemListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/inventory/items/   — any active member
    POST /api/farms/<farm_pk>/inventory/items/   — owner/manager only

    CanManageInventory leaves reads open (empty read_roles), so a worker
    can still fetch the list to pick an item to log usage against.
    """

    serializer_class = InventoryItemSerializer
    permission_classes = [CanManageInventory]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        qs = InventoryItem.objects.filter(farm=self.request.farm).with_levels()
        if self.request.query_params.get("active") == "1":
            qs = qs.filter(is_active=True)
        return qs


class InventoryItemDetailView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/farms/<farm_pk>/inventory/items/<pk>/ — owner/manager to write."""

    serializer_class = InventoryItemSerializer
    permission_classes = [CanManageInventory]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        return InventoryItem.objects.filter(farm=self.request.farm).with_levels()


class InventoryStockInListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/inventory/items/<item_pk>/stock-ins/
    POST /api/farms/<farm_pk>/inventory/items/<item_pk>/stock-ins/  — owner/manager

    Stock-in is the in-flow the balance is computed from. Without it the
    balance only ever falls, goes negative, and the low-stock alert sticks.
    """

    serializer_class = InventoryStockInSerializer
    permission_classes = [CanManageInventory]

    @property
    def item(self):
        if not hasattr(self, "_item"):
            self._item = get_object_or_404(
                InventoryItem, pk=self.kwargs["item_pk"], farm=self.request.farm
            )
        return self._item

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        context["item"] = self.item
        return context

    def get_queryset(self):
        return InventoryStockIn.objects.filter(item=self.item).select_related(
            "recorded_by"
        )


class InventoryUsageListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/inventory/usage/            — any active member
    POST /api/farms/<farm_pk>/inventory/usage/            — workers included

    The worker's write path. Idempotent: a POST whose client-generated `id`
    already exists is a retry, not a new event — it is routed through the
    serializer's update() (which enforces the 24-hour lock) and returns
    200, mirroring DailyRecordListCreateView. There is no same-date
    conflict branch — a usage log is an event, and two events on one day
    are legitimate.
    """

    serializer_class = InventoryUsageLogSerializer
    permission_classes = [CanRecordInventoryUsage]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        qs = InventoryUsageLog.objects.filter(
            item__farm=self.request.farm
        ).select_related("item", "recorded_by")
        item_id = self.request.query_params.get("item")
        if item_id:
            qs = qs.filter(item_id=item_id)
        if self.request.query_params.get("mine") == "1":
            qs = qs.filter(recorded_by=self.request.user)
        usage_date = self.request.query_params.get("date")
        if usage_date:
            qs = qs.filter(usage_date=usage_date)
        return qs

    def create(self, request, *args, **kwargs):
        supplied_id = request.data.get("id")
        existing = None
        if supplied_id:
            try:
                uuid.UUID(str(supplied_id))
            except (ValueError, AttributeError, TypeError):
                pass
            else:
                existing = self.get_queryset().filter(pk=supplied_id).first()

        if existing is not None:
            # Reusing the serializer's update() keeps this retry path
            # governed by the same 24-hour lock as any other edit.
            serializer = self.get_serializer(existing, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)


class InventoryUsageDetailView(generics.RetrieveUpdateAPIView):
    """
    GET / PATCH /api/farms/<farm_pk>/inventory/usage/<pk>/

    PATCH is refused by the serializer once the 24-hour window closes.
    No DELETE — a recorded event is not removable.
    """

    serializer_class = InventoryUsageLogSerializer
    permission_classes = [CanRecordInventoryUsage]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        return InventoryUsageLog.objects.filter(
            item__farm=self.request.farm
        ).select_related("item", "recorded_by")


@extend_schema(
    tags=["Production"],
    summary="Offline bulk sync — inventory usage logs",
    request=InventoryUsageBulkSyncSerializer,
    description=(
        "Upload a backlog of usage events in one request.\n\n"
        "**Idempotent.** Supply the client-generated UUID as `id`. "
        "Re-syncing an event that already exists overwrites it, provided it "
        "is still inside the 24-hour window.\n\n"
        "**An event, not a daily record.** The same item may appear more "
        "than once — each row is its own event, keyed only by `id`. There "
        "is no per-day uniqueness.\n\n"
        "**Partial success is normal.** `207 Multi-Status` means some rows "
        "failed — read `failed[]` (each carries the offending `id`) and "
        "retry only those. Maximum 60 records per request."
    ),
)
class InventoryUsageBulkSyncView(APIView):
    """POST /api/farms/<farm_pk>/inventory/usage/bulk-sync/"""

    permission_classes = [CanRecordInventoryUsage]

    def post(self, request, farm_pk):
        serializer = InventoryUsageBulkSyncSerializer(
            data=request.data,
            context={"request": request, "farm": request.farm},
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        http_status = (
            status.HTTP_207_MULTI_STATUS
            if result["failed"]
            else status.HTTP_200_OK
        )
        return Response(result, status=http_status)