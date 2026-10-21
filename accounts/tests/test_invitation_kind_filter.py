# accounts/tests/test_invitation_kind_filter.py
"""
GET /api/farms/<farm_pk>/invitations/?kind=buyer|staff

Buyer (CONSUMER) invitations are saved with `farm` set — same as staff
invitations — so this one endpoint lists both kinds together unless
`kind` narrows it. A manager never sees buyer invitations here, with or
without the parameter: creating, listing-as-buyer, and revoking a buyer
invitation are all owner-only (see partners.permissions
.IsFarmOwnerForBuyerManagement for the write-side equivalent).
"""
import pytest
from django.urls import reverse

from accounts.models import Invitation, User

pytestmark = pytest.mark.django_db


def _invitations_url(farm):
    return reverse("farms:farm-invitations", kwargs={"farm_pk": farm.pk})


def _revoke_url(farm, pk):
    return reverse("farms:invitation-revoke", kwargs={"farm_pk": farm.pk, "pk": pk})


@pytest.fixture
def staff_invite(db, staffed_farm, owner):
    return Invitation.objects.create(
        farm=staffed_farm,
        invited_by=owner,
        phone_number="+639170001234",
        full_name="New Worker",
        account_role=User.Role.WORKER,
        membership_role="WORKER",
        pin_hash="x",
    )


@pytest.fixture
def buyer_invite(db, staffed_farm, owner):
    return Invitation.objects.create(
        farm=staffed_farm,
        invited_by=owner,
        phone_number="+639170005678",
        full_name="New Buyer",
        account_role=User.Role.CONSUMER,
        membership_role="",
        pin_hash="x",
    )


class TestKindFilter:
    def test_no_kind_param_is_unchanged_includes_both(
        self, auth, owner, staffed_farm, staff_invite, buyer_invite
    ):
        resp = auth(owner).get(_invitations_url(staffed_farm))
        ids = {row["id"] for row in resp.data}
        assert ids == {staff_invite.pk, buyer_invite.pk}

    def test_kind_buyer_returns_only_consumer_invites(
        self, auth, owner, staffed_farm, staff_invite, buyer_invite
    ):
        resp = auth(owner).get(_invitations_url(staffed_farm), {"kind": "buyer"})
        ids = {row["id"] for row in resp.data}
        assert ids == {buyer_invite.pk}

    def test_kind_staff_excludes_consumer_invites(
        self, auth, owner, staffed_farm, staff_invite, buyer_invite
    ):
        resp = auth(owner).get(_invitations_url(staffed_farm), {"kind": "staff"})
        ids = {row["id"] for row in resp.data}
        assert ids == {staff_invite.pk}

    def test_manager_never_sees_buyer_invitations_without_kind(
        self, auth, manager, staffed_farm, staff_invite, buyer_invite
    ):
        resp = auth(manager).get(_invitations_url(staffed_farm))
        ids = {row["id"] for row in resp.data}
        assert ids == {staff_invite.pk}

    def test_manager_never_sees_buyer_invitations_even_with_kind_buyer(
        self, auth, manager, staffed_farm, staff_invite, buyer_invite
    ):
        resp = auth(manager).get(_invitations_url(staffed_farm), {"kind": "buyer"})
        assert resp.data == []

    def test_read_serializer_includes_business_name_and_account_role(
        self, auth, owner, staffed_farm, buyer_invite
    ):
        buyer_invite.business_name = "FreshMart"
        buyer_invite.save(update_fields=["business_name"])
        resp = auth(owner).get(_invitations_url(staffed_farm), {"kind": "buyer"})
        row = resp.data[0]
        assert row["business_name"] == "FreshMart"
        assert row["account_role"] == "CONSUMER"
        assert "pin" not in row
        assert "token" not in row

    def test_overdue_pending_invitation_reads_expired(
        self, auth, owner, staffed_farm, buyer_invite
    ):
        from django.utils import timezone

        buyer_invite.expires_at = timezone.now() - timezone.timedelta(days=1)
        buyer_invite.save(update_fields=["expires_at"])
        resp = auth(owner).get(_invitations_url(staffed_farm), {"kind": "buyer"})
        assert resp.data[0]["status"] == "EXPIRED"


class TestRevokeBuyerInvitation:
    def test_owner_can_revoke_a_buyer_invitation(
        self, auth, owner, staffed_farm, buyer_invite
    ):
        resp = auth(owner).post(_revoke_url(staffed_farm, buyer_invite.pk))
        assert resp.status_code == 200, resp.data
        buyer_invite.refresh_from_db()
        assert buyer_invite.status == Invitation.Status.REVOKED

    def test_manager_cannot_revoke_a_buyer_invitation(
        self, auth, manager, staffed_farm, buyer_invite
    ):
        resp = auth(manager).post(_revoke_url(staffed_farm, buyer_invite.pk))
        assert resp.status_code == 403
        buyer_invite.refresh_from_db()
        assert buyer_invite.status == Invitation.Status.PENDING

    def test_manager_can_still_revoke_a_worker_invitation(
        self, auth, manager, staffed_farm, staff_invite
    ):
        resp = auth(manager).post(_revoke_url(staffed_farm, staff_invite.pk))
        assert resp.status_code == 200, resp.data
