# farms/tests/test_archive.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestArchivedFarms:
    def test_owner_can_still_read(self, auth, owner, staffed_farm):
        """Analytics depend on archived history staying readable."""
        staffed_farm.archive()
        client = auth(owner)
        url = reverse("farms:farm-members", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 200

    def test_owner_cannot_write(self, auth, owner, staffed_farm):
        staffed_farm.archive()
        client = auth(owner)
        url = reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk})
        response = client.post(
            url,
            {
                "phone_number": "+639170000006",
                "full_name": "Too Late",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert response.status_code == 403

    def test_worker_cannot_see_archived_farm(self, auth, worker, staffed_farm):
        staffed_farm.archive()
        client = auth(worker)
        url = reverse("farms:farm-members", kwargs={"farm_pk": staffed_farm.pk})
        assert client.get(url).status_code == 403

    def test_owner_can_reactivate(self, auth, owner, staffed_farm):
        staffed_farm.archive()
        client = auth(owner)
        url = reverse("farms:farm-reactivate", kwargs={"farm_pk": staffed_farm.pk})
        assert client.post(url).status_code == 200

        staffed_farm.refresh_from_db()
        assert staffed_farm.is_active is True

    def test_non_owner_cannot_reactivate(self, auth, manager, staffed_farm):
        staffed_farm.archive()
        client = auth(manager)
        url = reverse("farms:farm-reactivate", kwargs={"farm_pk": staffed_farm.pk})
        assert client.post(url).status_code == 404