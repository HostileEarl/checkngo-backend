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

    def test_token_and_accept_url_on_create_only(self, auth, owner, staffed_farm, settings):
        """
        The token must reach the issuer exactly once, at creation — it's
        the deep-link half of the two-factor handoff (link + out-of-band
        PIN). The list endpoint must never carry it: anyone who can read
        the invitation list would otherwise hold every pending token,
        which paired with a 6-digit PIN is meaningfully weaker.
        """
        client = auth(owner)
        created = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000030",
                "full_name": "Token Check",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert created.status_code == 201
        invitation = Invitation.objects.get(pk=created.data["id"])
        assert created.data["token"] == invitation.token
        assert created.data["accept_url"] == (
            f"{settings.FRONTEND_URL}/accept-invite?token={invitation.token}"
        )

        listed = client.get(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk})
        )
        assert listed.status_code == 200
        entry = next(row for row in listed.data if row["id"] == invitation.pk)
        assert "token" not in entry
        assert "accept_url" not in entry

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


class TestInvitationRevoke:
    @staticmethod
    def _issue(client, farm, phone, name="Wrong Number"):
        response = client.post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": farm.pk}),
            {
                "phone_number": phone,
                "full_name": name,
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        return response.data["id"]

    def test_owner_revokes_pending_invitation(self, auth, owner, staffed_farm):
        client = auth(owner)
        invite_id = self._issue(client, staffed_farm, "+639170000040")

        response = client.post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            )
        )
        assert response.status_code == 200
        assert response.data["status"] == "REVOKED"
        assert Invitation.objects.get(pk=invite_id).status == Invitation.Status.REVOKED

    def test_revoke_frees_phone_for_reinvite(self, auth, owner, staffed_farm):
        """The point of the feature: the corrected number is usable at once."""
        client = auth(owner)
        url = reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk})
        phone = "+639170000041"

        invite_id = self._issue(client, staffed_farm, phone)

        # The partial unique constraint blocks a second pending invite.
        blocked = client.post(
            url,
            {
                "phone_number": phone,
                "full_name": "Right Person",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert blocked.status_code == 400

        revoked = client.post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            )
        )
        assert revoked.status_code == 200

        reinvited = client.post(
            url,
            {
                "phone_number": phone,
                "full_name": "Right Person",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        assert reinvited.status_code == 201

    def test_worker_cannot_revoke(self, auth, owner, worker, staffed_farm):
        invite_id = self._issue(auth(owner), staffed_farm, "+639170000042")

        response = auth(worker).post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            )
        )
        assert response.status_code == 403
        assert Invitation.objects.get(pk=invite_id).status == Invitation.Status.PENDING

    def test_cannot_revoke_accepted_invitation(self, auth, owner, staffed_farm):
        client = auth(owner)
        invite_id = self._issue(client, staffed_farm, "+639170000043")
        Invitation.objects.filter(pk=invite_id).update(
            status=Invitation.Status.ACCEPTED
        )

        response = client.post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            )
        )
        assert response.status_code == 400
        assert Invitation.objects.get(pk=invite_id).status == Invitation.Status.ACCEPTED

    def test_manager_cannot_revoke_manager_invitation_claiming_worker(
        self, auth, owner, manager, staffed_farm
    ):
        """
        The revoke permission used to read membership_role from the request
        body, so a manager could satisfy it by claiming WORKER while the
        invitation actually grants MANAGER. The view now reads the stored
        invitation's role, so the body claim is ignored and this is a 403.
        """
        created = auth(owner).post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000045",
                "full_name": "Deputy Manager",
                "account_role": "MANAGER",
                "membership_role": "MANAGER",
            },
            format="json",
        )
        assert created.status_code == 201, created.data
        invite_id = created.data["id"]

        response = auth(manager).post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            ),
            {"membership_role": "WORKER"},
            format="json",
        )
        assert response.status_code == 403
        assert Invitation.objects.get(pk=invite_id).status == Invitation.Status.PENDING

    def test_manager_can_revoke_worker_invitation(self, auth, owner, manager, staffed_farm):
        """The legitimate case still works: a manager revoking a worker invite."""
        invite_id = self._issue(auth(owner), staffed_farm, "+639170000046")

        response = auth(manager).post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": invite_id},
            )
        )
        assert response.status_code == 200
        assert Invitation.objects.get(pk=invite_id).status == Invitation.Status.REVOKED

    def test_cannot_revoke_invitation_from_another_farm(
        self, auth, owner, rival_owner, staffed_farm, rival_farm
    ):
        foreign_id = self._issue(auth(rival_owner), rival_farm, "+639170000044")

        response = auth(owner).post(
            reverse(
                "farms:invitation-revoke",
                kwargs={"farm_pk": staffed_farm.pk, "pk": foreign_id},
            )
        )
        assert response.status_code == 404
        assert Invitation.objects.get(pk=foreign_id).status == Invitation.Status.PENDING