# partners/serializers.py
from django.db import transaction
from rest_framework import serializers

from accounts.models import Invitation, User, UserManager
from production.models import Harvest, Payment, SaleEvent

from .models import FarmPartnerLink


class FarmPartnerLinkSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.full_name", read_only=True)
    phone_number = serializers.CharField(source="partner.phone_number", read_only=True)
    farm_name = serializers.CharField(source="farm.name", read_only=True)
    linked_by_name = serializers.CharField(
        source="linked_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = FarmPartnerLink
        fields = [
            "id",
            "farm",
            "farm_name",
            "partner",
            "partner_name",
            "phone_number",
            "business_name",
            "link_type",
            "notes",
            "is_active",
            "linked_at",
            "deactivated_at",
            "linked_by_name",
        ]
        read_only_fields = ["id", "farm", "partner", "linked_at", "deactivated_at"]


class PartnerInvitationCreateSerializer(serializers.ModelSerializer):
    """
    Invite a consumer (buyer). Identical credential mechanics to staff
    invitations — one-time PIN, forced rotation — but the acceptance produces
    a FarmPartnerLink rather than farm authority.
    """

    class Meta:
        model = Invitation
        fields = [
            "id",
            "phone_number",
            "full_name",
            "email",
            "business_name",
            "account_role",
            "status",
            "expires_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "expires_at", "created_at"]

    def validate_phone_number(self, value):
        return UserManager.normalize_phone(value)

    def validate_account_role(self, value):
        if value != User.Role.CONSUMER:
            raise serializers.ValidationError(
                "This endpoint issues consumer invitations only."
            )
        return value

    def validate(self, attrs):
        farm = self.context["farm"]
        phone = attrs["phone_number"]
        role = attrs["account_role"]

        existing = User.objects.filter(phone_number=phone).first()
        if existing and existing.is_internal:
            raise serializers.ValidationError(
                {"phone_number": "This number belongs to internal staff."}
            )
        if existing and existing.role != role:
            raise serializers.ValidationError(
                {"account_role": f"This account is registered as a {existing.role}."}
            )
        if existing and not existing.is_active:
            raise serializers.ValidationError(
                {"phone_number": "This account has been deactivated."}
            )

        if FarmPartnerLink.objects.filter(
            farm=farm, partner__phone_number=phone, link_type=role, is_active=True
        ).exists():
            raise serializers.ValidationError(
                {"phone_number": "This partner is already linked to this farm."}
            )

        if Invitation.objects.pending().filter(farm=farm, phone_number=phone).exists():
            raise serializers.ValidationError(
                {"phone_number": "A pending invitation already exists for this number."}
            )

        return attrs

    def create(self, validated_data):
        farm = self.context["farm"]
        invited_by = self.context["request"].user

        existing = User.objects.filter(
            phone_number=validated_data["phone_number"]
        ).first()

        invitation = Invitation(
            farm=farm,
            invited_by=invited_by,
            membership_role="",  # external partners hold no farm role
            **validated_data,
        )

        self.issued_pin = None
        self.existing_user = existing
        if existing is None:
            self.issued_pin = invitation.issue_pin()

        # Same as staff invitations: an overdue PENDING invite for this
        # phone and farm would trip unique_pending_invite_per_phone_farm.
        with transaction.atomic():
            Invitation.objects.filter(
                farm=farm, phone_number=invitation.phone_number
            ).expire_overdue()
            invitation.save()
        return invitation


def existing_buyer_invite_message(invitation, existing_user):
    """
    Response text for a buyer invitation that linked an EXISTING account to
    another farm instead of issuing a new PIN. Mirrors
    accounts.serializers.existing_user_invite_message, but the label is
    always "buyer" — CONSUMER is an internal role token, not something to
    show the inviting owner.
    """
    typed_name = (invitation.full_name or "").strip()
    real_name = existing_user.full_name

    detail = (
        f"{real_name} already has an account. They have been linked to "
        f"{invitation.farm.name} as a buyer using their existing PIN."
    )

    name_mismatch = bool(
        typed_name and typed_name.casefold() != real_name.casefold()
    )
    if name_mismatch:
        detail += (
            f' Heads up: you entered "{typed_name}", but '
            f"{invitation.phone_number} belongs to {real_name}."
        )

    return detail, name_mismatch


class BuyerPurchaseSerializer(serializers.ModelSerializer):
    """
    A buyer's read-only view of one harvest a farm sold them: what arrived
    and how much it weighed.

    Revenue and revenue_per_kg are deliberately withheld. Revenue is the
    FARM'S figure — entered by the farm after the sale — and showing it back
    to the buyer means showing them what the farm recorded them as paying,
    which may not match their own books. This endpoint is a delivery and
    weight ledger, not an invoice; surfacing revenue here would manufacture
    disputes the system has no business creating.

    Also withheld: `notes` and `recorded_by` (the farm's internal
    annotations), and anything about mortality, feed, or FCR (production
    performance is the farm's business, not the buyer's).
    """

    farm = serializers.IntegerField(source="batch.house.farm_id", read_only=True)
    farm_name = serializers.CharField(
        source="batch.house.farm.name", read_only=True
    )
    batch_code = serializers.CharField(source="batch.batch_code", read_only=True)
    average_weight_kg = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = Harvest
        fields = [
            "id",
            "source",
            "harvest_date",
            "farm",
            "farm_name",
            "batch_code",
            "birds_harvested",
            "total_weight_kg",
            "average_weight_kg",
        ]
        read_only_fields = fields

    def get_average_weight_kg(self, obj) -> str | None:
        val = obj.average_weight_kg
        return str(val) if val is not None else None

    def get_source(self, obj) -> str:
        return "harvest"


class BuyerSalePurchaseSerializer(serializers.ModelSerializer):
    """
    A buyer's read-only view of one SaleEvent recorded against them
    directly (see SaleEvent.buyer_link) — the per-sale counterpart to
    BuyerPurchaseSerializer, same withholding policy: no revenue (the
    farm's figure, not an invoice — see BuyerPurchaseSerializer's
    docstring), no notes, no recorded_by, nothing about mortality, feed,
    or FCR.
    """

    farm = serializers.IntegerField(source="batch.house.farm_id", read_only=True)
    farm_name = serializers.CharField(
        source="batch.house.farm.name", read_only=True
    )
    batch_code = serializers.CharField(source="batch.batch_code", read_only=True)
    harvest_date = serializers.DateField(source="sale_date", read_only=True)
    birds_harvested = serializers.IntegerField(source="bird_count", read_only=True)
    average_weight_kg = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = SaleEvent
        fields = [
            "id",
            "source",
            "harvest_date",
            "farm",
            "farm_name",
            "batch_code",
            "birds_harvested",
            "total_weight_kg",
            "average_weight_kg",
            "sale_type",
            "part_name",
        ]
        read_only_fields = fields

    def get_average_weight_kg(self, obj) -> str | None:
        val = obj.average_weight_kg
        return str(val) if val is not None else None

    def get_source(self, obj) -> str:
        return "sale"


class BuyerPaymentSerializer(serializers.ModelSerializer):
    """
    A buyer's read-only view of one payment they made against a sale.

    This is the buyer's own record of money they paid — not a figure the
    farm entered about them — so, unlike BuyerPurchaseSerializer, there is
    no dispute risk in showing it back. What stays withheld is the sale's
    side of the ledger: no revenue, no outstanding balance, no other
    buyer's payments, nothing about mortality, feed, or FCR.
    """

    farm = serializers.IntegerField(
        source="sale.batch.house.farm_id", read_only=True
    )
    farm_name = serializers.CharField(
        source="sale.batch.house.farm.name", read_only=True
    )
    batch_code = serializers.CharField(source="sale.batch.batch_code", read_only=True)
    sale_date = serializers.DateField(source="sale.sale_date", read_only=True)
    is_reversal = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "farm",
            "farm_name",
            "batch_code",
            "sale_date",
            "amount",
            "received_date",
            "is_reversal",
        ]
        read_only_fields = fields

    def get_is_reversal(self, obj) -> bool:
        return obj.reverses_id is not None