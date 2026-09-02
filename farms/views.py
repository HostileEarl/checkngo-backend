# farms/views.py
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Farm, FarmMembership, FarmOwnershipHistory
from .permissions import (
    HasRotatedCredential,
    IsFarmManagerOrOwner,
    IsFarmMember,
    IsFarmOwner,
)
from .serializers import (
    FarmMembershipSerializer,
    FarmOwnershipHistorySerializer,
    FarmSerializer,
    OwnershipTransferSerializer,
)


class FarmListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/farms/ — farms this user belongs to, archived ones included
    POST /api/farms/ — create a farm; the signal grants OWNER membership
    """

    serializer_class = FarmSerializer
    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def get_queryset(self):
        return (
            Farm.objects.filter(
                memberships__user=self.request.user,
                memberships__is_active=True,
            )
            .select_related("owner")
            .distinct()
        )

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class FarmDetailView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/farms/<pk>/ — archived farms are read-only."""

    serializer_class = FarmSerializer
    permission_classes = [IsFarmManagerOrOwner]
    lookup_url_kwarg = "pk"

    def get_object(self):
        return self.request.farm  # already resolved and authorized


class FarmMemberListView(generics.ListAPIView):
    """GET /api/farms/<farm_pk>/members/ — the farm's roster."""

    serializer_class = FarmMembershipSerializer
    permission_classes = [IsFarmMember]

    def get_queryset(self):
        return (
            FarmMembership.objects.filter(farm=self.request.farm)
            .select_related("user", "invited_by")
            .prefetch_related("houses")
        )


class FarmMemberHouseAssignmentView(APIView):
    """
    PATCH /api/farms/<farm_pk>/members/<pk>/houses/

    Set which houses a member may record against. Owner/manager only — a
    worker must not be able to assign themselves houses, which is why this
    is a dedicated endpoint rather than general membership editing.

    Body: {"houses": [<house id>, ...]}. An empty list clears the
    restriction (the member may then record against any house).
    """

    permission_classes = [IsFarmManagerOrOwner]

    def patch(self, request, farm_pk, pk):
        membership = get_object_or_404(
            FarmMembership, pk=pk, farm=request.farm
        )
        serializer = FarmMembershipSerializer(
            membership,
            data={"houses": request.data.get("houses", [])},
            partial=True,
            context={"request": request, "farm": request.farm},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class FarmOwnershipHistoryView(generics.ListAPIView):
    """
    GET /api/farms/<farm_pk>/ownership-history/

    Owner-only: the ledger names prior owners, which is not roster
    information a worker needs.
    """

    serializer_class = FarmOwnershipHistorySerializer
    permission_classes = [IsFarmOwner]

    def get_queryset(self):
        return FarmOwnershipHistory.objects.filter(
            farm=self.request.farm
        ).select_related("performed_by")


class FarmMemberRevokeView(APIView):
    """
    POST /api/farms/<farm_pk>/members/<pk>/revoke/

    Soft revocation — the row survives so historical records still resolve
    to a person who demonstrably had access at the time.
    """

    permission_classes = [IsFarmManagerOrOwner]

    def post(self, request, farm_pk, pk):
        membership = get_object_or_404(FarmMembership, pk=pk, farm=request.farm)

        if membership.role == FarmMembership.Role.OWNER:
            return Response(
                {"detail": "The owner's access cannot be revoked. Transfer ownership instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (
            request.membership.role == FarmMembership.Role.MANAGER
            and membership.role == FarmMembership.Role.MANAGER
        ):
            return Response(
                {"detail": "Only the owner can revoke a manager."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if membership.user == request.user:
            return Response(
                {"detail": "You cannot revoke your own access."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership.deactivate()
        return Response(
            {"detail": f"{membership.user.full_name}'s access has been revoked."},
            status=status.HTTP_200_OK,
        )


class FarmArchiveView(APIView):
    """POST /api/farms/<farm_pk>/archive/ — close to new data, keep history."""

    permission_classes = [IsFarmOwner]

    def post(self, request, farm_pk):
        request.farm.archive()
        return Response(
            {"detail": "Farm archived. Historical data remains available to you."},
            status=status.HTTP_200_OK,
        )


class FarmReactivateView(APIView):
    """
    POST /api/farms/<farm_pk>/reactivate/

    Deliberately NOT farm-scoped: FarmScopedPermission blocks writes to
    archived farms, so a scoped permission could never let an owner
    un-archive one. Ownership is verified directly here instead.
    """

    permission_classes = [IsAuthenticated, HasRotatedCredential]

    def post(self, request, farm_pk):
        farm = get_object_or_404(Farm, pk=farm_pk, owner=request.user)
        farm.reactivate()
        return Response({"detail": "Farm reactivated."}, status=status.HTTP_200_OK)


class OwnershipTransferView(APIView):
    """POST /api/farms/<farm_pk>/transfer-ownership/ — owner only."""

    permission_classes = [IsFarmOwner]

    def post(self, request, farm_pk):
        serializer = OwnershipTransferSerializer(
            data=request.data, context={"farm": request.farm, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        farm = serializer.save()
        return Response(
            {
                "detail": f"Ownership transferred to {farm.owner.full_name}.",
                "farm": FarmSerializer(farm).data,
            },
            status=status.HTTP_200_OK,
        )