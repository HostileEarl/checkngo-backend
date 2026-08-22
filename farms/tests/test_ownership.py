# farms/tests/test_ownership.py
import pytest
from django.urls import reverse

from farms.models import FarmMembership, FarmOwnershipHistory

pytestmark = pytest.mark.django_db


class TestOwnershipTransfer:
    def test_registration_writes_first_ledger_entry(self, farm, owner):
        history = FarmOwnershipHistory.objects.filter(farm=farm)
        assert history.count() == 1
        entry = history.first()
        assert entry.from_owner is None
        assert entry.to_owner == owner
        assert entry.to_owner_name == "Maria Santos"

    def test_transfer_requires_confirmation(self, auth, owner, staffed_farm, manager):
        client = auth(owner)
        url = reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk})
        response = client.post(
            url,
            {"new_owner_phone": manager.phone_number, "confirm_transfer": False},
            format="json",
        )
        assert response.status_code == 400

    def test_transfer_succeeds(self, auth, owner, staffed_farm, manager):
        client = auth(owner)
        url = reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk})
        response = client.post(
            url,
            {
                "new_owner_phone": manager.phone_number,
                "confirm_transfer": True,
                "note": "Handover",
            },
            format="json",
        )
        assert response.status_code == 200

        staffed_farm.refresh_from_db()
        assert staffed_farm.owner == manager

    def test_old_owner_loses_all_access(self, auth, owner, staffed_farm, manager, api):
        client = auth(owner)
        client.post(
            reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk}),
            {"new_owner_phone": manager.phone_number, "confirm_transfer": True},
            format="json",
        )

        assert not FarmMembership.objects.filter(
            farm=staffed_farm, user=owner
        ).exists()

        client = auth(owner)
        url = reverse("farms:farm-members", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 403

    def test_history_survives_the_transfer(self, auth, owner, staffed_farm, manager):
        client = auth(owner)
        client.post(
            reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk}),
            {"new_owner_phone": manager.phone_number, "confirm_transfer": True},
            format="json",
        )

        history = FarmOwnershipHistory.objects.filter(farm=staffed_farm).order_by(
            "transferred_at"
        )
        assert history.count() == 2
        latest = history.last()
        assert latest.from_owner_name == "Maria Santos"
        assert latest.to_owner_name == "Jose Cruz"

    def test_history_is_immutable(self, farm):
        entry = FarmOwnershipHistory.objects.filter(farm=farm).first()
        entry.note = "tampered"
        with pytest.raises(ValueError):
            entry.save()

    def test_cannot_transfer_to_external_partner(self, auth, owner, staffed_farm, supplier):
        client = auth(owner)
        url = reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk})
        response = client.post(
            url,
            {"new_owner_phone": supplier.phone_number, "confirm_transfer": True},
            format="json",
        )
        assert response.status_code == 400

    def test_manager_cannot_transfer(self, auth, manager, staffed_farm, worker):
        client = auth(manager)
        url = reverse("farms:ownership-transfer", kwargs={"farm_pk": staffed_farm.pk})
        response = client.post(
            url,
            {"new_owner_phone": worker.phone_number, "confirm_transfer": True},
            format="json",
        )
        assert response.status_code == 403