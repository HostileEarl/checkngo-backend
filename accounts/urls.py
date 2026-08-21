# accounts/urls.py
from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from .views import (
    CredentialChangeView,
    InvitationAcceptView,
    LogoutView,
    MeView,
    PhonePinLoginView,
)

app_name = "accounts"

urlpatterns = [
    path("auth/login/", PhonePinLoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/verify/", TokenVerifyView.as_view(), name="token-verify"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/credential/change/", CredentialChangeView.as_view(), name="credential-change"),
    path("invitations/accept/", InvitationAcceptView.as_view(), name="invitation-accept"),
]