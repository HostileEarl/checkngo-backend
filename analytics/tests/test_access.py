# analytics/tests/test_access.py
"""Analytics inherits farm scoping — these prove it wasn't lost in a new app."""
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestAnalyticsAccess:
    def test_worker_can_read_operational_analytics(self, auth, worker, staffed_farm):
        client = auth(worker)
        url = reverse("analytics:fcr", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 200

    def test_worker_cannot_read_financials(self, auth, worker, staffed_farm):
        """Revenue and margins are not worker data."""
        client = auth(worker)
        url = reverse("analytics:profitability", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 403

    def test_manager_can_read_financials(self, auth, manager, staffed_farm):
        client = auth(manager)
        url = reverse("analytics:profitability", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 200

    def test_rival_owner_blocked(self, auth, rival_owner, staffed_farm):
        client = auth(rival_owner)
        url = reverse("analytics:dashboard", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 403

    def test_supplier_blocked(self, auth, supplier, staffed_farm):
        client = auth(supplier)
        url = reverse("analytics:dashboard", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 403

    def test_archived_farm_readable_by_owner(self, auth, owner, staffed_farm):
        """The whole reason archived farms stay readable."""
        staffed_farm.archive()
        client = auth(owner)
        url = reverse("analytics:fcr", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 200