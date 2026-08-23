# checkngo/urls.py
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

admin.site.site_header = "CheckN Go Administration"
admin.site.site_title = "CheckN Go"
admin.site.index_title = "Farm operations backend"

urlpatterns = [
    path("admin/", admin.site.urls),

    path("api/", include("accounts.urls")),
    path("api/", include("farms.urls")),
    path("api/", include("partners.urls")),
    path("api/", include("production.urls")),
    path("api/", include("analytics.urls")),

    # Documentation
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]