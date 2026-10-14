# accounts/tests/test_retire_supplier_role_migration.py
"""
Exercises accounts.migrations.0006_retire_supplier_role.retire_supplier_role
directly against historical model state, the way the real migration runs
it against production data — rather than against the post-migration
schema, which no longer has FeedDelivery.supplier_link to test with.
"""
import importlib

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

PRE_STATE = [
    ("accounts", "0005_farmowneraccount"),
    ("partners", "0001_initial"),
    ("production", "0015_payment"),
    # Not rolled back — already at these leaves — but named explicitly so
    # the historical apps registry below includes them, the same way the
    # real migration's own `dependencies` do.
    ("admin", "0003_logentry_add_action_flag_choices"),
    ("token_blacklist", "0013_alter_blacklistedtoken_options_and_more"),
]

migration_module = importlib.import_module(
    "accounts.migrations.0006_retire_supplier_role"
)


@pytest.mark.django_db(transaction=True)
def test_retire_supplier_role_moves_name_into_other_source():
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(PRE_STATE)
        old_apps = executor.loader.project_state(PRE_STATE).apps

        User = old_apps.get_model("accounts", "User")
        Farm = old_apps.get_model("farms", "Farm")
        FarmPartnerLink = old_apps.get_model("partners", "FarmPartnerLink")
        FeedDelivery = old_apps.get_model("production", "FeedDelivery")

        owner = User.objects.create(
            phone_number="+639171112222", full_name="Owner", role="OWNER", password="x"
        )
        farm = Farm.objects.create(name="Test Farm", owner=owner)

        supplier_user = User.objects.create(
            phone_number="+639175556666",
            full_name="Ricardo Lim",
            role="SUPPLIER",
            password="x",
        )
        link = FarmPartnerLink.objects.create(
            farm=farm,
            partner=supplier_user,
            link_type="SUPPLIER",
            business_name="Lim Feeds Trading",
            linked_by=owner,
        )
        delivery = FeedDelivery.objects.create(
            farm=farm,
            supplier_link=link,
            delivery_date="2026-01-10",
            feed_type="STARTER",
            quantity_kg="1000.00",
        )

        # A second delivery whose other_source was already filled in — the
        # anomalous, exclusivity-violating case. It must be kept, not
        # overwritten, and reported rather than silently lost.
        pre_filled = FeedDelivery.objects.create(
            farm=farm,
            supplier_link=link,
            other_source="Already recorded by hand",
            delivery_date="2026-01-11",
            feed_type="STARTER",
            quantity_kg="500.00",
        )

        migration_module.retire_supplier_role(old_apps, None)

        delivery.refresh_from_db()
        assert delivery.other_source == "Lim Feeds Trading"
        assert delivery.supplier_link_id is None

        pre_filled.refresh_from_db()
        assert pre_filled.other_source == "Already recorded by hand"
        assert pre_filled.supplier_link_id is None

        assert not FarmPartnerLink.objects.filter(link_type="SUPPLIER").exists()
        assert not User.objects.filter(pk=supplier_user.pk).exists()
    finally:
        # Restore the project to its full, current migration state for the
        # rest of the suite.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
