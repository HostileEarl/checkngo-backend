# analytics/urls.py
from django.urls import path

from .views import (
    AlertsView,
    BatchGrowthCurveView,
    BatchMortalityView,
    FarmDashboardView,
    FarmFCRView,
    FarmMortalityComparisonView,
    FarmProfitabilityView,
)

app_name = "analytics"

_base = "farms/<int:farm_pk>/analytics"

urlpatterns = [
    path(f"{_base}/dashboard/", FarmDashboardView.as_view(), name="dashboard"),
    path(f"{_base}/batches/<uuid:batch_pk>/mortality/", BatchMortalityView.as_view(), name="batch-mortality"),
    path(f"{_base}/batches/<uuid:batch_pk>/growth/", BatchGrowthCurveView.as_view(), name="batch-growth"),
    path(f"{_base}/mortality-comparison/", FarmMortalityComparisonView.as_view(), name="mortality-comparison"),
    path(f"{_base}/fcr/", FarmFCRView.as_view(), name="fcr"),
    path(f"{_base}/profitability/", FarmProfitabilityView.as_view(), name="profitability"),

    path("farms/<int:farm_pk>/alerts/", AlertsView.as_view(), name="alerts"),
]