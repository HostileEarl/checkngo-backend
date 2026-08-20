# accounts/serializers.py
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User, UserManager


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

    Overrides SimpleJWT's username/password flow so the client can post
    a normalized E.164 number, and so the response carries the routing
    signal the mobile app needs (must_change_credential).
    """

    username_field = User.USERNAME_FIELD

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Claims embedded in the JWT itself — readable by the client
        # without an extra round trip. Never put secrets here; a JWT
        # payload is base64, not encrypted.
        token["role"] = user.role
        token["full_name"] = user.full_name
        token["must_change_credential"] = user.must_change_credential
        return token

    def validate(self, attrs):
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
    event: after this, only the user knows their credential.
    """

    current_credential = serializers.CharField(write_only=True, trim_whitespace=False)
    new_credential = serializers.CharField(
        write_only=True, min_length=6, trim_whitespace=False
    )
    confirm_credential = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_credential(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current PIN is incorrect.")
        return value

    def validate(self, attrs):
        if attrs["new_credential"] != attrs["confirm_credential"]:
            raise serializers.ValidationError(
                {"confirm_credential": "Credentials do not match."}
            )
        if attrs["new_credential"] == attrs["current_credential"]:
            raise serializers.ValidationError(
                {"new_credential": "New credential must differ from the issued PIN."}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_credential(self.validated_data["new_credential"])
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