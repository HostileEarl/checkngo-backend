# production/catalogue.py
"""
The LIMCOMA starter feed catalogue.

Deliberately NOT wired to Farm creation. A brand-new farm with zero feed
items is a real, load-bearing state — FeedDelivery.clean() only demands an
inventory_item once the farm *has* active feed items (see
InventoryItem.clean / FeedDelivery.clean), so seeding this automatically on
every signup would force item selection on a farm's very first delivery
before it has decided what it tracks. That is the opposite of "a starting
catalogue a farm can use."

Instead this is applied on request — see
production/management/commands/seed_feed_catalogue.py — to a specific farm,
once someone (support, onboarding) decides that farm wants it. A farm can
still add its own items or edit any sack weight afterward; nothing here is
a locked list.
"""
from decimal import Decimal

# CBM (Chick Booster Mash) is seeded without a kg_per_unit: its LIMCOMA sack
# weight was not available at the time this was written, and a guessed
# weight is worse than an incomplete item. It still appears as a selectable
# feed item, but with no kg_per_unit it is excluded from every kg/sack
# conversion path (delivery entry, daily-record entry) until someone sets
# the weight by editing the item.
STARTER_FEED_CATALOGUE = [
    {"name": "Titanium Chick Booster Crumble", "unit": "sack", "kg_per_unit": Decimal("25")},
    {"name": "Broiler Starter Crumble", "unit": "sack", "kg_per_unit": Decimal("50")},
    {"name": "Broiler Finisher Crumble", "unit": "sack", "kg_per_unit": Decimal("50")},
    {"name": "Chick Booster Mash", "unit": "sack", "kg_per_unit": None},
]


def seed_starter_feed_catalogue(farm):
    """
    Create the starter feed catalogue on `farm`, skipping any item it
    already has by that name. Idempotent — safe to call more than once for
    the same farm without duplicating rows.
    """
    from production.models import InventoryItem

    created = []
    for item in STARTER_FEED_CATALOGUE:
        obj, was_created = InventoryItem.objects.get_or_create(
            farm_id=farm.pk,
            name=item["name"],
            defaults={
                "unit": item["unit"],
                "kg_per_unit": item["kg_per_unit"],
                "is_feed": True,
                "created_by_id": farm.owner_id,
            },
        )
        if was_created:
            created.append(obj)
    return created
