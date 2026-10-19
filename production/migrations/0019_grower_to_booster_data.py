# Generated manually — data migration for the feed-type rename.
from django.db import migrations


def grower_to_booster(apps, schema_editor):
    """
    Rename every stored GROWER feed delivery to BOOSTER.

    OTHER rows are untouched — they stay as legacy history, not selectable
    for a new delivery (enforced in FeedDelivery.clean()), but still valid
    on any row written before this change.
    """
    FeedDelivery = apps.get_model("production", "FeedDelivery")
    FeedDelivery.objects.filter(feed_type="GROWER").update(feed_type="BOOSTER")


def booster_to_grower(apps, schema_editor):
    """
    Best-effort reverse: renames every BOOSTER row back to GROWER.

    Not a perfect inverse if a BOOSTER row was created after this
    migration ran (there is no way to tell it apart from a migrated
    GROWER row), but it restores the pre-migration state for the common
    case of reversing this migration shortly after applying it.
    """
    FeedDelivery = apps.get_model("production", "FeedDelivery")
    FeedDelivery.objects.filter(feed_type="BOOSTER").update(feed_type="GROWER")


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0018_feed_type_buyer_parts"),
    ]

    operations = [
        migrations.RunPython(grower_to_booster, booster_to_grower),
    ]
