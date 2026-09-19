from django.db import migrations


def set_is_feed(apps, schema_editor):
    InventoryItem = apps.get_model("production", "InventoryItem")
    InventoryItem.objects.filter(kg_per_unit__isnull=False).update(is_feed=True)


def unset_is_feed(apps, schema_editor):
    InventoryItem = apps.get_model("production", "InventoryItem")
    InventoryItem.objects.filter(kg_per_unit__isnull=False).update(is_feed=False)


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0011_alter_batch_options_feeddelivery_inventory_item_and_more"),
    ]

    operations = [
        migrations.RunPython(set_is_feed, unset_is_feed),
    ]
