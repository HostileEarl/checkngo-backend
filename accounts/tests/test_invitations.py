# accounts/tests/test_invitations.py
import pytest
from django.urls import reverse

from accounts.models import Invitation, User
from farms.models import FarmMembership

pytestmark = pytest.mark.django_db


class TestInvitationIssue:
    def test_pin_returned_once_for_new_person(self, auth, owner, staffed_farm):
        client = auth(owner)
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000010",
                "full_name": "Brand New",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert response.status_code == 201
        assert len(response.data["pin"]) == 6
        assert response.data["pin"].isdigit()

    def test_pin_is_hashed_not_stored(self, auth, owner, staffed_farm):
        client = auth(owner)
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000011",
                "full_name": "Hash Check",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        pin = response.data["pin"]
        invitation = Invitation.objects.get(pk=response.data["id"])
        assert invitation.pin_hash != pin
        assert invitation.pin_hash.startswith("pbkdf2_sha256$")
        assert invitation.check_pin(pin) is True

    def test_existing_user_gets_no_pin(self, auth, owner, staffed_farm, rival_farm, worker):
        """The upsert path: an existing account keeps its own credential."""
        rival_farm.owner = owner
        rival_farm.save()

        client = auth(owner)
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": rival_farm.pk}),
            {
                "phone_number": worker.phone_number,
                "full_name": worker.full_name,
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert response.status_code == 201
        assert "pin" not in response.data
        assert response.data["existing_user"] == "Ana Reyes"

    def test_duplicate_member_rejected(self, auth, owner, staffed_farm, worker):
        client = auth(owner)
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": worker.phone_number,
                "full_name": worker.full_name,
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_cannot_invite_as_owner(self, auth, owner, staffed_farm):
        client = auth(owner)
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000012",
                "full_name": "Usurper",
                "account_role": "OWNER",
                "membership_role": "OWNER",
            },
            format="json",
        )
        assert response.status_code == 400


class TestInvitationAccept:
    def test_new_user_created_with_gate_on(self, api, owner, staffed_farm, auth):
        client = auth(owner)
        created = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000020",
                "full_name": "Accept Me",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        pin = created.data["pin"]
        invitation = Invitation.objects.get(pk=created.data["id"])

        api.force_authenticate(user=None)
        response = api.post(
            reverse("accounts:invitation-accept"),
            {"token": invitation.token, "pin": pin},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["must_change_credential"] is True

        user = User.objects.get(phone_number="+639170000020")
        assert user.must_change_credential is True
        assert FarmMembership.objects.filter(user=user, farm=staffed_farm).exists()

    def test_wrong_pin_rejected(self, api, owner, staffed_farm, auth):
        client = auth(owner)
        created = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000021",
                "full_name": "Wrong Pin",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        invitation = Invitation.objects.get(pk=created.data["id"])

        api.force_authenticate(user=None)
        response = api.post(
            reverse("accounts:invitation-accept"),
            {"token": invitation.token, "pin": "000000"},
            format="json",
        )
        assert response.status_code == 400
        assert not User.objects.filter(phone_number="+639170000021").exists()

    def test_invitation_is_single_use(self, api, owner, staffed_farm, auth):
        client = auth(owner)
        created = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000022",
                "full_name": "Once Only",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        pin = created.data["pin"]
        invitation = Invitation.objects.get(pk=created.data["id"])

        api.force_authenticate(user=None)
        payload = {"token": invitation.token, "pin": pin}
        assert api.post(reverse("accounts:invitation-accept"), payload, format="json").status_code == 200
        assert api.post(reverse("accounts:invitation-accept"), payload, format="json").status_code == 400

    def test_bad_token_rejected(self, api):
        response = api.post(
            reverse("accounts:invitation-accept"),
            {"token": "not-a-real-token", "pin": "123456"},
            format="json",
        )
        assert response.status_code == 400