# production/permissions.py
from rest_framework.permissions import SAFE_METHODS

from farms.models import FarmMembership
from farms.permissions import FarmScopedPermission


class CanViewProduction(FarmScopedPermission):
    """
    Read access to production data. Any active member of the farm.

    Inherits the archive rule from the base: an archived farm stays readable
    by the owner, which is the entire point of keeping historical batches.
    """

    allowed_roles = set()


class CanManageBatches(FarmScopedPermission):
    """
    Placement, harvest, house setup — owner and manager only.

    Per the ruling: creating a batch commits bird counts and capital, which
    is a business decision. Workers record against batches, they do not
    open or close them.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Only an owner or manager can place or close a batch."

    def has_permission(self, request, view):
        if not FarmScopedPermission.has_permission(self, request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.membership.role in self.allowed_roles


class CanRecordDaily(FarmScopedPermission):
    """
    Daily records and weight samples — every active member, workers included.

    This is the one write path a worker holds, and it is deliberately the
    widest: the person in the shed is the person with the numbers.
    """

    allowed_roles = set()


class CanCorrectLockedRecord(FarmScopedPermission):
    """
    Override the 24-hour lock. Manager and owner.

    Not owner-only: a day-3 typo is operational, and routing every one
    through the owner creates a bottleneck on a farm where the owner is
    not on site.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Only an owner or manager can correct a locked record."

    def has_permission(self, request, view):
        if not FarmScopedPermission.has_permission(self, request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.membership.role in self.allowed_roles


class CanManageFeed(FarmScopedPermission):
    """
    Feed deliveries carry cost and supplier attribution, so they sit with
    owner/manager. Consumption (the daily feed_kg field) stays with workers.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Only an owner or manager can record feed deliveries."

    def has_permission(self, request, view):
        if not FarmScopedPermission.has_permission(self, request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.membership.role in self.allowed_roles