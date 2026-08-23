# production/urls.py
from django.urls import path

from .views import (
    BatchCorrectionListView,
    BatchDetailView,
    BatchListCreateView,
    BatchTerminateView,
    DailyRecordBulkSyncView,
    DailyRecordCorrectView,
    DailyRecordDetailView,
    DailyRecordListCreateView,
    FarmCorrectionListView,
    FeedDeliveryListCreateView,
    FeedStockView,
    HarvestCreateView,
    HarvestDetailView,
    HouseDetailView,
    HouseListCreateView,
    WeightSampleDetailView,
    WeightSampleListCreateView,
)

app_name = "production"

_farm = "farms/<int:farm_pk>"
_batch = f"{_farm}/batches/<uuid:batch_pk>"

urlpatterns = [
    path(f"{_farm}/houses/", HouseListCreateView.as_view(), name="house-list"),
    path(f"{_farm}/houses/<int:pk>/", HouseDetailView.as_view(), name="house-detail"),

    path(f"{_farm}/batches/", BatchListCreateView.as_view(), name="batch-list"),
    path(f"{_farm}/batches/<uuid:pk>/", BatchDetailView.as_view(), name="batch-detail"),
    path(f"{_farm}/batches/<uuid:pk>/terminate/", BatchTerminateView.as_view(), name="batch-terminate"),

    path(f"{_batch}/daily-records/", DailyRecordListCreateView.as_view(), name="daily-list"),
    path(f"{_batch}/daily-records/bulk-sync/", DailyRecordBulkSyncView.as_view(), name="daily-bulk-sync"),
    path(f"{_batch}/daily-records/<uuid:pk>/", DailyRecordDetailView.as_view(), name="daily-detail"),

    path(f"{_batch}/weights/", WeightSampleListCreateView.as_view(), name="weight-list"),
    path(f"{_batch}/weights/<uuid:pk>/", WeightSampleDetailView.as_view(), name="weight-detail"),

    path(f"{_batch}/harvest/", HarvestCreateView.as_view(), name="harvest-create"),
    path(f"{_batch}/harvest/detail/", HarvestDetailView.as_view(), name="harvest-detail"),

    path(f"{_farm}/feed-deliveries/", FeedDeliveryListCreateView.as_view(), name="feed-list"),
    path(f"{_farm}/feed-stock/", FeedStockView.as_view(), name="feed-stock"),
    
    path(f"{_batch}/daily-records/<uuid:pk>/correct/", DailyRecordCorrectView.as_view(), name="daily-correct"),
    path(f"{_batch}/corrections/", BatchCorrectionListView.as_view(), name="batch-corrections"),
    path(f"{_farm}/corrections/", FarmCorrectionListView.as_view(), name="farm-corrections"),
]