# partners/permissions.py
from rest_framework.permissions import BasePermission

from farms.models import FarmMembership
from farms.permissions import FarmScopedPermission

from .models import FarmPartnerLink


class IsFarmOwnerForBuyerManagement(FarmScopedPermission):
    """
    Creating a buyer invitation, archiving (unlinking), and restoring a
    buyer link: owner only. A manager keeps reading the partner directory
    (unlike buyer *invitations*, which a manager never sees — see
    accounts.views.FarmInvitationListCreateView) but cannot act on it.
    """

    allowed_roles = {FarmMembership.Role.OWNER}
    read_roles = {FarmMembership.Role.OWNER, FarmMembership.Role.MANAGER}
    message = "Only the farm owner can manage buyers."


class IsLinkedPartner(BasePermission):
    """
    An external partner acting on a farm they are linked to.

    Deliberately narrow: this grants standing to transact with the farm,
    never to read its operational data.
    """

    farm_url_kwarg = "farm_pk"

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or user.is_internal:
            return False

        farm_id = view.kwargs.get(self.farm_url_kwarg)
        if not farm_id:
            return False

        link = FarmPartnerLink.objects.filter(
            farm_id=farm_id, partner=user, is_active=True
        ).select_related("farm").first()
        if link is None or not link.farm.is_active:
            return False

        request.partner_link = link
        return True