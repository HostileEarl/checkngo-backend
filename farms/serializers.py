# farms/serializers.py
from django.db import transaction
from rest_framework import serializers

from accounts.models import User, UserManager

from .models import Farm, FarmMembership, FarmOwnershipHistory


class FarmSerializer(serializers.ModelSerializer):
    owner_name = serializers.CharField(source="owner.full_name", read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Farm
        fields = [
            "id",
            "name",
            "owner",
            "owner_name",
            "address",
            "municipality",
            "province",
            "is_active",
            "member_count",
            "created_at",
        ]
        read_only_fields = ["id", "owner", "is_active", "created_at"]
    
    def get_member_count(self, obj) -> int:
        return obj.memberships.filter(is_active=True).count()


class FarmMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", read_only=True)
    invited_by_name = serializers.CharField(
        source="invited_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = FarmMembership
        fields = [
            "id",
            "user",
            "user_name",
            "phone_number",
            "role",
            "is_active",
            "joined_at",
            "deactivated_at",
            "invited_by_name",
        ]
        read_only_fields = ["id", "user", "joined_at", "deactivated_at"]


class FarmOwnershipHistorySerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source="performed_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = FarmOwnershipHistory
        fields = [
            "id",
            "from_owner_name",
            "to_owner_name",
            "transferred_at",
            "performed_by_name",
            "note",
        ]


class OwnershipTransferSerializer(serializers.Serializer):
    """
    Hand a farm to a new owner.

    The outgoing owner's membership is DELETED, so they retain no visibility
    into the new owner's operations. The tenure itself is preserved in
    FarmOwnershipHistory — access ends, the record does not.
    """

    new_owner_phone = serializers.CharField(write_only=True)
    confirm_transfer = serializers.BooleanField(write_only=True)
    note = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=255
    )

    def validate_confirm_transfer(self, value):
        if not value:
            raise serializers.ValidationError(
                "Ownership transfer must be explicitly confirmed."
            )
        return value

    def validate_new_owner_phone(self, value):
        phone = UserManager.normalize_phone(value)
        farm = self.context["farm"]

        new_owner = User.objects.filter(phone_number=phone, is_active=True).first()
        if new_owner is None:
            raise serializers.ValidationError("No active account with that number.")
        if not new_owner.is_internal:
            raise serializers.ValidationError("External partners cannot own a farm.")
        if new_owner == farm.owner:
            raise serializers.ValidationError("This person already owns the farm.")

        self.new_owner = new_owner
        return phone

    @transaction.atomic
    def save(self, **kwargs):
        farm = self.context["farm"]
        actor = self.context["request"].user
        outgoing = farm.owner
        incoming = self.new_owner

        # Write the ledger entry BEFORE destroying the membership, so the
        # record exists even if a later step fails and rolls back.
        FarmOwnershipHistory.objects.create(
            farm=farm,
            from_owner=outgoing,
            to_owner=incoming,
            performed_by=actor,
            note=self.validated_data.get("note", ""),
        )

        FarmMembership.objects.filter(farm=farm, user=outgoing).delete()

        farm.owner = incoming
        farm.save(update_fields=["owner"])
        # post_save promotes the incoming user's membership to OWNER.

        incoming.role = User.Role.OWNER
        incoming.save(update_fields=["role"])

        return farm