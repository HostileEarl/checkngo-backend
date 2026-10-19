# production/tests/test_feed_type.py
"""
Part A: feed_type on InventoryItem, and the three-choice rework of
FeedDelivery.FeedType (GROWER -> BOOSTER, OTHER retired to legacy-only).
"""
from datetime import date
from decimal import Decimal

import pytest

from production.catalogue import seed_starter_feed_catalogue
from production.models import FeedDelivery, InventoryItem

pytestmark = pytest.mark.django_db


def _delivery_url(farm):
    return f"/api/farms/{farm.id}/feed-deliveries/"


def _payload(**overrides):
    body = {
        "delivery_date": date(2026, 1, 10).isoformat(),
        "feed_type": FeedDelivery.FeedType.STARTER,
        "quantity_kg": "500.00",
        "unit_cost": "30.00",
    }
    body.update(overrides)
    return body


class TestInventoryItemFeedType:
    def test_a_feed_item_can_carry_a_feed_type(self, auth, owner, staffed_farm):
        resp = auth(owner).post(
            f"/api/farms/{staffed_farm.id}/inventory/items/",
            {
                "name": "Starter Crumble",
                "unit": "sack",
                "kg_per_unit": "50",
                "is_feed": True,
                "feed_type": InventoryItem.FeedType.STARTER,
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["feed_type"] == "STARTER"

    def test_a_non_feed_item_cannot_carry_a_feed_type(self, auth, owner, staffed_farm):
        resp = auth(owner).post(
            f"/api/farms/{staffed_farm.id}/inventory/items/",
            {
                "name": "Disinfectant",
                "unit": "litre",
                "is_feed": False,
                "feed_type": InventoryItem.FeedType.STARTER,
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "feed_type" in resp.data


class TestFeedDeliveryFeedTypeChoices:
    def test_new_delivery_without_item_accepts_the_three_choices(
        self, auth, owner, staffed_farm
    ):
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            _payload(feed_type=FeedDelivery.FeedType.BOOSTER),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["feed_type"] == "BOOSTER"

    def test_new_delivery_without_item_rejects_other(self, auth, owner, staffed_farm):
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            _payload(feed_type=FeedDelivery.FeedType.OTHER),
            format="json",
        )
        assert resp.status_code == 400
        assert "feed_type" in resp.data

    def test_new_delivery_without_item_rejects_grower(self, auth, owner, staffed_farm):
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            _payload(feed_type="GROWER"),
            format="json",
        )
        assert resp.status_code == 400

    def test_delivery_against_an_item_with_a_feed_type_ignores_the_client_value(
        self, auth, owner, staffed_farm
    ):
        item = InventoryItem.objects.create(
            farm=staffed_farm,
            name="Booster Crumble",
            unit="sack",
            kg_per_unit=Decimal("25"),
            is_feed=True,
            feed_type=InventoryItem.FeedType.BOOSTER,
            created_by=owner,
        )
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            _payload(
                inventory_item=item.id,
                feed_type=FeedDelivery.FeedType.FINISHER,  # ignored
                quantity_kg="250.00",
            ),
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert resp.data["feed_type"] == "BOOSTER"

    def test_delivery_against_an_item_with_no_feed_type_still_requires_one_of_the_three(
        self, auth, owner, staffed_farm
    ):
        item = InventoryItem.objects.create(
            farm=staffed_farm,
            name="CBM",
            unit="sack",
            kg_per_unit=Decimal("25"),
            is_feed=True,
            created_by=owner,
        )
        resp = auth(owner).post(
            _delivery_url(staffed_farm),
            _payload(
                inventory_item=item.id,
                feed_type=FeedDelivery.FeedType.OTHER,
                quantity_kg="100.00",
            ),
            format="json",
        )
        assert resp.status_code == 400
        assert "feed_type" in resp.data

    def test_legacy_other_row_reads_back_flagged_legacy(
        self, owner, staffed_farm
    ):
        # Bypasses clean() to represent a row written before this change.
        delivery = FeedDelivery.objects.create(
            farm=staffed_farm,
            delivery_date=date(2025, 1, 1),
            feed_type=FeedDelivery.FeedType.OTHER,
            quantity_kg=Decimal("100.00"),
            recorded_by=owner,
        )
        from production.serializers import FeedDeliverySerializer

        data = FeedDeliverySerializer(delivery).data
        assert data["legacy_feed_type"] is True

    def test_non_legacy_row_is_not_flagged(self, owner, staffed_farm):
        delivery = FeedDelivery.objects.create(
            farm=staffed_farm,
            delivery_date=date(2026, 1, 1),
            feed_type=FeedDelivery.FeedType.STARTER,
            quantity_kg=Decimal("100.00"),
            recorded_by=owner,
        )
        from production.serializers import FeedDeliverySerializer

        assert FeedDeliverySerializer(delivery).data["legacy_feed_type"] is False


class TestGrowerToBoosterDataMigration:
    def test_migration_renames_grower_rows_to_booster_and_leaves_other_alone(
        self, owner, staffed_farm
    ):
        import importlib

        mig = importlib.import_module(
            "production.migrations.0019_grower_to_booster_data"
        )

        grower_row = FeedDelivery.objects.create(
            farm=staffed_farm, delivery_date=date(2025, 6, 1),
            feed_type="GROWER", quantity_kg=Decimal("100.00"), recorded_by=owner,
        )
        other_row = FeedDelivery.objects.create(
            farm=staffed_farm, delivery_date=date(2025, 6, 2),
            feed_type=FeedDelivery.FeedType.OTHER, quantity_kg=Decimal("50.00"),
            recorded_by=owner,
        )

        class _FakeApps:
            @staticmethod
            def get_model(app_label, name):
                assert app_label == "production" and name == "FeedDelivery"
                return FeedDelivery

        mig.grower_to_booster(_FakeApps(), None)

        grower_row.refresh_from_db()
        other_row.refresh_from_db()
        assert grower_row.feed_type == "BOOSTER"
        assert other_row.feed_type == FeedDelivery.FeedType.OTHER

        mig.booster_to_grower(_FakeApps(), None)
        grower_row.refresh_from_db()
        assert grower_row.feed_type == "GROWER"


class TestCatalogueFeedTypes:
    def test_seeded_catalogue_items_carry_a_feed_type(self, farm):
        created = seed_starter_feed_catalogue(farm)
        by_name = {item.name: item.feed_type for item in created}
        assert by_name["Broiler Starter Crumble"] == "STARTER"
        assert by_name["Broiler Finisher Crumble"] == "FINISHER"
        assert by_name["Titanium Chick Booster Crumble"] == "BOOSTER"
        assert by_name["Chick Booster Mash"] == "BOOSTER"
