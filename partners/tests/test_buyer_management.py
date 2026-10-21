# partners/tests/test_buyer_management.py
"""
Buyer invitations and buyer links (FarmPartnerLink) are owner-only to
create, archive (unlink), and restore. A manager keeps reading the
partner directory but cannot act on it — see partners.permissions
.IsFarmOwnerForBuyerManagement.
"""
import pytest

from partners.models import FarmPartnerLink

pytestmark = pytest.mark.django_db


def _partners_url(farm):
    return f"/api/farms/{farm.id}/partners/"


def _unlink_url(farm, pk):
    return f"/api/farms/{farm.id}/partners/{pk}/unlink/"


def _restore_url(farm, pk):
    return f"/api/farms/{farm.id}/partners/{pk}/restore/"


@pytest.fixture
def link(db, staffed_farm, consumer, owner):
    return FarmPartnerLink.objects.create(
        farm=staffed_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=owner,
    )


@pytest.fixture
def rival_link(db, rival_farm, consumer, rival_owner):
    return FarmPartnerLink.objects.create(
        farm=rival_farm,
        partner=consumer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        linked_by=rival_owner,
    )


class TestCreateBuyerInvitation:
    def test_owner_can_invite_a_buyer(self, auth, owner, staffed_farm):
        resp = auth(owner).post(
            _partners_url(staffed_farm),
            {
                "phone_number": "+639179998888",
                "full_name": "Some Buyer",
                "account_role": "CONSUMER",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data

    def test_manager_cannot_invite_a_buyer(self, auth, manager, staffed_farm):
        resp = auth(manager).post(
            _partners_url(staffed_farm),
            {
                "phone_number": "+639179998888",
                "full_name": "Some Buyer",
                "account_role": "CONSUMER",
            },
            format="json",
        )
        assert resp.status_code == 403

    def test_manager_can_still_list_the_partner_directory(
        self, auth, manager, staffed_farm, link
    ):
        resp = auth(manager).get(_partners_url(staffed_farm))
        assert resp.status_code == 200
        assert len(resp.data) == 1


class TestUnlinkBuyer:
    def test_owner_can_unlink(self, auth, owner, staffed_farm, link):
        resp = auth(owner).post(_unlink_url(staffed_farm, link.pk))
        assert resp.status_code == 200, resp.data
        link.refresh_from_db()
        assert link.is_active is False

    def test_manager_cannot_unlink(self, auth, manager, staffed_farm, link):
        resp = auth(manager).post(_unlink_url(staffed_farm, link.pk))
        assert resp.status_code == 403
        link.refresh_from_db()
        assert link.is_active is True


class TestRestoreBuyer:
    def test_owner_can_restore_an_archived_link(self, auth, owner, staffed_farm, link):
        link.deactivate()
        resp = auth(owner).post(_restore_url(staffed_farm, link.pk))
        assert resp.status_code == 200, resp.data
        link.refresh_from_db()
        assert link.is_active is True
        assert link.deactivated_at is None

    def test_manager_cannot_restore(self, auth, manager, staffed_farm, link):
        link.deactivate()
        resp = auth(manager).post(_restore_url(staffed_farm, link.pk))
        assert resp.status_code == 403

    def test_restoring_an_already_active_link_is_400(
        self, auth, owner, staffed_farm, link
    ):
        resp = auth(owner).post(_restore_url(staffed_farm, link.pk))
        assert resp.status_code == 400

    def test_restoring_another_farms_link_is_404(
        self, auth, owner, staffed_farm, rival_link
    ):
        rival_link.deactivate()
        resp = auth(owner).post(_restore_url(staffed_farm, rival_link.pk))
        assert resp.status_code == 404


class TestPartnerStatusFilter:
    @pytest.fixture
    def archived_link(self, db, staffed_farm, owner):
        from accounts.models import User

        buyer_b = User.objects.create_user(
            "+639170001111",
            "000111",
            full_name="Archived Buyer",
            role=User.Role.CONSUMER,
            must_change_credential=False,
        )
        archived = FarmPartnerLink.objects.create(
            farm=staffed_farm,
            partner=buyer_b,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=owner,
        )
        archived.deactivate()
        return archived

    def test_default_returns_active_and_archived(
        self, auth, owner, staffed_farm, link, archived_link
    ):
        resp = auth(owner).get(_partners_url(staffed_farm))
        assert len(resp.data) == 2

    def test_status_active_filters_to_active_only(
        self, auth, owner, staffed_farm, link, archived_link
    ):
        resp = auth(owner).get(_partners_url(staffed_farm), {"status": "active"})
        ids = {row["id"] for row in resp.data}
        assert ids == {link.pk}

    def test_status_archived_filters_to_archived_only(
        self, auth, owner, staffed_farm, link, archived_link
    ):
        resp = auth(owner).get(_partners_url(staffed_farm), {"status": "archived"})
        ids = {row["id"] for row in resp.data}
        assert ids == {archived_link.pk}

    def test_status_all_returns_both(self, auth, owner, staffed_farm, link, archived_link):
        resp = auth(owner).get(_partners_url(staffed_farm), {"status": "all"})
        assert len(resp.data) == 2

    def test_response_rows_carry_is_active_and_deactivated_at(
        self, auth, owner, staffed_farm, archived_link
    ):
        resp = auth(owner).get(_partners_url(staffed_farm), {"status": "archived"})
        row = resp.data[0]
        assert row["is_active"] is False
        assert row["deactivated_at"] is not None
