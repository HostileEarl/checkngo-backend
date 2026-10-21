# partners/views.py
from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import (
    InvitationReadSerializer,
    existing_user_invite_message,
    send_invitation_pin_sms,
)
from farms.permissions import HasRotatedCredential
from production.models import Harvest, Payment, SaleEvent, per_sale_mode_batch_ids

from .models import FarmPartnerLink
from .permissions import IsFarmOwnerForBuyerManagement
from .serializers import (
    BuyerPaymentSerializer,
    BuyerPurchaseSerializer,
    BuyerSalePurchaseSerializer,
    FarmPartnerLinkSerializer,
    PartnerInvitationCreateSerializer,
)


class FarmPartnerListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/partners/ — this farm's consumer directory
    POST /api/farms/<farm_pk>/partners/ — invite a new partner

    Reading the directory stays open to owner and manager alike. Inviting
    a buyer is owner-only — unlike manager promotion this grants no
    authority over the farm, but the owner wants to be the one deciding
    who the farm transacts with.
    """

    permission_classes = [IsFarmOwnerForBuyerManagement]

    def get_serializer_class(self):
        return (
            PartnerInvitationCreateSerializer
            if self.request.method == "POST"
            else FarmPartnerLinkSerializer
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        return context

    def get_queryset(self):
        qs = FarmPartnerLink.objects.filter(farm=self.request.farm).select_related(
            "farm", "partner", "linked_by"
        )
        link_type = self.request.query_params.get("type")
        if link_type:
            qs = qs.filter(link_type=link_type.upper())

        # Default (no ?status, or any value other than active/archived)
        # keeps today's behaviour: every link, active and archived alike.
        status_param = (self.request.query_params.get("status") or "").lower()
        if status_param == "active":
            qs = qs.filter(is_active=True)
        elif status_param == "archived":
            qs = qs.filter(is_active=False)

        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = serializer.save()

        payload = InvitationReadSerializer(invitation).data

        if serializer.issued_pin:
            # See FarmInvitationListCreateView.create() — same split: the
            # token travels in the accept link, the PIN travels out-of-band.
            payload["pin"] = serializer.issued_pin
            payload["token"] = invitation.token
            payload["accept_url"] = (
                f"{settings.FRONTEND_URL}/accept-invite?token={invitation.token}"
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


class FarmPartnerUnlinkView(APIView):
    """
    POST /api/farms/<farm_pk>/partners/<pk>/unlink/

    Soft — the relationship ends, the record of it having existed does
    not. Owner only — see FarmPartnerListCreateView.
    """

    permission_classes = [IsFarmOwnerForBuyerManagement]

    def post(self, request, farm_pk, pk):
        link = get_object_or_404(FarmPartnerLink, pk=pk, farm=request.farm)
        link.deactivate()
        return Response(
            {"detail": f"{link.partner.full_name} is no longer linked to this farm."},
            status=status.HTTP_200_OK,
        )


class FarmPartnerRestoreView(APIView):
    """
    POST /api/farms/<farm_pk>/partners/<pk>/restore/

    Undo an unlink. Owner only — see FarmPartnerListCreateView. 400 if the
    link is already active; 404 if the link belongs to another farm (the
    farm scoping on the lookup below is what produces that, same as every
    other farm-scoped detail action).
    """

    permission_classes = [IsFarmOwnerForBuyerManagement]

    def post(self, request, farm_pk, pk):
        link = get_object_or_404(FarmPartnerLink, pk=pk, farm=request.farm)
        if link.is_active:
            return Response(
                {"detail": f"{link.partner.full_name} is already linked to this farm."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        link.reactivate()
        return Response(
            FarmPartnerLinkSerializer(link).data,
            status=status.HTTP_200_OK,
        )


class MyPartnerFarmsView(generics.ListAPIView):
    """
    GET /api/partners/my-farms/ — the partner's own view of who they work with.

    Returns only the link records, never the farms' operational data.
    """

    serializer_class = FarmPartnerLinkSerializer
    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return FarmPartnerLink.objects.none()
        return FarmPartnerLink.objects.filter(
            partner=user, is_active=True
        ).select_related("farm", "partner", "linked_by")


@extend_schema(
    responses=PolymorphicProxySerializer(
        component_name="BuyerPurchaseRow",
        serializers=[BuyerPurchaseSerializer, BuyerSalePurchaseSerializer],
        resource_type_field_name="source",
        many=True,
    ),
)
class MyPurchasesView(generics.ListAPIView):
    """
    GET /api/partners/my-purchases/ — a buyer's view of what farms recorded
    selling them.

    Two sources, merged: Harvest rows whose buyer_link points at the
    requesting user (the batch's overall buyer, recorded once at closing),
    and SaleEvent rows whose own buyer_link points at them (a per-sale
    buyer — see SaleEvent.buyer_link). Each row carries a "source" field
    ("harvest" or "sale") so the two are distinguishable.

    No double counting, without breaking legacy batches: a batch is only
    in "per-sale" mode — reporting through its SaleEvent rows and
    dropping the Harvest row entirely — once at least one of its sale
    events actually carries a buyer_link (see
    production.models.per_sale_mode_batch_ids, the single source of truth
    for this rule, shared with the sales-history report's buyer column).
    A batch whose sale events are all unlinked (every existing production
    batch, today) is NOT in per-sale mode — its Harvest.buyer_link is
    shown exactly as before. A buyer therefore never sees the same batch
    counted on both sides, and never loses a purchase that predates
    per-sale buyers.

    A weight and delivery ledger only: no revenue (see
    BuyerPurchaseSerializer / BuyerSalePurchaseSerializer for why), no farm
    annotations, nothing about mortality, feed, or FCR, and never another
    buyer's rows. Internal staff have no buyer identity here, so they get
    an empty list rather than a 403.
    """

    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def list(self, request, *args, **kwargs):
        user = request.user
        if user.is_internal:
            return Response([])

        farm_id = request.query_params.get("farm")

        sales = SaleEvent.objects.filter(buyer_link__partner=user).select_related(
            "batch__house__farm", "buyer_link"
        )
        if farm_id and str(farm_id).isdigit():
            sales = sales.filter(batch__house__farm_id=farm_id)

        # Only a batch already in per-sale mode (>=1 buyer-linked sale)
        # drops its Harvest row — see the no-double-counting note above.
        harvests = (
            Harvest.objects.filter(buyer_link__partner=user)
            .exclude(batch_id__in=per_sale_mode_batch_ids())
            .select_related("batch__house__farm", "buyer_link")
        )
        if farm_id and str(farm_id).isdigit():
            harvests = harvests.filter(batch__house__farm_id=farm_id)

        rows = [
            *BuyerPurchaseSerializer(harvests, many=True).data,
            *BuyerSalePurchaseSerializer(sales, many=True).data,
        ]
        rows.sort(key=lambda r: r["harvest_date"], reverse=True)
        return Response(rows)


class MyPaymentsView(generics.ListAPIView):
    """
    GET /api/partners/my-payments/ — a buyer's view of payments they made.

    Scoped to Payment rows whose buyer_link points at the requesting user,
    plus payments with no buyer_link of their own on a sale whose
    buyer_link does. Payments recorded before the app sent buyer_link have
    none, but their sale names the buyer; reversals follow the same rule.
    A payment explicitly attributed to someone else never shows here, even
    on this buyer's sale. Read-side only: PaymentSerializer still never
    infers buyer_link on write.

    A buyer sees only their own payments, never another buyer's, and
    nothing about the sale's revenue or outstanding balance (that's the
    farm's side of the ledger — see BuyerPaymentSerializer). Internal
    staff have no buyer identity here, so they get an empty list rather
    than a 403, same as MyPurchasesView and MyDeliveriesView.
    """

    serializer_class = BuyerPaymentSerializer
    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return Payment.objects.none()

        qs = (
            Payment.objects.filter(
                Q(buyer_link__partner=user)
                | Q(buyer_link__isnull=True, sale__buyer_link__partner=user)
            )
            .select_related("sale__batch__house__farm", "buyer_link")
            .order_by("-recorded_at")
        )

        farm_id = self.request.query_params.get("farm")
        if farm_id and str(farm_id).isdigit():
            qs = qs.filter(sale__batch__house__farm_id=farm_id)
        return qs