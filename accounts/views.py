# accounts/views.py
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from farms.models import FarmMembership
from farms.permissions import CanInviteRole, IsFarmManagerOrOwner

from .models import Invitation, User
from .serializers import (
    AccessRequestCreatedSerializer,
    AccessRequestSerializer,
    CredentialChangeSerializer,
    InvitationAcceptSerializer,
    InvitationCreateResponseSerializer,
    InvitationCreateSerializer,
    InvitationReadSerializer,
    LogoutSerializer,
    PhonePinTokenSerializer,
    UserSerializer,
    build_short_accept_url,
    existing_user_invite_message,
    notify_access_request,
    send_invitation_pin_sms,
)
from .throttles import (
    AccessRequestThrottle,
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


@extend_schema_view(
    post=extend_schema(responses=InvitationCreateResponseSerializer),
)
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
        qs = Invitation.objects.filter(farm=self.request.farm).select_related(
            "invited_by", "farm"
        )

        kind = self.request.query_params.get("kind")
        if kind == "buyer":
            qs = qs.filter(account_role=User.Role.CONSUMER)
        elif kind == "staff":
            qs = qs.exclude(account_role=User.Role.CONSUMER)

        # A manager never sees buyer invitations here, regardless of ?kind
        # — buyer invitations are owner-only (see FarmPartnerListCreateView
        # and FarmInvitationRevokeView for the matching write-side rule).
        if self.request.membership.role == FarmMembership.Role.MANAGER:
            qs = qs.exclude(account_role=User.Role.CONSUMER)

        return qs

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
            payload["accept_url"] = build_short_accept_url(
                invitation.token, invitation.phone_number
            )
            payload["detail"] = (
                f"Invitation created. Give this PIN to {invitation.full_name}."
            )
            payload["sms_sent"] = send_invitation_pin_sms(
                invitation, serializer.issued_pin
            )
        else:
            existing = serializer.existing_user
            detail, name_mismatch = existing_user_invite_message(
                invitation, existing
            )
            payload["existing_user"] = existing.full_name
            payload["name_mismatch"] = name_mismatch
            payload["detail"] = detail

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

        if request.membership.role == FarmMembership.Role.MANAGER:
            if invitation.account_role == User.Role.CONSUMER:
                return Response(
                    {"detail": "Only the farm owner can revoke a buyer invitation."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if invitation.membership_role != FarmMembership.Role.WORKER:
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

    @extend_schema(request=InvitationAcceptSerializer)
    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        payload = {
            "detail": "Invitation accepted.",
            "must_change_credential": user.must_change_credential,
        }
        # Only set when accepting just created the account and a personal
        # PIN was issued in the same call — the invitee signs straight in
        # instead of being sent back to the login screen.
        if serializer.tokens:
            payload.update(serializer.tokens)
        return Response(payload, status=status.HTTP_200_OK)


class AccessRequestCreateView(APIView):
    """
    POST /api/access-requests/

    The landing page's "Request access" form. Public, so it answers with
    the same fixed message whatever happens after validation — it never
    echoes the saved row back or says whether the SMS went out.

    "website" is a honeypot: the form hides it, so a person leaves it
    empty and a form-filling bot doesn't. A filled one gets the normal 201
    with nothing saved and nothing sent, so the bot learns nothing.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AccessRequestThrottle]

    SUCCESS = {"detail": "Thanks! We'll contact you soon."}

    @extend_schema(
        request=AccessRequestSerializer,
        responses={201: AccessRequestCreatedSerializer},
    )
    def post(self, request):
        honeypot = request.data.get("website") if hasattr(request.data, "get") else None
        if str(honeypot or "").strip():
            return Response(self.SUCCESS, status=status.HTTP_201_CREATED)

        serializer = AccessRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        access_request = serializer.save()
        notify_access_request(access_request)
        return Response(self.SUCCESS, status=status.HTTP_201_CREATED)
