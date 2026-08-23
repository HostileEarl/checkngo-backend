# production/views.py
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
    RecordCorrection,
    WeightSample,
)
from .permissions import (
    CanCorrectLockedRecord,
    CanManageBatches,
    CanManageFeed,
    CanRecordDaily,
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