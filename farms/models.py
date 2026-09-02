# farms/models.py
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Farm(models.Model):
    """A physical poultry site. The unit of operational isolation in CheckN Go."""

    name = models.CharField(max_length=150)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_farms",
    )
    address = models.CharField(max_length=255, blank=True)
    municipality = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "farms_farm"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="unique_farm_name_per_owner",
            ),
        ]

    def __str__(self):
        return self.name

    def active_members(self):
        """Every user currently authorized to operate on this farm, owner included."""
        return self.memberships.filter(is_active=True).select_related("user")

    def membership_for(self, user):
        """Authoritative permission lookup. Returns None if the user has no active role here."""
        if not user or not user.is_authenticated:
            return None
        return self.memberships.filter(user=user, is_active=True).first()

    def archive(self):
        """Close the farm to new data. History remains readable by the owner."""
        if not self.is_active:
            return
        self.is_active = False
        self.save(update_fields=["is_active"])

    def reactivate(self):
        self.is_active = True
        self.save(update_fields=["is_active"])


class FarmMembership(models.Model):
    """
    Join table binding an internal user to a farm with an operational role.
    This is the single authoritative source for all permission checks.

    External partners (suppliers, consumers) never appear here — they transact
    with farms but hold no operational authority over them.
    """

    class Role(models.TextChoices):
        OWNER = "OWNER", "Farm Owner"
        MANAGER = "MANAGER", "Farm Manager"
        WORKER = "WORKER", "Farm Worker"

    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="farm_memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices)

    houses = models.ManyToManyField(
        "production.House",
        blank=True,
        related_name="assigned_memberships",
        help_text="Houses this member may record against. Empty means all.",
    )

    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_memberships",
    )

    class Meta:
        db_table = "farms_membership"
        ordering = ["farm", "role", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "user"],
                name="unique_membership_per_farm_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"], name="membership_user_active_idx"),
            models.Index(fields=["farm", "role"], name="membership_farm_role_idx"),
        ]

    def __str__(self):
        return f"{self.user.full_name} @ {self.farm.name} ({self.get_role_display()})"

    def clean(self):
        """External partners must never receive operational authority over a farm."""
        if self.user_id and not self.user.is_internal:
            raise ValidationError(
                {"user": "External partners cannot hold farm memberships."}
            )
        # M2M rows are not populated on an unsaved instance, and save() never
        # calls clean(), so this only bites for an already-saved membership
        # (Django admin, a manual full_clean()). The serializer's
        # validate_houses is the authoritative guard.
        if self.pk and self._assigned_houses_mismatch():
            raise ValidationError(
                {"houses": "A house assigned here must belong to the same farm."}
            )

    @property
    def can_manage_staff(self):
        return self.is_active and self.role in {self.Role.OWNER, self.Role.MANAGER}

    def may_write_to_house(self, house):
        """
        Owners and managers write anywhere on their farm. A worker with no
        house assignments is unrestricted; one with assignments is limited
        to those houses.
        """
        if self.role in {self.Role.OWNER, self.Role.MANAGER}:
            return True
        if not self.houses.exists():
            return True
        return self.houses.filter(pk=house.pk).exists()

    def _assigned_houses_mismatch(self):
        """House pks assigned to this membership that belong to another farm."""
        return list(
            self.houses.exclude(farm_id=self.farm_id).values_list("pk", flat=True)
        )

    def deactivate(self):
        """Soft-revoke access while preserving the audit trail."""
        if not self.is_active:
            return
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save(update_fields=["is_active", "deactivated_at"])

    def reactivate(self):
        self.is_active = True
        self.deactivated_at = None
        self.save(update_fields=["is_active", "deactivated_at"])


class FarmOwnershipHistory(models.Model):
    """
    Immutable record of every ownership change.

    Transfers delete the outgoing owner's membership, so without this table
    the question "who held this farm in March?" would be unanswerable. Rows
    here are written once and never edited — that is what makes them evidence.
    """

    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="ownership_history",
    )
    from_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="farms_transferred_away",
        help_text="Null for the original registration.",
    )
    to_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="farms_transferred_in",
    )
    # Denormalized snapshots: survive even if an account is later deleted.
    from_owner_name = models.CharField(max_length=150, blank=True)
    to_owner_name = models.CharField(max_length=150, blank=True)

    transferred_at = models.DateTimeField(default=timezone.now, db_index=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="ownership_transfers_performed",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "farms_ownership_history"
        ordering = ["-transferred_at"]
        verbose_name_plural = "Farm ownership history"
        indexes = [
            models.Index(fields=["farm", "-transferred_at"], name="ownership_farm_time_idx"),
        ]

    def __str__(self):
        origin = self.from_owner_name or "registration"
        return f"{self.farm.name}: {origin} → {self.to_owner_name}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Ownership history records are immutable.")
        # Snapshot the names at transfer time.
        if self.from_owner and not self.from_owner_name:
            self.from_owner_name = self.from_owner.full_name
        if self.to_owner and not self.to_owner_name:
            self.to_owner_name = self.to_owner.full_name
        super().save(*args, **kwargs)