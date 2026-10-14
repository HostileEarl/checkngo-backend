# partners/tests/test_partner_invitations.py
"""
POST /api/farms/<farm_pk>/partners/ — inviting a partner.

There is no SUPPLIER partner type any more; this endpoint issues consumer
invitations only.
"""
import pytest

pytestmark = pytest.mark.django_db


def _url(farm):
    return f"/api/farms/{farm.id}/partners/"


def test_inviting_a_supplier_account_role_is_rejected(auth, owner, staffed_farm):
    resp = auth(owner).post(
        _url(staffed_farm),
        {
            "phone_number": "+639179998888",
            "full_name": "Some Supplier",
            "account_role": "SUPPLIER",
        },
        format="json",
    )
    assert resp.status_code == 400


def test_inviting_a_consumer_account_role_succeeds(auth, owner, staffed_farm):
    resp = auth(owner).post(
        _url(staffed_farm),
        {
            "phone_number": "+639179998888",
            "full_name": "Some Buyer",
            "account_role": "CONSUMER",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.data
