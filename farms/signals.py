# farms/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Farm, FarmMembership, FarmOwnershipHistory


@receiver(post_save, sender=Farm, dispatch_uid="farm_owner_membership")
def ensure_owner_membership(sender, instance, created, raw, **kwargs):
    """
    Guarantee the farm owner holds an OWNER membership.

    Without this, every permission class would need a second code path for
    the owner. With it, authorization is always a single membership lookup.
    """
    if raw:
        # Loading fixtures — the related User may not exist in the DB yet.
        return

    FarmMembership.objects.update_or_create(
        farm=instance,
        user=instance.owner,
        defaults={
            "role": FarmMembership.Role.OWNER,
            "is_active": True,
            "deactivated_at": None,
        },
    )


@receiver(post_save, sender=Farm, dispatch_uid="farm_registration_history")
def record_initial_ownership(sender, instance, created, raw, **kwargs):
    """
    Open the ownership ledger when a farm is registered.

    Only fires on creation — later transfers are recorded explicitly by the
    transfer serializer, which knows who performed the action.
    """
    if raw or not created:
        return

    FarmOwnershipHistory.objects.create(
        farm=instance,
        from_owner=None,
        to_owner=instance.owner,
        performed_by=instance.owner,
        note="Farm registered.",
    )