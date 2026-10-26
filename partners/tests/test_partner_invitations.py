# partners/tests/test_partner_invitations.py
"""
POST /api/farms/<farm_pk>/partners/ — inviting a partner.

There is no SUPPLIER partner type any more; this endpoint issues consumer
invitations only.
"""
from unittest.mock import patch

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


def test_buyer_invite_sms_carries_the_pin_without_a_link(
    auth, owner, staffed_farm, settings
):
    """
    Philippine carriers silently filter SMS containing links, so the
    buyer invitation SMS — same send_invitation_pin_sms used for staff
    invites — must carry only the farm name and PIN, no link.
    """
    settings.SMS_ENABLED = True
    settings.SEMAPHORE_API_KEY = "test-key"

    with patch("notifications.sms.requests.post") as mock_post:
        mock_post.return_value.json.return_value = [{"message_id": "1"}]
        resp = auth(owner).post(
            _url(staffed_farm),
            {
                "phone_number": "+639179998889",
                "full_name": "Link Free Buyer",
                "account_role": "CONSUMER",
            },
            format="json",
        )

    assert resp.status_code == 201, resp.data
    sent_message = mock_post.call_args.kwargs["data"]["message"]
    assert "http" not in sent_message
    assert "/i/" not in sent_message
    assert resp.data["pin"] in sent_message
