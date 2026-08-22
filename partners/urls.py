# partners/urls.py
from django.urls import path

from .views import (
    FarmPartnerListCreateView,
    FarmPartnerUnlinkView,
    MyPartnerFarmsView,
)

app_name = "partners"

urlpatterns = [
    path("farms/<int:farm_pk>/partners/", FarmPartnerListCreateView.as_view(), name="farm-partners"),
    path("farms/<int:farm_pk>/partners/<int:pk>/unlink/", FarmPartnerUnlinkView.as_view(), name="partner-unlink"),
    path("partners/my-farms/", MyPartnerFarmsView.as_view(), name="my-partner-farms"),
]