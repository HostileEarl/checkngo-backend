# production/permissions.py
from farms.models import FarmMembership
from farms.permissions import FarmScopedPermission


class CanViewProduction(FarmScopedPermission):
    """Read access. Any active member of the farm."""

    allowed_roles = set()
    read_roles = set()


class CanManageBatches(FarmScopedPermission):
    """
    Placement, harvest, house setup — owner and manager only.

    Reads stay open to workers: a worker needs to see the batch they are
    recording against. The base class applies read_roles before
    allowed_roles, so no override is needed here.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()
    message = "Only an owner or manager can place or close a batch."


class CanRecordDaily(FarmScopedPermission):
    """Daily records and weight samples — every active member, workers included."""

    allowed_roles = set()
    read_roles = set()


class CanCorrectLockedRecord(FarmScopedPermission):
    """Override the 24-hour lock. Manager and owner."""

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()
    message = "Only an owner or manager can correct a locked record."


class CanManageFeed(FarmScopedPermission):
    """
    Feed deliveries carry cost and supplier attribution, so writes sit with
    owner and manager. Consumption (the daily feed_kg field) stays with workers.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()
    message = "Only an owner or manager can record feed deliveries."


class CanManageInventory(FarmScopedPermission):
    """
    Inventory items, their reorder thresholds, and stock-ins — owner and
    manager only, the same split as feed deliveries. Reads stay open to
    workers: a worker needs to see the item they are logging usage against.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()
    message = "Only an owner or manager can manage inventory items."


class CanRecordInventoryUsage(FarmScopedPermission):
    """Inventory usage logs — every active member, workers included."""

    allowed_roles = set()
    read_roles = set()


class CanManageRoutine(FarmScopedPermission):
    """
    The daily task routine (templates) — owner and manager only, the same
    split as feed and inventory. Reads stay open to workers: a worker needs
    the routine to know what to tick off. Ticking itself is CanRecordDaily.
    """

    allowed_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    read_roles = set()
    message = "Only an owner or manager can change the daily routine."