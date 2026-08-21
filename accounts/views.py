# accounts/views.py
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from farms.permissions import CanInviteRole

from .models import Invitation
from .serializers import (
    CredentialChangeSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationReadSerializer,
    LogoutSerializer,
    PhonePinTokenSerializer,
    UserSerializer,
)
from .throttles import (
    CredentialChangeThrottle,
    InvitationAcceptThrottle,
    LoginIPThrottle,
    PhoneLoginThrottle,
)


class PhonePinLoginView(TokenObtainPairView):
    """
    POST /api/auth/login/

    Two throttle layers: per-phone stops brute-forcing one account,
    per-IP stops spraying one PIN across many accounts.
    """

    serializer_class = PhonePinTokenSerializer
    throttle_classes = [PhoneLoginThrottle, LoginIPThrottle]


class MeView(APIView):
    """GET /api/auth/me/ — reachable while gated, so the app can boot."""

    permission_classes = [IsAuthenticated]  # NOT gated — deliberate

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class CredentialChangeView(APIView):
    """
    POST /api/auth/credential/change/

    The only way through the rotation gate, so it must stay reachable by
    gated users. Throttled because it verifies the current PIN.
    """

    permission_classes = [IsAuthenticated]  # NOT gated — deliberate
    throttle_classes = [CredentialChangeThrottle]

    def post(self, request):
        serializer = CredentialChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "PIN updated.", "must_change_credential": False},
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """POST /api/auth/logout/ — revoke a single device's refresh token."""

    permission_classes = [IsAuthenticated]  # NOT gated — deliberate

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_205_RESET_CONTENT)


class FarmInvitationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/invitations/  — list this farm's invitations
    POST /api/farms/<farm_pk>/invitations/  — issue one
    """

    permission_classes = [CanInviteRole]

    def get_serializer_class(self):
        return (
            InvitationCreateSerializer
            if self.request.method == "POST"
            else InvitationReadSerializer
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        return Invitation.objects.filter(farm=self.request.farm).select_related(
            "invited_by", "farm"
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = serializer.save()

        payload = InvitationReadSerializer(invitation).data

        if serializer.issued_pin:
            # Shown once, never recoverable. There is no "resend PIN" path
            # by design — a lost PIN means revoke and re-invite.
            payload["pin"] = serializer.issued_pin
            payload["detail"] = (
                f"Invitation created. Give this PIN to {invitation.full_name}."
            )
        else:
            name = serializer.existing_user.full_name
            payload["existing_user"] = name
            payload["detail"] = (
                f"{name} already has an account and will use their existing PIN."
            )

        return Response(payload, status=status.HTTP_201_CREATED)


class InvitationAcceptView(APIView):
    """
    POST /api/invitations/accept/

    Public by necessity — the invitee has no account yet. The most exposed
    surface in the system, hence the tightest anonymous throttle.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [InvitationAcceptThrottle]

    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "detail": "Invitation accepted.",
                "must_change_credential": user.must_change_credential,
            },
            status=status.HTTP_200_OK,
        )