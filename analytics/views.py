# analytics/views.py
import csv
import io

from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from farms.models import FarmMembership
from farms.permissions import FarmScopedPermission
from production.models import Batch

from . import reports
from .alert_history import sync_alert_logs
from .alerts import compute_alerts
from .models import AlertLog
from .serializers import AlertLogSerializer
from .xlsx_reports import build_workbook, xlsx_filename
from .services import (
    farm_dashboard,
    farm_mortality_comparison,
    fcr_by_batch,
    growth_curve,
    mortality_timeseries,
    profitability_by_batch,
)
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema

_REPORT_DATE_RANGE_PARAMS = [
    OpenApiParameter(
        "from", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=False,
        description="Inclusive start date (YYYY-MM-DD).",
    ),
    OpenApiParameter(
        "to", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=False,
        description="Inclusive end date (YYYY-MM-DD).",
    ),
]
_CSV_RESPONSE = {(200, "text/csv"): OpenApiTypes.BINARY}
_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_XLSX_RESPONSE = {(200, _XLSX_CONTENT_TYPE): OpenApiTypes.BINARY}


class AnalyticsPermission(FarmScopedPermission):
    """Any active member may read analytics. Archived farms stay readable by the owner."""

    allowed_roles = set()


class OwnerManagerAnalyticsPermission(FarmScopedPermission):
    """Money figures — revenue, margins, supplier costs — are not worker data."""

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Financial analytics are restricted to owners and managers."


class _BatchMixin:
    """Confirms the batch belongs to the farm the permission layer authorized."""

    def get_batch(self, request, batch_pk):
        return get_object_or_404(
            Batch.objects.select_related("house"),
            pk=batch_pk,
            house__farm=request.farm,
        )

@extend_schema(
    tags=["Analytics"],
    summary="Farm dashboard rollup",
    description=(
        "Single call for the landing screen — active batches, bird counts, "
        "feed balance, and lifetime FCR. Avoids four round trips on mobile."
    ),
)

class FarmDashboardView(APIView):
    """GET /api/farms/<farm_pk>/analytics/dashboard/"""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk):
        return Response(farm_dashboard(request.farm))

@extend_schema(
    tags=["Analytics"],
    summary="CHART 1 — mortality over time",
    description=(
        "Daily deaths for one batch, split by cause, with a running "
        "cumulative rate.\n\n"
        "**Gaps are real.** A day with no record is absent from `points` — "
        "it is not filled with zero, because 'nobody recorded' is a "
        "different claim from 'no birds died'. Render the gap.\n\n"
        "**`was_corrected`** marks days a manager amended after the 24-hour "
        "lock. Flag these visually."
    ),
    examples=[
        OpenApiExample(
            "Mid-cycle batch",
            value={
                "batch_code": "DEMO-2026-01",
                "initial_bird_count": 3400,
                "points": [
                    {
                        "date": "2026-01-23",
                        "age_days": 22,
                        "disease": 12,
                        "heat": 89,
                        "culled": 6,
                        "unknown": 0,
                        "daily_total": 107,
                        "cumulative_total": 198,
                        "cumulative_rate_pct": "5.82",
                        "birds_alive": 3202,
                        "feed_kg": "365.03",
                        "was_corrected": False,
                    }
                ],
                "summary": {
                    "total_mortality": 198,
                    "mortality_rate_pct": "5.82",
                    "days_recorded": 22,
                    "days_corrected": 0,
                    "by_cause": {"disease": 71, "heat": 98, "culled": 24, "unknown": 5},
                },
            },
            response_only=True,
        )
    ],
)
class BatchMortalityView(_BatchMixin, APIView):
    """GET /api/farms/<farm_pk>/analytics/batches/<batch_pk>/mortality/ — CHART 1"""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk, batch_pk):
        batch = self.get_batch(request, batch_pk)
        return Response(mortality_timeseries(batch))

@extend_schema(
    tags=["Analytics"],
    summary="Mortality rates across batches",
    parameters=[
        OpenApiParameter(
            "status", str, description="Filter: ACTIVE, HARVESTED, or TERMINATED."
        )
    ],
)
class FarmMortalityComparisonView(APIView):
    """GET /api/farms/<farm_pk>/analytics/mortality-comparison/?status=ACTIVE"""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk):
        status_filter = request.query_params.get("status")
        rows = farm_mortality_comparison(
            request.farm, status=status_filter.upper() if status_filter else None
        )
        return Response({"rows": rows})

@extend_schema(
    tags=["Analytics"],
    summary="CHART 2 — feed conversion ratio by batch",
    description=(
        "FCR = feed consumed (kg) / live weight produced (kg). Lower is better; "
        "1.5–1.8 is typical for well-run broilers.\n\n"
        "**Harvested batches only.** Active batches have no weight denominator "
        "and terminated batches never reached a production outcome. The "
        "`excluded` block reports how many were left out and why — surface "
        "this so the chart never silently under-reports."
    ),
)
class FarmFCRView(APIView):
    """GET /api/farms/<farm_pk>/analytics/fcr/ — CHART 2"""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk):
        return Response(fcr_by_batch(request.farm))

@extend_schema(tags=["Analytics"], summary="Growth curve — weight samples over time")
class BatchGrowthCurveView(_BatchMixin, APIView):
    """GET /api/farms/<farm_pk>/analytics/batches/<batch_pk>/growth/"""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk, batch_pk):
        batch = self.get_batch(request, batch_pk)
        return Response(
            {
                "batch_code": batch.batch_code,
                "start_date": batch.start_date.isoformat(),
                "points": growth_curve(batch),
            }
        )

@extend_schema(
    tags=["Analytics"],
    summary="CHART 3 — feed margin by batch (owner/manager only)",
    description=(
        "Revenue against apportioned feed cost.\n\n"
        "**This is feed margin, not net profit.** Feed cost is allocated using "
        "the farm's average cost per kg — it is not a traced expense. Chicks, "
        "labour, utilities, medication, and depreciation are excluded. The "
        "`methodology` block in the response states this; display it.\n\n"
        "**`null` is not zero.** A batch with no recorded sale price returns "
        "`revenue: null` and `feed_margin: null`. Render as 'not recorded', "
        "never as ₱0."
    ),
)
class FarmProfitabilityView(APIView):
    """GET /api/farms/<farm_pk>/analytics/profitability/ — CHART 3, restricted"""

    permission_classes = [OwnerManagerAnalyticsPermission]

    def get(self, request, farm_pk):
        return Response(profitability_by_batch(request.farm))


@extend_schema(
    tags=["Analytics"],
    summary="Computed alerts for the notification bell",
    description=(
        "Standing conditions derived from live data — high mortality, an "
        "unbalanced feed ledger, a day not yet recorded, an approaching "
        "harvest, an expiring invitation, a recent correction.\n\n"
        "Computed on every call. Each alert carries a stable `id` so the "
        "client can remember dismissals, and an `audience` — the list is "
        "already filtered to the caller's role before it is returned. "
        "`link` omits the role prefix; the client prepends it.\n\n"
        "Each call also records the farm's alerts in its history — see "
        "`alerts/history/`."
    ),
)
class AlertsView(APIView):
    """GET /api/farms/<farm_pk>/alerts/ — any active member; audience narrows further."""

    permission_classes = [AnalyticsPermission]

    def get(self, request, farm_pk):
        # History is synced from the farm-wide set, never the caller's view
        # of it — see alert_history.py.
        farm_alerts = compute_alerts(request.farm)
        sync_alert_logs(request.farm, farm_alerts)

        # Only a worker's list can differ (house-scoped "today" alerts); for
        # owners and managers the farm-wide list is the same, so reuse it.
        alerts = farm_alerts
        if request.membership.role == FarmMembership.Role.WORKER:
            alerts = compute_alerts(request.farm, request.membership)

        role = request.membership.role
        visible = [alert for alert in alerts if role in alert["audience"]]

        counts = {"danger": 0, "warning": 0, "info": 0}
        for alert in visible:
            counts[alert["severity"]] += 1

        return Response({"alerts": visible, "counts": counts})


class AlertHistoryPagination(PageNumberPagination):
    page_size = 20


@extend_schema(
    tags=["Analytics"],
    summary="Alert history for the notification bell",
    description=(
        "Every alert the farm has had, newest first (`first_seen_at`, then "
        "`id`), 20 per page via `?page=N`. `resolved_at` is null while the "
        "condition still stands. Filtered to the caller's role exactly as "
        "`alerts/` is. A condition that clears and comes back is a new row "
        "with the same `alert_key`.\n\n"
        "Resolved entries are kept for 90 days. A page past the end is a 404."
    ),
)
class AlertHistoryView(generics.ListAPIView):
    """GET /api/farms/<farm_pk>/alerts/history/ — same access as alerts/."""

    serializer_class = AlertLogSerializer
    permission_classes = [AnalyticsPermission]
    pagination_class = AlertHistoryPagination

    def get_queryset(self):
        return AlertLog.objects.filter(
            farm=self.request.farm,
            audience__contains=[self.request.membership.role],
        ).order_by("-first_seen_at", "-id")


# ─────────────────────────────────────────────────────────────
# CSV report exports — owner and manager only
# ─────────────────────────────────────────────────────────────
#
# A report is an aggregate export, which is not a worker's function, so
# every one of these is behind OwnerManagerAnalyticsPermission — the
# non-financial ones included. The rows come from analytics.reports; the
# views here only parse the query, stream the response, and name the file.


class _Echo:
    """A file-like object that returns what it is handed — csv.writer writes
    into this, and each written row becomes one chunk of the stream."""

    def write(self, value):
        return value


def _csv_stream(rows, filename):
    """
    Wrap a row generator in a StreamingHttpResponse.

    The generator is consumed lazily by Django as it fills the socket, so
    the whole file is never held in memory — the point of the exercise on a
    server with a few thousand daily records and not much RAM.
    """
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in rows),
        content_type="text/csv",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _date_range(request):
    """
    Parse ?from= and ?to=, both optional and both inclusive. A malformed
    date, or `from` later than `to`, is a 400 — DRF's ValidationError
    renders it for us.
    """
    raw_from = request.query_params.get("from")
    raw_to = request.query_params.get("to")

    date_from = parse_date(raw_from) if raw_from else None
    date_to = parse_date(raw_to) if raw_to else None

    if raw_from and date_from is None:
        raise ValidationError({"from": "Expected a date as YYYY-MM-DD."})
    if raw_to and date_to is None:
        raise ValidationError({"to": "Expected a date as YYYY-MM-DD."})
    if date_from and date_to and date_from > date_to:
        raise ValidationError({"detail": "`from` must not be after `to`."})

    return date_from, date_to


def _report_filename(farm, slug, date_from=None, date_to=None):
    """e.g. santos-broiler-farm-daily-records-2026-01-01-to-2026-03-31.csv"""
    base = f"{slugify(farm.name)}-{slug}"
    if date_from and date_to:
        return f"{base}-{date_from.isoformat()}-to-{date_to.isoformat()}.csv"
    if date_from:
        return f"{base}-from-{date_from.isoformat()}.csv"
    if date_to:
        return f"{base}-through-{date_to.isoformat()}.csv"
    return f"{base}.csv"


def _xlsx_response(report_key, request, farm, date_from=None, date_to=None, extra=None):
    """
    Build the workbook for one report and wrap it in a plain
    HttpResponse — unlike the CSV export, a workbook is not something
    openpyxl can stream cell-by-cell, so it is built in memory and
    returned whole. Reports are a farm's own aggregate data (thousands of
    rows at most, not an unbounded feed), so this does not carry the same
    memory risk the CSV streaming was written to avoid.
    """
    wb = build_workbook(report_key, farm, date_from, date_to, extra)
    buffer = io.BytesIO()
    wb.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type=_XLSX_CONTENT_TYPE)
    filename = xlsx_filename(report_key, date_from, date_to)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class _ReportView(APIView):
    permission_classes = [OwnerManagerAnalyticsPermission]


@extend_schema(
    tags=["Reports"],
    summary="Daily records — CSV export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "batch", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=False,
            description="Restrict to one batch.",
        ),
    ],
    responses=_CSV_RESPONSE,
)
class DailyRecordsReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/daily-records.csv?from=&to=&batch="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        rows = reports.daily_records_rows(
            request.farm,
            date_from,
            date_to,
            request.query_params.get("batch"),
        )
        return _csv_stream(
            rows,
            _report_filename(request.farm, "daily-records", date_from, date_to),
        )


@extend_schema(
    tags=["Reports"],
    summary="Mortality summary by batch — CSV export",
    parameters=_REPORT_DATE_RANGE_PARAMS,
    responses=_CSV_RESPONSE,
)
class MortalitySummaryReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/mortality-summary.csv?from=&to="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        rows = reports.mortality_summary_rows(request.farm, date_from, date_to)
        return _csv_stream(
            rows,
            _report_filename(
                request.farm, "mortality-summary", date_from, date_to
            ),
        )


@extend_schema(
    tags=["Reports"],
    summary="Feed conversion ratio — CSV export",
    responses=_CSV_RESPONSE,
)
class FCRReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/fcr.csv"""

    def get(self, request, farm_pk):
        rows = reports.fcr_rows(request.farm)
        return _csv_stream(rows, _report_filename(request.farm, "fcr"))


@extend_schema(
    tags=["Reports"],
    summary="Feed margin by batch — CSV export",
    responses=_CSV_RESPONSE,
)
class FeedMarginReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/feed-margin.csv"""

    def get(self, request, farm_pk):
        rows = reports.feed_margin_rows(request.farm)
        return _csv_stream(rows, _report_filename(request.farm, "feed-margin"))


@extend_schema(
    tags=["Reports"],
    summary="Inventory usage — CSV export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "item", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False,
            description="Restrict to one inventory item (its id).",
        ),
    ],
    responses=_CSV_RESPONSE,
)
class InventoryUsageReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/inventory-usage.csv?from=&to=&item="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        rows = reports.inventory_usage_rows(
            request.farm,
            date_from,
            date_to,
            request.query_params.get("item"),
        )
        return _csv_stream(
            rows,
            _report_filename(
                request.farm, "inventory-usage", date_from, date_to
            ),
        )


@extend_schema(
    tags=["Reports"],
    summary="Routine completion — CSV export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "house", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False,
            description="Restrict to one house (its id).",
        ),
    ],
    responses=_CSV_RESPONSE,
)
class RoutineCompletionReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/routine-completion.csv?from=&to=&house="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        rows = reports.routine_completion_rows(
            request.farm,
            date_from,
            date_to,
            request.query_params.get("house"),
        )
        return _csv_stream(
            rows,
            _report_filename(
                request.farm, "routine-completion", date_from, date_to
            ),
        )


@extend_schema(
    tags=["Reports"],
    summary="Sales history — CSV export",
    parameters=_REPORT_DATE_RANGE_PARAMS,
    responses=_CSV_RESPONSE,
)
class SalesHistoryReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/sales-history.csv?from=&to="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        rows = reports.sales_history_rows(request.farm, date_from, date_to)
        return _csv_stream(
            rows,
            _report_filename(request.farm, "sales-history", date_from, date_to),
        )


# ─────────────────────────────────────────────────────────────
# XLSX report exports — same data, same permission, same farm scoping.
# ─────────────────────────────────────────────────────────────
#
# Each view below pulls its rows from exactly the same analytics.reports
# generator the CSV sibling above uses (via build_workbook), parses the
# same query params, and differs only in how the response is built and
# named. No report's rows are computed twice.


@extend_schema(
    tags=["Reports"],
    summary="Daily records — XLSX export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "batch", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=False,
            description="Restrict to one batch.",
        ),
    ],
    responses=_XLSX_RESPONSE,
)
class DailyRecordsXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/daily-records.xlsx?from=&to=&batch="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        return _xlsx_response(
            "daily-records",
            request,
            request.farm,
            date_from,
            date_to,
            request.query_params.get("batch"),
        )


@extend_schema(
    tags=["Reports"],
    summary="Mortality summary by batch — XLSX export",
    parameters=_REPORT_DATE_RANGE_PARAMS,
    responses=_XLSX_RESPONSE,
)
class MortalitySummaryXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/mortality-summary.xlsx?from=&to="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        return _xlsx_response(
            "mortality-summary", request, request.farm, date_from, date_to
        )


@extend_schema(
    tags=["Reports"],
    summary="Feed conversion ratio — XLSX export",
    responses=_XLSX_RESPONSE,
)
class FCRXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/fcr.xlsx"""

    def get(self, request, farm_pk):
        return _xlsx_response("fcr", request, request.farm)


@extend_schema(
    tags=["Reports"],
    summary="Feed margin by batch — XLSX export",
    responses=_XLSX_RESPONSE,
)
class FeedMarginXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/feed-margin.xlsx"""

    def get(self, request, farm_pk):
        return _xlsx_response("feed-margin", request, request.farm)


@extend_schema(
    tags=["Reports"],
    summary="Inventory usage — XLSX export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "item", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False,
            description="Restrict to one inventory item (its id).",
        ),
    ],
    responses=_XLSX_RESPONSE,
)
class InventoryUsageXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/inventory-usage.xlsx?from=&to=&item="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        return _xlsx_response(
            "inventory-usage",
            request,
            request.farm,
            date_from,
            date_to,
            request.query_params.get("item"),
        )


@extend_schema(
    tags=["Reports"],
    summary="Routine completion — XLSX export",
    parameters=_REPORT_DATE_RANGE_PARAMS + [
        OpenApiParameter(
            "house", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False,
            description="Restrict to one house (its id).",
        ),
    ],
    responses=_XLSX_RESPONSE,
)
class RoutineCompletionXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/routine-completion.xlsx?from=&to=&house="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        return _xlsx_response(
            "routine-completion",
            request,
            request.farm,
            date_from,
            date_to,
            request.query_params.get("house"),
        )


@extend_schema(
    tags=["Reports"],
    summary="Sales history — XLSX export",
    parameters=_REPORT_DATE_RANGE_PARAMS,
    responses=_XLSX_RESPONSE,
)
class SalesHistoryXLSXReportView(_ReportView):
    """GET /api/farms/<farm_pk>/reports/sales-history.xlsx?from=&to="""

    def get(self, request, farm_pk):
        date_from, date_to = _date_range(request)
        return _xlsx_response(
            "sales-history", request, request.farm, date_from, date_to
        )