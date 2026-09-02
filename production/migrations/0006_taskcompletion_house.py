"""
Make task completions per-house.

TaskCompletion shipped keyed on (template, completion_date) — one tick per
routine item per day for the whole farm. That was a design error: the
pilot farm runs sheds in different locations with different workers, so
"morning feed" is a task each worker performs at their own house, not one
thing done once for the farm.

**This migration is destructive.** TaskCompletion has only ever held seed
and test data — the per-farm model shipped and is being corrected inside
the same unmerged change — so there is nothing real to preserve, and no
basis for choosing a house to backfill existing rows to. Every existing
completion is deleted, then the non-null `house` FK is added (safe on the
now-empty table) and the uniqueness constraint is widened to
(template, completion_date, house).
"""
import django.db.models.deletion
from django.db import migrations, models


def clear_completions(apps, schema_editor):
    apps.get_model("production", "TaskCompletion").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0005_tasktemplate_taskcompletion_and_more"),
    ]

    operations = [
        migrations.RunPython(clear_completions, migrations.RunPython.noop),
        migrations.AddField(
            model_name="taskcompletion",
            name="house",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="task_completions",
                to="production.house",
            ),
            preserve_default=False,
        ),
        migrations.RemoveConstraint(
            model_name="taskcompletion",
            name="unique_completion_per_template_day",
        ),
        migrations.AddConstraint(
            model_name="taskcompletion",
            constraint=models.UniqueConstraint(
                fields=("template", "completion_date", "house"),
                name="unique_completion_per_template_house_day",
            ),
        ),
    ]
