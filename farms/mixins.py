# farms/mixins.py
from django.shortcuts import get_object_or_404

from .models import Farm


class FarmScopedViewMixin:
    """
    Guarantees every queryset in a farm-scoped view is filtered to that farm.

    Data isolation lives here rather than in each view's get_queryset, so
    Farm A's records cannot leak into a Farm B response through an oversight
    in one endpoint.
    """

    farm_url_kwarg = "farm_pk"
    farm_field = "farm"

    @property
    def farm(self):
        # Set by FarmScopedPermission; fall back for safety.
        if hasattr(self.request, "farm"):
            return self.request.farm
        return get_object_or_404(
            Farm, pk=self.kwargs[self.farm_url_kwarg], is_active=True
        )

    @property
    def membership(self):
        return getattr(self.request, "membership", None)

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(**{self.farm_field: self.farm})

    def perform_create(self, serializer):
        serializer.save(**{self.farm_field: self.farm})