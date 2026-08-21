# farms/urls.py
from django.urls import path

from accounts.views import FarmInvitationListCreateView

from .views import (
    FarmArchiveView,
    FarmDetailView,
    FarmListCreateView,
    FarmMemberListView,
    FarmMemberRevokeView,
    FarmOwnershipHistoryView,
    FarmReactivateView,
    OwnershipTransferView,
)

app_name = "farms"

urlpatterns = [
    path("farms/", FarmListCreateView.as_view(), name="farm-list"),
    path("farms/<int:pk>/", FarmDetailView.as_view(), name="farm-detail"),
    path("farms/<int:farm_pk>/invitations/", FarmInvitationListCreateView.as_view(), name="farm-invitations"),
    path("farms/<int:farm_pk>/members/", FarmMemberListView.as_view(), name="farm-members"),
    path("farms/<int:farm_pk>/members/<int:pk>/revoke/", FarmMemberRevokeView.as_view(), name="member-revoke"),
    path("farms/<int:farm_pk>/ownership-history/", FarmOwnershipHistoryView.as_view(), name="ownership-history"),
    path("farms/<int:farm_pk>/archive/", FarmArchiveView.as_view(), name="farm-archive"),
    path("farms/<int:farm_pk>/reactivate/", FarmReactivateView.as_view(), name="farm-reactivate"),
    path("farms/<int:farm_pk>/transfer-ownership/", OwnershipTransferView.as_view(), name="ownership-transfer"),
]