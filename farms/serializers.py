# farms/serializers.py
from django.db import IntegrityError, transaction
from rest_framework import serializers

from accounts.models import User, UserManager

from .models import Farm, FarmMembership, FarmOwnershipHistory

# unique_farm_name_per_owner, answered as a 400 instead of an IntegrityError.
DUPLICATE_FARM_NAME = "A farm with this name already exists for this owner."


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

    def validate_name(self, value):
        # The owner is the caller when registering, the farm's owner when
        # an owner or manager renames it.
        owner = self.instance.owner if self.instance else self.context["request"].user
        clash = Farm.objects.filter(owner=owner, name=value)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(DUPLICATE_FARM_NAME)
        return value

    def create(self, validated_data):
        # Two identical registrations at once both pass validate_name; the
        # loser gets the same answer instead of the constraint's 500.
        try:
            with transaction.atomic():
                return super().create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError({"name": [DUPLICATE_FARM_NAME]})


class FarmMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", read_only=True)
    invited_by_name = serializers.CharField(
        source="invited_by.full_name", read_only=True, default=None
    )
    house_names = serializers.SerializerMethodField()

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
            "houses",
            "house_names",
        ]
        read_only_fields = ["id", "user", "joined_at", "deactivated_at"]

    def get_house_names(self, obj) -> list[str]:
        return [h.name for h in obj.houses.all()]

    def validate_houses(self, value):
        """A house assigned to a membership must belong to that membership's
        farm, and must not be archived — an archived house is retired from
        daily use, so there is nothing left to assign a worker to it for."""
        farm = self.instance.farm if self.instance else self.context.get("farm")
        if farm is not None:
            # Never name them: house ids are sequential, and echoing the
            # name of whatever another farm's id points at would let any
            # owner or manager read every farm's house names.
            if any(h.farm_id != farm.pk for h in value):
                raise serializers.ValidationError(
                    "One or more of these houses is not on this farm."
                )
        archived = [h.name for h in value if h.is_archived]
        if archived:
            raise serializers.ValidationError(
                f"These houses are archived: {', '.join(archived)}."
            )
        return value


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
        if Farm.objects.filter(owner=new_owner, name=farm.name).exists():
            raise serializers.ValidationError(
                f"{DUPLICATE_FARM_NAME} Rename this farm before transferring it."
            )

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