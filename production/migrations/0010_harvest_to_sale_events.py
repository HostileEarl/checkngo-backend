"""
Migrate each historical Harvest to a single SaleEvent.

The old model recorded a batch leaving in one harvest: a bird count, a
group weight, and an optional revenue figure. The new model records the
same birds leaving across many SaleEvent rows, and FCR / feed margin now
read the SUM of those rows.

To keep historical FCR and feed margin identical, every existing Harvest
becomes one SaleEvent carrying its exact figures:

    sale_date   <- harvest.harvest_date
    bird_count  <- harvest.birds_harvested
    total_weight_kg <- harvest.total_weight_kg
    revenue     <- harvest.revenue
    recorded_by <- harvest.recorded_by

ASSUMPTION: sale_type is set to DRESSED. The original data did not record
a type, and dressed is the dominant channel for a bulk close; there is no
way to recover the real split, so a single deterministic choice is made
and noted here rather than guessed per row.

Then every batch's denormalised sale totals are rebuilt from the rows.
"""
import uuid

from django.db import migrations
from django.db.models import Sum


MIGRATION_NOTE = "Migrated from the pre-incremental single-harvest record."


def forwards(apps, schema_editor):
    Harvest = apps.get_model("production", "Harvest")
    SaleEvent = apps.get_model("production", "SaleEvent")
    Batch = apps.get_model("production", "Batch")

    for harvest in Harvest.objects.select_related("batch").all():
        if harvest.birds_harvested is None or harvest.total_weight_kg is None:
            continue
        SaleEvent.objects.create(
            id=uuid.uuid4(),
            batch=harvest.batch,
            sale_date=harvest.harvest_date,
            sale_type="DRESSED",
            bird_count=harvest.birds_harvested,
            total_weight_kg=harvest.total_weight_kg,
            revenue=harvest.revenue,
            notes=MIGRATION_NOTE,
            recorded_by=harvest.recorded_by,
            recorded_at=harvest.created_at,
        )

    for batch in Batch.objects.all():
        agg = SaleEvent.objects.filter(batch=batch).aggregate(
            birds=Sum("bird_count"), weight=Sum("total_weight_kg")
        )
        batch.total_birds_sold = agg["birds"] or 0
        batch.total_weight_sold_kg = agg["weight"] or 0
        batch.save(update_fields=["total_birds_sold", "total_weight_sold_kg"])


def backwards(apps, schema_editor):
    SaleEvent = apps.get_model("production", "SaleEvent")
    Batch = apps.get_model("production", "Batch")

    SaleEvent.objects.filter(notes=MIGRATION_NOTE).delete()
    for batch in Batch.objects.all():
        agg = SaleEvent.objects.filter(batch=batch).aggregate(
            birds=Sum("bird_count"), weight=Sum("total_weight_kg")
        )
        batch.total_birds_sold = agg["birds"] or 0
        batch.total_weight_sold_kg = agg["weight"] or 0
        batch.save(update_fields=["total_birds_sold", "total_weight_sold_kg"])


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0009_batch_total_birds_sold_batch_total_weight_sold_kg_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
