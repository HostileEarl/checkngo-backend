# production/mixins.py
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS

from .models import Batch


class BatchScopedMixin:
    """
    Resolves the batch from the URL and confirms it belongs to the farm the
    permission layer already authorized.

    The farm check is not redundant: without it, a member of Farm A could
    pass Farm A's id in the URL and Farm B's batch id in the path, and the
    permission class would happily approve the farm while the queryset
    served another farm's records.

    On a non-safe method it also enforces house-level write scoping: a
    worker restricted to specific houses may only write against batches in
    those houses. Reads are untouched. Views whose writes need per-record
    handling (bulk-sync) set `enforce_house_write_scope = False` and do the
    check themselves.
    """

    batch_url_kwarg = "batch_pk"
    enforce_house_write_scope = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if (
            self.enforce_house_write_scope
            and request.method not in SAFE_METHODS
            and getattr(request, "membership", None) is not None
            and not request.membership.may_write_to_house(self.batch.house)
        ):
            raise PermissionDenied(
                f"You are not assigned to {self.batch.house.name}. "
                "Ask your manager to assign you."
            )

    @property
    def batch(self):
        if not hasattr(self, "_batch"):
            self._batch = get_object_or_404(
                Batch.objects.select_related("house__farm"),
                pk=self.kwargs[self.batch_url_kwarg],
                house__farm=self.request.farm,
            )
        return self._batch

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["farm"] = self.request.farm
        context["batch"] = self.batch
        return context