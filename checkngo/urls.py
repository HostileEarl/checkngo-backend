# checkngo/urls.py
from decouple import config
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

# The admin lives at an unguessable path in every real environment. Read it
# from the environment so the value differs between local and deployment
# without a code change and never lands in the repository. The default is
# only for a bare checkout; production MUST override it (see docs).
ADMIN_URL = config("ADMIN_URL", default="admin/")

admin.site.site_header = "CheckN Go Administration"
admin.site.site_title = "CheckN Go"
admin.site.index_title = (
    "Platform administration — manage users and farms. This is the operator "
    "interface, separate from the farm-facing application; there is no in-app "
    "admin role."
)

urlpatterns = [
    path(ADMIN_URL, admin.site.urls),

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