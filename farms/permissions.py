# farms/permissions.py
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Farm, FarmMembership


class FarmScopedPermission(BasePermission):
    """
    Base for all farm-scoped access.

    Resolves the farm from the URL (per the URL-scoping decision), looks up
    the caller's ACTIVE membership, and caches it on the request so views
    and serializers can reuse it without re-querying.

    Subclasses declare `allowed_roles`; an empty set means "any active member".
    """

    allowed_roles = set()
    farm_url_kwarg = "farm_pk"

    def get_farm(self, request, view):
        farm_id = view.kwargs.get(self.farm_url_kwarg) or view.kwargs.get("pk")
        if not farm_id:
            return None
        return Farm.objects.filter(pk=farm_id, is_active=True).first()

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        farm = self.get_farm(request, view)
        if farm is None:
            return False

        membership = farm.membership_for(user)
        if membership is None:
            return False

        # Cache for downstream use — avoids a second identical query.
        request.farm = farm
        request.membership = membership

        if not self.allowed_roles:
            return True
        return membership.role in self.allowed_roles


class IsFarmMember(FarmScopedPermission):
    """Any active member of this farm: owner, manager, or worker."""

    allowed_roles = set()


class IsFarmManagerOrOwner(FarmScopedPermission):
    """Staff-management authority. Workers are excluded."""

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}


class IsFarmOwner(FarmScopedPermission):
    """Owner-only. Manager promotion, ownership transfer, farm deletion."""

    allowed_roles = {FarmMembership.Role.OWNER}


class IsFarmMemberReadOnly(FarmScopedPermission):
    """
    Everyone active can read; only owner/manager can write.

    Useful for reference data a worker consults but must not edit.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.membership.role in {
            FarmMembership.Role.OWNER,
            FarmMembership.Role.MANAGER,
        }


class CanInviteRole(FarmScopedPermission):
    """
    Enforces your hierarchy ruling: managers may invite workers only;
    granting MANAGER authority is reserved to the owner.

    Checked here rather than in the serializer because it's an authorization
    question, not a validation question — the distinction matters when a
    panelist asks where your access control lives.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True

        requested_role = (request.data.get("membership_role") or "").upper()
        if request.membership.role == FarmMembership.Role.OWNER:
            return True
        # Manager path: workers only.
        return requested_role == FarmMembership.Role.WORKER


class IsExternalPartner(BasePermission):
    """Suppliers and consumers. No farm membership, so no farm scoping."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and not user.is_internal)


class HasRotatedCredential(BasePermission):
    """
    The Task 7 gate, defined now so it's ready to wire in.

    A user still holding an owner-issued PIN has not established sole
    knowledge of their credential — so nothing they do is yet non-repudiable.
    Block everything except the endpoints needed to fix that.
    """

    message = "You must change your initial PIN before continuing."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return not user.must_change_credential