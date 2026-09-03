# analytics/urls.py
from django.urls import path

from .views import (
    AlertsView,
    BatchGrowthCurveView,
    BatchMortalityView,
    DailyRecordsReportView,
    FarmDashboardView,
    FarmFCRView,
    FarmMortalityComparisonView,
    FarmProfitabilityView,
    FCRReportView,
    FeedMarginReportView,
    InventoryUsageReportView,
    MortalitySummaryReportView,
    RoutineCompletionReportView,
)

app_name = "analytics"

_base = "farms/<int:farm_pk>/analytics"
_reports = "farms/<int:farm_pk>/reports"

urlpatterns = [
    path(f"{_base}/dashboard/", FarmDashboardView.as_view(), name="dashboard"),
    path(f"{_base}/batches/<uuid:batch_pk>/mortality/", BatchMortalityView.as_view(), name="batch-mortality"),
    path(f"{_base}/batches/<uuid:batch_pk>/growth/", BatchGrowthCurveView.as_view(), name="batch-growth"),
    path(f"{_base}/mortality-comparison/", FarmMortalityComparisonView.as_view(), name="mortality-comparison"),
    path(f"{_base}/fcr/", FarmFCRView.as_view(), name="fcr"),
    path(f"{_base}/profitability/", FarmProfitabilityView.as_view(), name="profitability"),

    path("farms/<int:farm_pk>/alerts/", AlertsView.as_view(), name="alerts"),

    # CSV report exports — owner and manager only.
    path(f"{_reports}/daily-records.csv", DailyRecordsReportView.as_view(), name="report-daily-records"),
    path(f"{_reports}/mortality-summary.csv", MortalitySummaryReportView.as_view(), name="report-mortality-summary"),
    path(f"{_reports}/fcr.csv", FCRReportView.as_view(), name="report-fcr"),
    path(f"{_reports}/feed-margin.csv", FeedMarginReportView.as_view(), name="report-feed-margin"),
    path(f"{_reports}/inventory-usage.csv", InventoryUsageReportView.as_view(), name="report-inventory-usage"),
    path(f"{_reports}/routine-completion.csv", RoutineCompletionReportView.as_view(), name="report-routine-completion"),
]
