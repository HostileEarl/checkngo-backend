# partners/views.py
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import InvitationReadSerializer
from farms.permissions import HasRotatedCredential, IsFarmManagerOrOwner

from .models import FarmPartnerLink
from .serializers import FarmPartnerLinkSerializer, PartnerInvitationCreateSerializer


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
            "partner", "linked_by"
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