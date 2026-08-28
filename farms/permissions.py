# farms/permissions.py
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Farm, FarmMembership


class FarmScopedPermission(BasePermission):
    """
    Base for all farm-scoped access.

    Two role sets, because reads and writes are different questions:

      read_roles    — who may see this farm's data. Empty means any active
                      member, which is the common case: a worker needs to
                      read the batch they are recording against.
      allowed_roles — who may modify it.

    Subclasses that restrict writes should set `allowed_roles` and leave
    `read_roles` empty rather than overriding has_permission. An override
    that calls super() first cannot re-open a read the base already denied,
    which is how the worker read path got swallowed previously.
    """

    allowed_roles = set()
    read_roles = set()
    farm_url_kwarg = "farm_pk"
    message = "You do not have access to this farm."

    def get_farm(self, request, view):
        farm_id = view.kwargs.get(self.farm_url_kwarg) or view.kwargs.get("pk")
        if not farm_id:
            return None
        # No is_active filter here — archive rules are applied below so we
        # can distinguish "no access" from "archived, read-only".
        return Farm.objects.filter(pk=farm_id).first()

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

        if not farm.is_active:
            if membership.role != FarmMembership.Role.OWNER:
                self.message = "This farm has been archived."
                return False
            if request.method not in SAFE_METHODS:
                self.message = "This farm is archived and is read-only."
                return False

        request.farm = farm
        request.membership = membership

        # Reads resolve first. A worker must be able to read the batch they
        # are recording against, even on a view whose writes are restricted.
        if request.method in SAFE_METHODS:
            if not self.read_roles:
                return True
            return membership.role in self.read_roles

        if not self.allowed_roles:
            return True
        return membership.role in self.allowed_roles


class IsFarmMember(FarmScopedPermission):
    """Any active member of this farm: owner, manager, or worker."""

    allowed_roles = set()
    read_roles = set()


class IsFarmManagerOrOwner(FarmScopedPermission):
    """Staff-management authority. Workers are excluded from writes."""

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()


class IsFarmOwner(FarmScopedPermission):
    """Owner-only. Ownership transfer, archiving, the ownership ledger."""

    allowed_roles = {FarmMembership.Role.OWNER}
    read_roles = {FarmMembership.Role.OWNER}


class IsFarmMemberReadOnly(FarmScopedPermission):
    """Everyone active can read; only owner and manager can write."""

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()


class CanInviteRole(FarmScopedPermission):
    """
    Managers may invite workers only; granting MANAGER authority is
    reserved to the owner.

    The role check is authorization, not validation, which is why this
    returns 403 rather than 400. The override is safe here because super()
    already returns True for reads before the payload is inspected.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Only the farm owner can grant manager-level access."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True

        requested_role = (request.data.get("membership_role") or "").upper()
        if request.membership.role == FarmMembership.Role.OWNER:
            return True
        return requested_role == FarmMembership.Role.WORKER


class IsExternalPartner(BasePermission):
    """Suppliers and consumers. No farm membership, so no farm scoping."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and not user.is_internal)


class HasRotatedCredential(BasePermission):
    """
    THE GATE.

    A user still holding an owner-issued PIN has not established sole
    knowledge of their credential, so nothing they do is yet non-repudiable.
    Until they rotate it, every endpoint is closed except the ones needed
    to fix that.
    """

    message = "You must change your initial PIN before continuing."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return not user.must_change_credential