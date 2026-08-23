# production/mixins.py
from django.shortcuts import get_object_or_404

from .models import Batch


class BatchScopedMixin:
    """
    Resolves the batch from the URL and confirms it belongs to the farm the
    permission layer already authorized.

    The farm check is not redundant: without it, a member of Farm A could
    pass Farm A's id in the URL and Farm B's batch id in the path, and the
    permission class would happily approve the farm while the queryset
    served another farm's records.
    """

    batch_url_kwarg = "batch_pk"

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