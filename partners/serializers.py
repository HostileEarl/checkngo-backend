# partners/serializers.py
from rest_framework import serializers

from accounts.models import Invitation, User, UserManager
from production.models import FeedDelivery

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
    Invite a supplier or consumer. Identical credential mechanics to staff
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
        if value not in {User.Role.SUPPLIER, User.Role.CONSUMER}:
            raise serializers.ValidationError(
                "This endpoint issues supplier and consumer invitations only."
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

        invitation.save()
        return invitation


class SupplierDeliverySerializer(serializers.ModelSerializer):
    """
    A supplier's read-only view of one delivery a farm recorded from them.

    Exposes what the delivery was and what it cost. Withholds `notes` and
    `recorded_by` — the farm's internal annotations, not the supplier's
    business.
    """

    farm_name = serializers.CharField(source="farm.name", read_only=True)
    feed_type_display = serializers.CharField(
        source="get_feed_type_display", read_only=True
    )

    class Meta:
        model = FeedDelivery
        fields = [
            "id",
            "farm",
            "farm_name",
            "delivery_date",
            "feed_type",
            "feed_type_display",
            "quantity_kg",
            "unit_cost",
            "total_cost",
            "invoice_ref",
        ]
        read_only_fields = fields