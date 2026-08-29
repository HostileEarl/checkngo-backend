# partners/views.py
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import InvitationReadSerializer
from farms.permissions import HasRotatedCredential, IsFarmManagerOrOwner
from production.models import FeedDelivery, Harvest

from .models import FarmPartnerLink
from .serializers import (
    BuyerPurchaseSerializer,
    FarmPartnerLinkSerializer,
    PartnerInvitationCreateSerializer,
    SupplierDeliverySerializer,
)


class FarmPartnerListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/<farm_pk>/partners/ — this farm's supplier/consumer directory
    POST /api/farms/<farm_pk>/partners/ — invite a new partner

    Owner and manager both permitted: unlike manager promotion, adding a
    supplier grants no authority over the farm, so it needs no owner gate.
    """

    permission_classes = [IsFarmManagerOrOwner]

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
        else:
            name = serializer.existing_user.full_name
            payload["existing_user"] = name
            payload["detail"] = (
                f"{name} already has an account and will use their existing PIN."
            )

        return Response(payload, status=status.HTTP_201_CREATED)


class FarmPartnerUnlinkView(APIView):
    """
    POST /api/farms/<farm_pk>/partners/<pk>/unlink/

    Soft — the relationship ends, the record of it having existed does not.
    """

    permission_classes = [IsFarmManagerOrOwner]

    def post(self, request, farm_pk, pk):
        link = get_object_or_404(FarmPartnerLink, pk=pk, farm=request.farm)
        link.deactivate()
        return Response(
            {"detail": f"{link.partner.full_name} is no longer linked to this farm."},
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


class MyDeliveriesView(generics.ListAPIView):
    """
    GET /api/partners/my-deliveries/ — a supplier's view of what farms
    recorded receiving from them.

    Scoped to FeedDelivery rows whose supplier_link points at the requesting
    user. Nothing about flocks, staff, or production, and never another
    supplier's rows. Internal staff have no supplier identity here, so they
    get an empty list rather than a 403.
    """

    serializer_class = SupplierDeliverySerializer
    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return FeedDelivery.objects.none()

        qs = (
            FeedDelivery.objects.filter(supplier_link__partner=user)
            .select_related("farm", "supplier_link")
            .order_by("-delivery_date")
        )

        farm_id = self.request.query_params.get("farm")
        if farm_id and str(farm_id).isdigit():
            qs = qs.filter(farm_id=farm_id)
        return qs


class MyPurchasesView(generics.ListAPIView):
    """
    GET /api/partners/my-purchases/ — a buyer's view of what farms recorded
    selling them.

    Scoped to Harvest rows whose buyer_link points at the requesting user.
    A weight and delivery ledger only: no revenue (see BuyerPurchaseSerializer
    for why), no farm annotations, nothing about mortality, feed, or FCR, and
    never another buyer's rows. Internal staff have no buyer identity here, so
    they get an empty list rather than a 403.
    """

    serializer_class = BuyerPurchaseSerializer
    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return Harvest.objects.none()

        qs = (
            Harvest.objects.filter(buyer_link__partner=user)
            .select_related("batch__house__farm", "buyer_link")
            .order_by("-harvest_date")
        )

        farm_id = self.request.query_params.get("farm")
        if farm_id and str(farm_id).isdigit():
            qs = qs.filter(batch__house__farm_id=farm_id)
        return qs