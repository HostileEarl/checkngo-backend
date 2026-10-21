# accounts/tests/test_invitation_expiry.py
"""
Invitations past expires_at become EXPIRED.

Nothing used to write EXPIRED, so an overdue row stayed PENDING: the admin
showed "Pending" forever, and re-inviting the same phone to the same farm
tripped unique_pending_invite_per_phone_farm and returned a 500.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import Invitation, User

pytestmark = pytest.mark.django_db

PAST = timedelta(days=1)


def _invitation(farm, phone, *, status=Invitation.Status.PENDING, expires_in=timedelta(days=7)):
    return Invitation.objects.create(
        phone_number=phone,
        full_name="Someone",
        farm=farm,
        account_role=User.Role.WORKER,
        membership_role="WORKER",
        pin_hash="unused",
        status=status,
        expires_at=timezone.now() + expires_in,
    )


def _make_overdue(invitation_id):
    Invitation.objects.filter(pk=invitation_id).update(expires_at=timezone.now() - PAST)


class TestReinviteAfterExpiry:
    def test_staff_reinvite_after_expiry_succeeds_and_expires_the_old_row(
        self, auth, owner, staffed_farm
    ):
        client = auth(owner)
        url = reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk})
        payload = {
            "phone_number": "+639170000301",
            "full_name": "Late Joiner",
            "account_role": "WORKER",
            "membership_role": "WORKER",
        }
        first = client.post(url, payload, format="json")
        assert first.status_code == 201, first.data
        _make_overdue(first.data["id"])

        again = client.post(url, payload, format="json")

        assert again.status_code == 201, again.data
        assert Invitation.objects.get(pk=first.data["id"]).status == Invitation.Status.EXPIRED
        assert Invitation.objects.get(pk=again.data["id"]).status == Invitation.Status.PENDING

    def test_a_still_valid_pending_invite_still_blocks_a_reinvite(
        self, auth, owner, staffed_farm
    ):
        client = auth(owner)
        url = reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk})
        payload = {
            "phone_number": "+639170000302",
            "full_name": "Twice",
            "account_role": "WORKER",
            "membership_role": "WORKER",
        }
        first = client.post(url, payload, format="json")
        assert first.status_code == 201

        assert client.post(url, payload, format="json").status_code == 400
        assert Invitation.objects.get(pk=first.data["id"]).status == Invitation.Status.PENDING

    def test_reinvite_leaves_other_farms_overdue_invites_alone(
        self, auth, owner, staffed_farm, rival_farm
    ):
        phone = "+639170000303"
        elsewhere = _invitation(rival_farm, phone, expires_in=-PAST)

        response = auth(owner).post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": phone,
                "full_name": "Two Farms",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )

        assert response.status_code == 201
        elsewhere.refresh_from_db()
        assert elsewhere.status == Invitation.Status.PENDING  # the daily job's to sweep

    def test_buyer_reinvite_after_expiry_succeeds_and_expires_the_old_row(
        self, auth, owner, staffed_farm
    ):
        client = auth(owner)
        url = f"/api/farms/{staffed_farm.id}/partners/"
        payload = {
            "phone_number": "+639170000304",
            "full_name": "Late Buyer",
            "account_role": "CONSUMER",
        }
        first = client.post(url, payload, format="json")
        assert first.status_code == 201, first.data
        old = Invitation.objects.get(phone_number=payload["phone_number"])
        _make_overdue(old.pk)

        again = client.post(url, payload, format="json")

        assert again.status_code == 201, again.data
        old.refresh_from_db()
        assert old.status == Invitation.Status.EXPIRED
        assert Invitation.objects.filter(
            phone_number=payload["phone_number"], status=Invitation.Status.PENDING
        ).count() == 1


class TestAcceptExpired:
    def test_accepting_an_overdue_invite_fails_and_marks_it_expired(
        self, api, auth, owner, staffed_farm
    ):
        created = auth(owner).post(
            reverse("farms:farm-invitations", kwargs={"farm_pk": staffed_farm.pk}),
            {
                "phone_number": "+639170000311",
                "full_name": "Too Late",
                "account_role": "WORKER",
                "membership_role": "WORKER",
            },
            format="json",
        )
        invitation = Invitation.objects.get(pk=created.data["id"])
        _make_overdue(invitation.pk)

        api.force_authenticate(user=None)
        response = api.post(
            reverse("accounts:invitation-accept"),
            {"token": invitation.token, "pin": created.data["pin"]},
            format="json",
        )

        assert response.status_code == 400
        invitation.refresh_from_db()
        assert invitation.status == Invitation.Status.EXPIRED
        assert not User.objects.filter(phone_number="+639170000311").exists()

    def test_a_revoked_invite_is_refused_and_stays_revoked(self, api, farm):
        invitation = _invitation(
            farm, "+639170000312", status=Invitation.Status.REVOKED, expires_in=-PAST
        )

        response = api.post(
            reverse("accounts:invitation-accept"),
            {"token": invitation.token, "pin": "123456"},
            format="json",
        )

        assert response.status_code == 400
        invitation.refresh_from_db()
        assert invitation.status == Invitation.Status.REVOKED


class TestExpireInvitationsCommand:
    def test_expires_only_overdue_pending_rows(self, farm, rival_farm):
        overdue = _invitation(farm, "+639170000321", expires_in=-PAST)
        overdue_elsewhere = _invitation(rival_farm, "+639170000322", expires_in=-PAST)
        still_valid = _invitation(farm, "+639170000323")
        accepted = _invitation(
            farm, "+639170000324", status=Invitation.Status.ACCEPTED, expires_in=-PAST
        )
        revoked = _invitation(
            farm, "+639170000325", status=Invitation.Status.REVOKED, expires_in=-PAST
        )

        out = StringIO()
        call_command("expire_invitations", stdout=out)

        assert "Expired 2 invitation(s)." in out.getvalue()
        status = lambda inv: Invitation.objects.get(pk=inv.pk).status  # noqa: E731
        assert status(overdue) == Invitation.Status.EXPIRED
        assert status(overdue_elsewhere) == Invitation.Status.EXPIRED
        assert status(still_valid) == Invitation.Status.PENDING
        assert status(accepted) == Invitation.Status.ACCEPTED
        assert status(revoked) == Invitation.Status.REVOKED

    def test_a_second_run_expires_nothing(self, farm):
        _invitation(farm, "+639170000326", expires_in=-PAST)
        call_command("expire_invitations", stdout=StringIO())

        out = StringIO()
        call_command("expire_invitations", stdout=out)

        assert "Expired 0 invitation(s)." in out.getvalue()

    def test_expire_overdue_returns_the_count(self, farm):
        _invitation(farm, "+639170000327", expires_in=-PAST)
        _invitation(farm, "+639170000328")

        assert Invitation.objects.expire_overdue() == 1


# ── admin ────────────────────────────────────────────────────────────────


@pytest.fixture
def admin_client(client, settings):
    """Superuser session; plain static storage so admin pages render under
    pytest-django's DEBUG=False (see test_owner_issuance)."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
    cache.clear()
    superuser = User.objects.create_superuser(
        phone_number="+639179000010",
        password="ops-secret-99",
        full_name="Platform Ops",
    )
    client.force_login(superuser)
    yield client
    cache.clear()


CHANGELIST = "admin:accounts_invitation_changelist"


class TestInvitationAdminStatus:
    def test_changelist_shows_expired_for_an_overdue_pending_invite(self, admin_client, farm):
        invitation = _invitation(farm, "+639170000331", expires_in=-PAST)
        invitation.full_name = "Overdue Person"
        invitation.save(update_fields=["full_name"])

        html = admin_client.get(reverse(CHANGELIST)).content.decode()

        assert '<span class="status-muted">Expired</span>' in html
        assert '<span class="status-warning">Pending</span>' not in html
        # The stored row is untouched: the admin is read-only.
        assert Invitation.objects.get(pk=invitation.pk).status == Invitation.Status.PENDING

    def test_changelist_still_shows_pending_for_a_valid_invite(self, admin_client, farm):
        _invitation(farm, "+639170000332")

        html = admin_client.get(reverse(CHANGELIST)).content.decode()

        assert '<span class="status-warning">Pending</span>' in html
        assert "status-muted" not in html

    def test_status_filter_treats_overdue_pending_as_expired(self, admin_client, farm):
        overdue = _invitation(farm, "+639170000333", expires_in=-PAST)
        stored_expired = _invitation(
            farm, "+639170000334", status=Invitation.Status.EXPIRED, expires_in=-PAST
        )
        valid = _invitation(farm, "+639170000335")

        def listed(status):
            response = admin_client.get(reverse(CHANGELIST), {"status": status})
            assert response.status_code == 200
            return {obj.pk for obj in response.context["cl"].result_list}

        assert listed("EXPIRED") == {overdue.pk, stored_expired.pk}
        assert listed("PENDING") == {valid.pk}
