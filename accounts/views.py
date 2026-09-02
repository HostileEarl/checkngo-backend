# accounts/views.py
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from farms.models import FarmMembership
from farms.permissions import CanInviteRole, IsFarmManagerOrOwner

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
    """
    GET /api/auth/me/ — current user plus farm scope.

    Memberships are included because the client needs to know which farms
    it may address before it can call any farm-scoped endpoint. Without
    them a restored session knows who it is but not where it works.
    """

    permission_classes = [IsAuthenticated]  # NOT gated — deliberate

    def get(self, request):
        from accounts.serializers import MembershipSummarySerializer

        memberships = (
            request.user.farm_memberships.filter(is_active=True)
            .select_related("farm")
            .prefetch_related("houses")
        )

        data = UserSerializer(request.user).data
        data["memberships"] = MembershipSummarySerializer(memberships, many=True).data
        return Response(data)   


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
            #
            # The token travels in the accept link (or a QR code of it)
            # while the PIN travels out-of-band — spoken, or by SMS. Neither
            # channel alone is enough to onboard, which is the point. This
            # is the only response that ever carries the token:
            # InvitationReadSerializer (list/detail) deliberately omits it.
            payload["pin"] = serializer.issued_pin
            payload["token"] = invitation.token
            payload["accept_url"] = (
                f"{settings.FRONTEND_URL}/accept-invite?token={invitation.token}"
            )
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


class FarmInvitationRevokeView(APIView):
    """
    POST /api/farms/<farm_pk>/invitations/<pk>/revoke/

    Undo a mistyped or misdirected invitation. The partial unique
    constraint on (phone_number, farm) WHERE status='PENDING' blocks
    re-inviting the correct number until the bad row leaves PENDING, so
    without this the fix is a seven-day wait for it to expire.

    Same authority as issuing one: a manager can revoke a worker
    invitation but not a manager-level one; the owner can revoke any.

    That distinction is enforced here against the invitation's stored
    membership_role, not against anything in the request body. The old
    approach reused CanInviteRole, which reads membership_role from the
    body — so a manager could revoke a MANAGER-level invitation by
    sending {"membership_role": "WORKER"}, since the check never looked
    at the invitation being targeted.
    """

    permission_classes = [IsFarmManagerOrOwner]

    def post(self, request, farm_pk, pk):
        invitation = get_object_or_404(Invitation, pk=pk, farm=request.farm)

        if (
            request.membership.role == FarmMembership.Role.MANAGER
            and invitation.membership_role != FarmMembership.Role.WORKER
        ):
            return Response(
                {"detail": "Only the farm owner can revoke a manager-level invitation."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if invitation.status != Invitation.Status.PENDING:
            return Response(
                {
                    "detail": (
                        f"This invitation is {invitation.get_status_display().lower()}, "
                        "not pending, so it cannot be revoked."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        invitation.revoke()
        return Response(
            InvitationReadSerializer(invitation).data,
            status=status.HTTP_200_OK,
        )


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