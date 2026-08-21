# accounts/serializers.py
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from farms.models import FarmMembership

from .models import Invitation, User, UserManager


class UserSerializer(serializers.ModelSerializer):
    is_internal = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "phone_number",
            "full_name",
            "email",
            "role",
            "is_internal",
            "must_change_credential",
            "date_joined",
        ]
        read_only_fields = ["id", "role", "must_change_credential", "date_joined"]


class MembershipSummarySerializer(serializers.Serializer):
    """Lightweight farm-scope payload for the login response."""

    farm_id = serializers.IntegerField(source="farm.id")
    farm_name = serializers.CharField(source="farm.name")
    role = serializers.CharField()


class PhonePinTokenSerializer(TokenObtainPairSerializer):
    """
    Login with phone number + PIN.

    SimpleJWT hardcodes 'password' in its parent serializer. We expose 'pin'
    to the client and remap it internally, so the API vocabulary matches the
    domain and the Flutter client never sees the word 'password'.
    """

    username_field = User.USERNAME_FIELD

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The parent builds 'password' in ITS __init__, so we remove it here
        # rather than declaring 'pin' as a class attribute.
        self.fields.pop("password", None)
        self.fields["pin"] = serializers.CharField(
            write_only=True, trim_whitespace=False, style={"input_type": "password"}
        )

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Claims embedded in the JWT itself. Never put secrets here —
        # a JWT payload is base64-encoded, not encrypted.
        token["role"] = user.role
        token["full_name"] = user.full_name
        token["must_change_credential"] = user.must_change_credential
        return token

    def validate(self, attrs):
        attrs["password"] = attrs.pop("pin")
        attrs[self.username_field] = UserManager.normalize_phone(
            attrs.get(self.username_field, "")
        )
        data = super().validate(attrs)

        user = self.user
        memberships = user.farm_memberships.filter(is_active=True).select_related("farm")

        data["user"] = UserSerializer(user).data
        data["must_change_credential"] = user.must_change_credential
        data["memberships"] = MembershipSummarySerializer(memberships, many=True).data
        return data


class CredentialChangeSerializer(serializers.Serializer):
    """
    Forced rotation of the owner-issued PIN. This is the non-repudiation
    event: afterwards, only the user knows their credential.
    """

    current_pin = serializers.CharField(write_only=True, trim_whitespace=False)
    new_pin = serializers.CharField(write_only=True, min_length=6, trim_whitespace=False)
    confirm_pin = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_pin(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current PIN is incorrect.")
        return value

    def validate(self, attrs):
        if attrs["new_pin"] != attrs["confirm_pin"]:
            raise serializers.ValidationError({"confirm_pin": "PINs do not match."})
        if attrs["new_pin"] == attrs["current_pin"]:
            raise serializers.ValidationError(
                {"new_pin": "New PIN must differ from the issued one."}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_credential(self.validated_data["new_pin"])
        return user


class LogoutSerializer(serializers.Serializer):
    """Explicit revocation. The client posts the refresh token it holds."""

    refresh = serializers.CharField(write_only=True)

    def validate_refresh(self, value):
        try:
            self.token = RefreshToken(value)
        except TokenError:
            raise serializers.ValidationError("Invalid or already-expired token.")
        return value

    def save(self, **kwargs):
        self.token.blacklist()


# ─────────────────────────────────────────────────────────────
# Invitations
# ─────────────────────────────────────────────────────────────


class InvitationCreateSerializer(serializers.ModelSerializer):
    """
    Issue an invitation. For a brand-new person a PIN is generated here and
    returned exactly once. For someone who already has an account, no PIN is
    issued — they keep the credential they already use elsewhere.
    """

    class Meta:
        model = Invitation
        fields = [
            "id",
            "phone_number",
            "full_name",
            "email",
            "account_role",
            "membership_role",
            "status",
            "expires_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "expires_at", "created_at"]

    def validate_phone_number(self, value):
        return UserManager.normalize_phone(value)

    def validate(self, attrs):
        farm = self.context["farm"]
        phone = attrs["phone_number"]
        membership_role = attrs.get("membership_role", "")

        if not membership_role:
            raise serializers.ValidationError(
                {"membership_role": "Required for farm invitations."}
            )
        if membership_role not in FarmMembership.Role.values:
            raise serializers.ValidationError(
                {"membership_role": "Not a valid farm role."}
            )
        if membership_role == FarmMembership.Role.OWNER:
            raise serializers.ValidationError(
                {"membership_role": "Ownership is transferred, not invited."}
            )

        if FarmMembership.objects.filter(
            farm=farm, user__phone_number=phone, is_active=True
        ).exists():
            raise serializers.ValidationError(
                {"phone_number": "This person is already a member of this farm."}
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

        invitation = Invitation(farm=farm, invited_by=invited_by, **validated_data)

        # New people need a credential; existing people keep theirs.
        self.issued_pin = None
        self.existing_user = existing
        if existing is None:
            self.issued_pin = invitation.issue_pin()

        invitation.save()
        return invitation


class InvitationReadSerializer(serializers.ModelSerializer):
    invited_by_name = serializers.CharField(source="invited_by.full_name", read_only=True)
    farm_name = serializers.CharField(source="farm.name", read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invitation
        fields = [
            "id",
            "phone_number",
            "full_name",
            "membership_role",
            "farm_name",
            "status",
            "is_expired",
            "invited_by_name",
            "created_at",
            "expires_at",
            "accepted_at",
        ]


class InvitationAcceptSerializer(serializers.Serializer):
    """Public endpoint — the invitee is not authenticated yet."""

    token = serializers.CharField(write_only=True)
    pin = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    def validate_token(self, value):
        invitation = Invitation.objects.filter(token=value).first()
        if invitation is None or not invitation.is_actionable:
            raise serializers.ValidationError("This invitation is not valid.")
        self.invitation = invitation
        return value

    def save(self, **kwargs):
        return self.invitation.accept(self.validated_data.get("pin"))