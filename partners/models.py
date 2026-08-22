# partners/models.py
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class FarmPartnerLink(models.Model):
    """
    An operational relationship between a farm and an external partner.

    Distinct from FarmMembership in kind, not degree: a membership grants
    authority INSIDE a farm, this grants a transactional relationship WITH
    one. A supplier linked here can see the orders addressed to them; they
    cannot see flock records, staff, or production data.
    """

    class LinkType(models.TextChoices):
        SUPPLIER = "SUPPLIER", "Supplier"
        CONSUMER = "CONSUMER", "Consumer"

    farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.CASCADE,
        related_name="partner_links",
    )
    partner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="farm_links",
    )
    link_type = models.CharField(max_length=20, choices=LinkType.choices)

    business_name = models.CharField(max_length=150, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)
    linked_at = models.DateTimeField(default=timezone.now)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_links_created",
    )

    class Meta:
        db_table = "partners_farm_link"
        ordering = ["farm", "link_type", "business_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "partner", "link_type"],
                name="unique_partner_link_per_farm_type",
            ),
        ]
        indexes = [
            models.Index(fields=["partner", "is_active"], name="link_partner_active_idx"),
            models.Index(fields=["farm", "link_type"], name="link_farm_type_idx"),
        ]

    def __str__(self):
        label = self.business_name or self.partner.full_name
        return f"{label} ↔ {self.farm.name} ({self.get_link_type_display()})"

    def clean(self):
        """Only external accounts belong here; internal staff use FarmMembership."""
        if self.partner_id and self.partner.is_internal:
            raise ValidationError(
                {"partner": "Internal staff are linked through FarmMembership, not here."}
            )
        if self.partner_id and self.link_type != self.partner.role:
            raise ValidationError(
                {"link_type": "Link type must match the partner's account role."}
            )

    def deactivate(self):
        """End the relationship without erasing the transaction history behind it."""
        if not self.is_active:
            return
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save(update_fields=["is_active", "deactivated_at"])

    def reactivate(self):
        self.is_active = True
        self.deactivated_at = None
        self.save(update_fields=["is_active", "deactivated_at"])