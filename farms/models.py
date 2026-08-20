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

    @property
    def can_manage_staff(self):
        return self.is_active and self.role in {self.Role.OWNER, self.Role.MANAGER}

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