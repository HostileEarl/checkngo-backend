# partners/tests/test_link_existing_buyer.py
"""
POST /api/farms/<farm_pk>/partners/ for a phone number that already has a
CONSUMER account on another farm (or an archived link on this one).

One phone number is one account, and the model already supports a buyer on
several farms — one FarmPartnerLink per farm. Before this fix, inviting an
existing buyer created a PENDING invitation with no PIN and no token (the
buyer has no accept step to reach it), so the invite could never be
accepted even though the response said "They have been added". This file
proves the fix: the invitation is accepted immediately, server-side, and
the buyer is actually linked.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import Invitation, User
from notifications.models import SmsLog
from partners.models import FarmPartnerLink
from production.models import Batch, House, SaleEvent

pytestmark = pytest.mark.django_db

MY_FARMS = reverse("partners:my-partner-farms")
MY_PURCHASES = reverse("partners:my-purchases")


def _invite_url(farm):
    return f"/api/farms/{farm.id}/partners/"


def _invite(client, farm, phone, full_name="Existing Buyer"):
    return client.post(
        _invite_url(farm),
        {
            "phone_number": phone,
            "full_name": full_name,
            "account_role": "CONSUMER",
        },
        format="json",
    )


def _sale(target_farm, link, *, owner, batch_code):
    house = House.objects.create(farm=target_farm, name=f"House {batch_code}", capacity=10000)
    batch = Batch.objects.create(
        house=house,
        batch_code=batch_code,
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    return SaleEvent.objects.create(
        batch=batch,
        sale_date=date(2026, 2, 1),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=80,
        total_weight_kg=Decimal("160.00"),
        price_per_kg=Decimal("75.00"),
        buyer_link=link,
        recorded_by=owner,
    )


class TestExistingBuyerLinkedImmediately:
    def test_second_farm_links_existing_buyer_immediately(
        self, auth, owner, rival_owner, farm, rival_farm, consumer
    ):
        """Buyer already linked to `farm`; `rival_farm`'s owner invites the
        same number. The buyer must be linked right away, with no PIN, no
        token, and no SMS — there is nothing left for them to accept."""
        FarmPartnerLink.objects.create(
            farm=farm,
            partner=consumer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=owner,
        )

        resp = _invite(
            auth(rival_owner), rival_farm, consumer.phone_number, consumer.full_name
        )

        assert resp.status_code == 201, resp.data
        assert "pin" not in resp.data
        assert "token" not in resp.data
        assert resp.data["existing_user"] == consumer.full_name
        assert "buyer" in resp.data["detail"]
        assert "Consumer" not in resp.data["detail"]
        assert rival_farm.name in resp.data["detail"]

        link = FarmPartnerLink.objects.get(farm=rival_farm, partner=consumer)
        assert link.is_active is True

        invitation = Invitation.objects.get(farm=rival_farm, phone_number=consumer.phone_number)
        assert invitation.status == Invitation.Status.ACCEPTED
        assert invitation.accepted_user == consumer

        assert not SmsLog.objects.exists()

    def test_buyer_now_sees_both_farms_and_both_purchases(
        self, auth, owner, rival_owner, farm, rival_farm, consumer
    ):
        link_a = FarmPartnerLink.objects.create(
            farm=farm,
            partner=consumer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=owner,
        )
        sale_a = _sale(farm, link_a, owner=owner, batch_code="A-001")

        resp = _invite(
            auth(rival_owner), rival_farm, consumer.phone_number, consumer.full_name
        )
        assert resp.status_code == 201, resp.data

        link_b = FarmPartnerLink.objects.get(farm=rival_farm, partner=consumer)
        sale_b = _sale(rival_farm, link_b, owner=rival_owner, batch_code="B-001")

        client = auth(consumer)
        farms_resp = client.get(MY_FARMS)
        assert farms_resp.status_code == 200
        assert {row["farm"] for row in farms_resp.data} == {farm.pk, rival_farm.pk}

        purchases_resp = client.get(MY_PURCHASES)
        assert purchases_resp.status_code == 200
        by_farm = {row["farm"]: row for row in purchases_resp.data}
        assert set(by_farm) == {farm.pk, rival_farm.pk}
        assert by_farm[farm.pk]["farm_name"] == farm.name
        assert by_farm[rival_farm.pk]["farm_name"] == rival_farm.name

        for row in purchases_resp.data:
            assert "price_per_kg" not in row
            assert "total" not in row
            assert "revenue" not in row

    def test_archived_link_on_the_inviting_farm_is_reactivated(
        self, auth, rival_owner, rival_farm, consumer
    ):
        archived = FarmPartnerLink.objects.create(
            farm=rival_farm,
            partner=consumer,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            linked_by=rival_owner,
        )
        archived.deactivate()
        assert archived.is_active is False

        resp = _invite(
            auth(rival_owner), rival_farm, consumer.phone_number, consumer.full_name
        )
        assert resp.status_code == 201, resp.data

        archived.refresh_from_db()
        assert archived.is_active is True

    def test_deactivated_buyer_account_is_rejected(
        self, auth, rival_owner, rival_farm, consumer
    ):
        consumer.is_active = False
        consumer.save(update_fields=["is_active"])

        resp = _invite(
            auth(rival_owner), rival_farm, consumer.phone_number, consumer.full_name
        )

        assert resp.status_code == 400
        assert not Invitation.objects.filter(
            farm=rival_farm, phone_number=consumer.phone_number
        ).exists()
        assert not FarmPartnerLink.objects.filter(farm=rival_farm, partner=consumer).exists()

    def test_staff_number_is_still_rejected(self, auth, rival_owner, rival_farm, worker):
        resp = _invite(auth(rival_owner), rival_farm, worker.phone_number, worker.full_name)

        assert resp.status_code == 400
        assert not Invitation.objects.filter(
            farm=rival_farm, phone_number=worker.phone_number
        ).exists()
