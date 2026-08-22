# partners/permissions.py
from rest_framework.permissions import BasePermission

from .models import FarmPartnerLink


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