# farms/tests/test_feed_catalogue.py
"""
The starter LIMCOMA feed catalogue (production/catalogue.py), applied to a
farm on request — via `manage.py seed_feed_catalogue` — rather than
automatically on farm creation.

Why not automatic: FeedDelivery.clean() requires an inventory_item to be
named as soon as a farm HAS any active feed item at all. A brand-new farm
with none yet is a real, intentional state (its first delivery can skip
item selection). Auto-seeding on signup would have put every new farm past
that point before its owner ever chose to track feed by item — the
opposite of "a starting catalogue a farm can use." So this only runs when
something (support, onboarding) explicitly asks for it, farm by farm.
"""
from decimal import Decimal

import pytest

from production.catalogue import seed_starter_feed_catalogue
from production.models import InventoryItem

pytestmark = pytest.mark.django_db


class TestStarterCatalogue:
    def test_a_fresh_farm_has_no_feed_items_until_asked_for(self, farm):
        assert not InventoryItem.objects.filter(farm=farm, is_feed=True).exists()

    def test_seeding_applies_the_catalogue_with_correct_sack_weights(self, farm):
        seed_starter_feed_catalogue(farm)

        items = {i.name: i for i in InventoryItem.objects.filter(farm=farm, is_feed=True)}
        assert set(items) == {
            "Titanium Chick Booster Crumble",
            "Broiler Starter Crumble",
            "Broiler Finisher Crumble",
            "Chick Booster Mash",
        }
        assert items["Titanium Chick Booster Crumble"].kg_per_unit == Decimal("25")
        assert items["Broiler Starter Crumble"].kg_per_unit == Decimal("50")
        assert items["Broiler Finisher Crumble"].kg_per_unit == Decimal("50")

    def test_cbm_is_seeded_without_a_guessed_sack_weight(self, farm):
        seed_starter_feed_catalogue(farm)

        cbm = InventoryItem.objects.get(farm=farm, name="Chick Booster Mash")
        assert cbm.is_feed is True
        assert cbm.kg_per_unit is None

    def test_reseeding_does_not_duplicate_items(self, farm):
        seed_starter_feed_catalogue(farm)
        before = InventoryItem.objects.filter(farm=farm, is_feed=True).count()
        seed_starter_feed_catalogue(farm)
        after = InventoryItem.objects.filter(farm=farm, is_feed=True).count()
        assert before == after == 4

    def test_management_command_seeds_one_farm(self, farm):
        from django.core.management import call_command

        call_command("seed_feed_catalogue", farm=farm.pk)
        assert InventoryItem.objects.filter(farm=farm, is_feed=True).count() == 4

    def test_management_command_all_does_not_touch_other_farms(self, farm, rival_farm):
        from django.core.management import call_command

        call_command("seed_feed_catalogue", all=True)
        assert InventoryItem.objects.filter(farm=farm, is_feed=True).count() == 4
        assert InventoryItem.objects.filter(farm=rival_farm, is_feed=True).count() == 4

    def test_catalogue_is_not_a_locked_list(self, auth, owner, farm):
        """A farm can add its own feed item alongside the starter catalogue."""
        seed_starter_feed_catalogue(farm)
        resp = auth(owner).post(
            f"/api/farms/{farm.id}/inventory/items/",
            {
                "name": "Local millrun feed",
                "unit": "sack",
                "kg_per_unit": "40.00",
                "is_feed": True,
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert InventoryItem.objects.filter(
            farm=farm, name="Local millrun feed", kg_per_unit=Decimal("40")
        ).exists()

    def test_a_catalogue_item_sack_weight_can_be_changed(self, auth, owner, farm):
        """A different supplier or a repackaged product can change the weight."""
        seed_starter_feed_catalogue(farm)
        item = InventoryItem.objects.get(farm=farm, name="Broiler Starter Crumble")
        resp = auth(owner).patch(
            f"/api/farms/{farm.id}/inventory/items/{item.id}/",
            {"kg_per_unit": "25.00"},
            format="json",
        )
        assert resp.status_code == 200, resp.data
        item.refresh_from_db()
        assert item.kg_per_unit == Decimal("25")
