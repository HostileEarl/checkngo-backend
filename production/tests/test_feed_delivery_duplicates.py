# production/tests/test_feed_delivery_duplicates.py
"""
Regression coverage for the double-feed-delivery bug.

Root cause: FeedDelivery inherits a client-generated UUID primary key from
OfflineSyncModel, but FeedDeliverySerializer declared the id field the same
way ModelSerializer infers it by default — which forces it read_only
because the model field is editable=False. A retried POST (weak signal:
the server saved the delivery, the reply never reached the device, the
worker/manager taps again) therefore always minted a fresh id and created a
second FeedDelivery, plus a second mirrored InventoryStockIn via
record_feed_delivery.

Three independent defences, each covered here:

  1. Idempotent create — a client-supplied `id` replayed for the same farm
     returns the existing delivery (200, nothing written); another farm's
     id is a 404; no id still works as before.
  2. Exact-repeat refusal — a *different* id but the same invoice_ref,
     item/feed_type, delivery_date and quantity_kg as an existing
     non-voided delivery is refused outright (400), since a client that
     lost its own id before retrying would otherwise sail straight past
     defence 1.
  3. Void — the audited way to undo a duplicate that already landed.
     Nothing is deleted; every balance/cost figure that fed off it drops
     by the voided amount.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from production.management.commands.check_feed_ledger import Command as CheckFeedLedger
from production.models import FeedDelivery, InventoryItem, InventoryStockIn

pytestmark = pytest.mark.django_db


@pytest.fixture
def feed_item(staffed_farm, owner):
    return InventoryItem.objects.create(
        farm=staffed_farm,
        name="Broiler feed",
        unit="sack",
        kg_per_unit=Decimal("50"),
        is_feed=True,
        low_stock_threshold=Decimal("5"),
        created_by=owner,
    )


def _list_url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/"


def _with_stock_url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/with-stock/"


def _void_url(farm, delivery):
    return f"/api/farms/{farm.id}/feed-deliveries/{delivery.id}/void/"


def _payload(**overrides):
    body = {
        "delivery_date": date(2026, 2, 1).isoformat(),
        "feed_type": FeedDelivery.FeedType.STARTER,
        "quantity_kg": "500.00",
        "unit_cost": "30.00",
        "invoice_ref": "INV-100",
    }
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────
# 1. Idempotent create
# ─────────────────────────────────────────────────────────────


class TestIdempotentCreate:
    def test_supplied_id_is_stored_exactly(self, auth, owner, staffed_farm):
        rid = str(uuid.uuid4())
        response = auth(owner).post(
            _list_url(staffed_farm), _payload(id=rid), format="json"
        )
        assert response.status_code == 201, response.data
        assert response.data["id"] == rid
        assert FeedDelivery.objects.filter(pk=rid).exists()

    def test_posting_same_id_twice_creates_only_one_delivery_and_stock_in(
        self, auth, owner, staffed_farm, feed_item
    ):
        rid = str(uuid.uuid4())
        payload = _payload(id=rid, inventory_item=feed_item.id)

        first = auth(owner).post(_with_stock_url(staffed_farm), payload, format="json")
        assert first.status_code == 201, first.data

        second = auth(owner).post(_with_stock_url(staffed_farm), payload, format="json")
        assert second.status_code == 200, second.data
        assert second.data["feed_delivery"]["id"] == rid

        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1
        assert InventoryStockIn.objects.filter(item=feed_item).count() == 1

    def test_posting_same_id_twice_on_plain_create_also_dedupes(
        self, auth, owner, staffed_farm
    ):
        rid = str(uuid.uuid4())
        payload = _payload(id=rid)

        first = auth(owner).post(_list_url(staffed_farm), payload, format="json")
        assert first.status_code == 201, first.data

        second = auth(owner).post(_list_url(staffed_farm), payload, format="json")
        assert second.status_code == 200, second.data
        assert second.data["id"] == rid
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1

    def test_another_farms_id_is_404_and_writes_nothing(
        self, auth, owner, staffed_farm, rival_farm, rival_owner
    ):
        theirs = auth(rival_owner).post(
            _list_url(rival_farm), _payload(), format="json"
        )
        assert theirs.status_code == 201, theirs.data
        their_id = theirs.data["id"]

        response = auth(owner).post(
            _list_url(staffed_farm),
            _payload(id=their_id, invoice_ref="INV-999"),
            format="json",
        )
        assert response.status_code == 404
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 0

    def test_posting_without_id_still_works_and_generates_one(
        self, auth, owner, staffed_farm
    ):
        response = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        assert response.status_code == 201, response.data
        assert response.data["id"]
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1


# ─────────────────────────────────────────────────────────────
# 2. Exact-repeat refusal
# ─────────────────────────────────────────────────────────────


class TestExactRepeatRefusal:
    def test_exact_repeat_without_an_id_is_rejected(self, auth, owner, staffed_farm):
        first = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        assert second.status_code == 400, second.data
        assert "2026-02-01" in str(second.data)
        assert owner.full_name in str(second.data)
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 1

    def test_same_invoice_different_quantity_is_allowed(
        self, auth, owner, staffed_farm
    ):
        first = auth(owner).post(
            _list_url(staffed_farm), _payload(quantity_kg="500.00"), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            _list_url(staffed_farm), _payload(quantity_kg="600.00"), format="json"
        )
        assert second.status_code == 201, second.data
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 2

    def test_invoice_ref_case_and_whitespace_insensitive(
        self, auth, owner, staffed_farm
    ):
        first = auth(owner).post(
            _list_url(staffed_farm), _payload(invoice_ref="inv-100"), format="json"
        )
        assert first.status_code == 201, first.data

        second = auth(owner).post(
            _list_url(staffed_farm), _payload(invoice_ref="  INV-100  "), format="json"
        )
        assert second.status_code == 400, second.data

    def test_voided_delivery_does_not_block_a_fresh_repeat(
        self, auth, owner, staffed_farm
    ):
        first = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        assert first.status_code == 201, first.data
        delivery = FeedDelivery.objects.get(pk=first.data["id"])

        void = auth(owner).post(
            _void_url(staffed_farm, delivery),
            {"reason": "Entered twice on a weak connection."},
            format="json",
        )
        assert void.status_code == 200, void.data

        second = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        assert second.status_code == 201, second.data
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 2


# ─────────────────────────────────────────────────────────────
# 3. Void
# ─────────────────────────────────────────────────────────────


class TestVoid:
    def test_void_reason_too_short_is_rejected(self, auth, owner, staffed_farm):
        created = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        delivery = FeedDelivery.objects.get(pk=created.data["id"])

        response = auth(owner).post(
            _void_url(staffed_farm, delivery), {"reason": "too short"}, format="json"
        )
        assert response.status_code == 400
        assert "reason" in response.data
        delivery.refresh_from_db()
        assert delivery.voided_at is None

    def test_voiding_twice_is_rejected(self, auth, owner, staffed_farm):
        created = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        delivery = FeedDelivery.objects.get(pk=created.data["id"])
        reason = "Duplicate entry from a dropped connection."

        first = auth(owner).post(
            _void_url(staffed_farm, delivery), {"reason": reason}, format="json"
        )
        assert first.status_code == 200, first.data

        second = auth(owner).post(
            _void_url(staffed_farm, delivery), {"reason": reason}, format="json"
        )
        assert second.status_code == 400
        assert "already" in str(second.data).lower()

    def test_worker_gets_403(self, auth, worker, owner, staffed_farm):
        created = auth(owner).post(
            _list_url(staffed_farm), _payload(), format="json"
        )
        delivery = FeedDelivery.objects.get(pk=created.data["id"])

        response = auth(worker).post(
            _void_url(staffed_farm, delivery),
            {"reason": "Duplicate entry, recorded by mistake twice."},
            format="json",
        )
        assert response.status_code == 403
        delivery.refresh_from_db()
        assert delivery.voided_at is None

    def test_void_drops_feed_balance_cost_margin_inventory_and_low_stock(
        self, auth, owner, staffed_farm, feed_item
    ):
        from analytics.services import farm_feed_cost_per_kg, feed_balance

        good = auth(owner).post(
            _with_stock_url(staffed_farm),
            _payload(
                inventory_item=feed_item.id,
                quantity_kg="500.00",
                unit_cost="30.00",
                invoice_ref="INV-GOOD",
            ),
            format="json",
        )
        assert good.status_code == 201, good.data

        duplicate = auth(owner).post(
            _with_stock_url(staffed_farm),
            _payload(
                inventory_item=feed_item.id,
                quantity_kg="500.00",
                unit_cost="30.00",
                invoice_ref="INV-DUPLICATE",
            ),
            format="json",
        )
        assert duplicate.status_code == 201, duplicate.data
        dup_delivery = FeedDelivery.objects.get(pk=duplicate.data["feed_delivery"]["id"])

        before = feed_balance(staffed_farm)
        before_cost = farm_feed_cost_per_kg(staffed_farm)
        feed_item.refresh_from_db()
        before_qty = feed_item.current_quantity
        before_low = InventoryItem.objects.filter(farm=staffed_farm).low_stock()

        void = auth(owner).post(
            _void_url(staffed_farm, dup_delivery),
            {"reason": "Same invoice recorded twice by accident."},
            format="json",
        )
        assert void.status_code == 200, void.data
        assert void.data["stock_in_needs_attention"] is False
        assert void.data["inventory_stock_in"]["voided_at"] is not None

        after = feed_balance(staffed_farm)
        assert after["delivered_kg"] == before["delivered_kg"] - Decimal("500.00")
        assert after["total_feed_cost"] == before["total_feed_cost"] - Decimal("15000.00")

        after_cost = farm_feed_cost_per_kg(staffed_farm)
        assert after_cost == Decimal("30.00")  # unchanged: both deliveries were 30/kg
        assert before_cost == Decimal("30.00")

        feed_item.refresh_from_db()
        after_qty = feed_item.current_quantity
        assert after_qty == before_qty - Decimal("10.00")  # 500kg / 50kg-per-sack

        dup_delivery.refresh_from_db()
        assert dup_delivery.voided_at is not None
        assert FeedDelivery.objects.filter(farm=staffed_farm).count() == 2
        assert InventoryStockIn.objects.filter(item=feed_item).count() == 2

    def test_old_delivery_without_fk_link_is_still_voided_correctly(
        self, auth, owner, staffed_farm, feed_item
    ):
        """
        Simulates a pre-feature row: a FeedDelivery whose mirrored
        InventoryStockIn has no feed_delivery FK set (as if written before
        that column existed). The void endpoint must fall back to matching
        on item/date/quantity/note.
        """
        delivery = FeedDelivery.objects.create(
            farm=staffed_farm,
            recorded_by=owner,
            inventory_item=feed_item,
            delivery_date=date(2026, 1, 15),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("250.00"),
            unit_cost=Decimal("30.00"),
            invoice_ref="OLD-INV-1",
        )
        stock_in = InventoryStockIn.objects.create(
            item=feed_item,
            quantity=Decimal("5.00"),  # 250kg / 50kg per sack
            stock_in_date=date(2026, 1, 15),
            note="Feed delivery — OLD-INV-1",
            recorded_by=owner,
            feed_delivery=None,  # not linked, as an old row would be
        )

        response = auth(owner).post(
            _void_url(staffed_farm, delivery),
            {"reason": "Duplicate pre-feature delivery, voided manually."},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["stock_in_needs_attention"] is False

        stock_in.refresh_from_db()
        assert stock_in.voided_at is not None
        delivery.refresh_from_db()
        assert delivery.voided_at is not None

    def test_ambiguous_legacy_match_is_flagged_not_guessed(
        self, auth, owner, staffed_farm, feed_item
    ):
        delivery = FeedDelivery.objects.create(
            farm=staffed_farm,
            recorded_by=owner,
            inventory_item=feed_item,
            delivery_date=date(2026, 1, 20),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("100.00"),
            unit_cost=Decimal("30.00"),
            invoice_ref="AMBIGUOUS",
        )
        # No stock-in at all for this one — zero matches.
        response = auth(owner).post(
            _void_url(staffed_farm, delivery),
            {"reason": "Voiding with no matching stock-in on file."},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["stock_in_needs_attention"] is True
        assert response.data["inventory_stock_in"] is None
        delivery.refresh_from_db()
        assert delivery.voided_at is not None


def test_check_feed_ledger_ignores_voided_pairs(auth, owner, staffed_farm, feed_item, capsys):
    good = auth(owner).post(
        f"/api/farms/{staffed_farm.id}/feed-deliveries/with-stock/",
        _payload(inventory_item=feed_item.id, invoice_ref="LEDGER-GOOD"),
        format="json",
    )
    assert good.status_code == 201, good.data

    duplicate = auth(owner).post(
        f"/api/farms/{staffed_farm.id}/feed-deliveries/with-stock/",
        _payload(inventory_item=feed_item.id, invoice_ref="LEDGER-DUP"),
        format="json",
    )
    assert duplicate.status_code == 201, duplicate.data
    dup_delivery = FeedDelivery.objects.get(pk=duplicate.data["feed_delivery"]["id"])

    void_response = auth(owner).post(
        f"/api/farms/{staffed_farm.id}/feed-deliveries/{dup_delivery.id}/void/",
        {"reason": "Duplicate recorded on a weak connection."},
        format="json",
    )
    assert void_response.status_code == 200, void_response.data

    command = CheckFeedLedger()
    command.stdout = _NullStream()
    command.stderr = _NullStream()
    command.style = _NullStyle()
    try:
        command.handle(farm=None)
    except SystemExit as exc:
        pytest.fail(f"check_feed_ledger reported a violation after voiding: {exc}")


class _NullStream:
    def write(self, *args, **kwargs):
        pass


class _NullStyle:
    def __getattr__(self, name):
        return lambda x: x
