# accounts/migrations/0006_retire_supplier_role.py
"""
Data migration for retiring the SUPPLIER role, run before any of the schema
changes that follow (production.FeedDelivery.supplier_link is dropped,
and SUPPLIER is removed from every choices list, in later migrations).

Feed source becomes free text (FeedDelivery.other_source) instead of a
link to a registered supplier partner. This migration folds the existing
supplier_link attribution into other_source so that history survives,
then removes the SUPPLIER identities that have nothing left to anchor
them.
"""
from django.db import migrations

# Every FK field across the project that points at accounts.User, as of
# this migration. Used to verify a SUPPLIER user has nothing else
# attached to them before it is deleted — if anything does, the migration
# fails loudly instead of silently losing that attribution via SET_NULL.
USER_REFERENCING_FIELDS = [
    ("admin", "logentry", "user"),
    ("token_blacklist", "outstandingtoken", "user"),
    ("accounts", "invitation", "invited_by"),
    ("accounts", "invitation", "accepted_user"),
    ("farms", "farm", "owner"),
    ("farms", "farmmembership", "user"),
    ("farms", "farmmembership", "invited_by"),
    ("farms", "farmownershiphistory", "from_owner"),
    ("farms", "farmownershiphistory", "to_owner"),
    ("farms", "farmownershiphistory", "performed_by"),
    ("partners", "farmpartnerlink", "partner"),
    ("partners", "farmpartnerlink", "linked_by"),
    ("production", "batch", "terminated_by"),
    ("production", "batch", "created_by"),
    ("production", "dailyrecord", "recorded_by"),
    ("production", "weightsample", "recorded_by"),
    ("production", "harvest", "recorded_by"),
    ("production", "saleevent", "recorded_by"),
    ("production", "payment", "recorded_by"),
    ("production", "feeddelivery", "recorded_by"),
    ("production", "recordcorrection", "corrected_by"),
    ("production", "inventoryitem", "created_by"),
    ("production", "inventorystockin", "recorded_by"),
    ("production", "inventoryusagelog", "recorded_by"),
    ("production", "tasktemplate", "created_by"),
    ("production", "taskcompletion", "recorded_by"),
]


def retire_supplier_role(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Invitation = apps.get_model("accounts", "Invitation")
    FarmPartnerLink = apps.get_model("partners", "FarmPartnerLink")
    FeedDelivery = apps.get_model("production", "FeedDelivery")

    # 1. Fold supplier_link into other_source. A delivery that already has
    #    other_source set (shouldn't happen given the exclusivity the API
    #    enforces, but this is a one-way migration over real data) keeps
    #    what it has rather than being overwritten; it's reported, not lost.
    kept_other_source = []
    deliveries = FeedDelivery.objects.filter(
        supplier_link__isnull=False
    ).select_related("supplier_link", "supplier_link__partner")
    for delivery in deliveries:
        link = delivery.supplier_link
        if delivery.other_source:
            kept_other_source.append(
                f"FeedDelivery {delivery.pk}: other_source already "
                f"{delivery.other_source!r}; discarding supplier_link "
                f"{link.pk} ({link.business_name or link.partner.full_name}) "
                "without copying its name."
            )
        else:
            delivery.other_source = link.business_name or link.partner.full_name
        delivery.supplier_link = None
        delivery.save(update_fields=["other_source", "supplier_link"])

    if kept_other_source:
        print(
            "\nremove-supplier-role data migration — deliveries whose "
            "other_source was already set (supplier name discarded):"
        )
        for line in kept_other_source:
            print(f"  - {line}")

    # 2. The supplier_link FKs pointing at them are now all cleared, so the
    #    SUPPLIER links themselves can go.
    FarmPartnerLink.objects.filter(link_type="SUPPLIER").delete()

    # 3. Cancel SUPPLIER invitations outright — the role they onboard into
    #    no longer exists, pending or accepted.
    Invitation.objects.filter(account_role="SUPPLIER").delete()

    # 4. Supplier users: delete iff nothing else references them.
    OutstandingToken = apps.get_model("token_blacklist", "OutstandingToken")
    for user in User.objects.filter(role="SUPPLIER"):
        # Session bookkeeping, not attribution — clearing it is part of
        # retiring the account, not data loss. BlacklistedToken rows
        # cascade with their OutstandingToken.
        OutstandingToken.objects.filter(user_id=user.pk).delete()

        blockers = []
        for app_label, model_name, field_name in USER_REFERENCING_FIELDS:
            model = apps.get_model(app_label, model_name)
            count = model.objects.filter(**{f"{field_name}_id": user.pk}).count()
            if count:
                blockers.append(f"{app_label}.{model_name}.{field_name} ({count} row(s))")
        if blockers:
            raise RuntimeError(
                f"Cannot delete supplier user {user.pk} ({user.full_name}, "
                f"{user.phone_number}): still referenced by "
                f"{', '.join(blockers)}. Resolve these references manually "
                "before re-running this migration."
            )
        user.delete()


def noop_reverse(apps, schema_editor):
    """
    The SUPPLIER role and its links cannot be reconstructed from
    other_source free text, so there is nothing to undo. A safe no-op
    rather than a crash.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_farmowneraccount"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("token_blacklist", "0013_alter_blacklistedtoken_options_and_more"),
        ("farms", "0003_farmmembership_houses"),
        ("partners", "0001_initial"),
        ("production", "0015_payment"),
    ]

    operations = [
        migrations.RunPython(retire_supplier_role, noop_reverse),
    ]
