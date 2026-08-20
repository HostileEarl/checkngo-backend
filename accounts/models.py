# accounts/models.py
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone

e164_validator = RegexValidator(
    regex=r"^\+[1-9]\d{7,14}$",
    message="Phone number must be in E.164 format, e.g. +639171234567.",
)


class UserManager(BaseUserManager):
    use_in_migrations = True

    @staticmethod
    def normalize_phone(phone_number):
        """Strip display formatting only. Validation is the regex's job."""
        if not phone_number:
            return phone_number
        return phone_number.replace(" ", "").replace("-", "")

    def _create_user(self, phone_number, password, **extra_fields):
        if not phone_number:
            raise ValueError("Phone number is required.")

        phone_number = self.normalize_phone(phone_number)

        email = extra_fields.pop("email", None)
        email = self.normalize_email(email) if email else None

        user = self.model(phone_number=phone_number, email=email, **extra_fields)
        user.full_clean(exclude=["password"])
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(phone_number, password, **extra_fields)

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("must_change_credential", False)
        extra_fields.setdefault("role", User.Role.OWNER)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(phone_number, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        OWNER = "OWNER", "Farm Owner"
        MANAGER = "MANAGER", "Farm Manager"
        WORKER = "WORKER", "Farm Worker"
        SUPPLIER = "SUPPLIER", "Supplier"
        CONSUMER = "CONSUMER", "Consumer"

    INTERNAL_ROLES = {Role.OWNER, Role.MANAGER, Role.WORKER}

    phone_number = models.CharField(
        max_length=16, unique=True, validators=[e164_validator], db_index=True
    )
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True, unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.WORKER)

    # Non-repudiation: True until the invitee replaces the owner-issued PIN.
    must_change_credential = models.BooleanField(default=True)
    credential_changed_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "accounts_user"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["full_name"]
        indexes = [
            models.Index(fields=["role", "is_active"], name="user_role_active_idx"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.phone_number})"

    @property
    def is_internal(self):
        return self.role in self.INTERNAL_ROLES

    def set_credential(self, raw_credential, revoke_sessions=True):
        """
        Rotate this user's PIN. Used at onboarding and for any later change.

        Revoking sessions is the point: if a PIN is rotated because it leaked,
        leaving old refresh tokens alive makes the rotation meaningless.
        """
        self.set_password(raw_credential)
        self.must_change_credential = False
        self.credential_changed_at = timezone.now()
        self.save(
            update_fields=["password", "must_change_credential", "credential_changed_at"]
        )
        if revoke_sessions:
            self.revoke_all_sessions()

    def revoke_all_sessions(self):
        """Blacklist every outstanding refresh token belonging to this user."""
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )

        for token in OutstandingToken.objects.filter(user=self):
            BlacklistedToken.objects.get_or_create(token=token)


# ─────────────────────────────────────────────────────────────
# Invitation & onboarding
# ─────────────────────────────────────────────────────────────


def generate_pin(length=6):
    """
    Cryptographically random numeric PIN.

    secrets, not random — random is a Mersenne Twister seeded from system
    time and is predictable given a few prior outputs. For a credential
    that grants farm access, that is disqualifying.
    """
    return "".join(secrets.choice("0123456789") for _ in range(length))


def generate_invite_token():
    """URL-safe opaque token. ~256 bits of entropy; not guessable, not enumerable."""
    return secrets.token_urlsafe(32)


class InvitationQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(
            status=Invitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        )

    def for_farm(self, farm):
        return self.filter(farm=farm)


class Invitation(models.Model):
    """
    An owner/manager-issued credential handoff.

    The PIN is shown to the issuer exactly once, at creation, then stored
    only as a hash. The invitee logs in with phone + PIN and is forced to
    replace it — that replacement is the non-repudiation event.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    DEFAULT_VALIDITY_DAYS = 7

    # --- Who is being invited ---
    phone_number = models.CharField(
        max_length=16, validators=[e164_validator], db_index=True
    )
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True)

    # --- What they are being invited to ---
    farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.CASCADE,
        related_name="invitations",
        null=True,
        blank=True,
        help_text="Null for external partners, who hold no farm membership.",
    )
    account_role = models.CharField(max_length=20, choices=User.Role.choices)
    membership_role = models.CharField(
        max_length=20,
        blank=True,
        help_text="Operational role at the farm. Blank for external partners.",
    )

    # --- Credential material ---
    token = models.CharField(
        max_length=64, unique=True, default=generate_invite_token, editable=False
    )
    pin_hash = models.CharField(max_length=128, editable=False)

    # --- Lifecycle ---
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitation",
    )

    objects = InvitationQuerySet.as_manager()

    class Meta:
        db_table = "accounts_invitation"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["phone_number", "status"], name="invite_phone_status_idx"),
            models.Index(fields=["farm", "status"], name="invite_farm_status_idx"),
        ]
        constraints = [
            # Internal invites: unique per (phone, farm).
            models.UniqueConstraint(
                fields=["phone_number", "farm"],
                condition=models.Q(status="PENDING", farm__isnull=False),
                name="unique_pending_invite_per_phone_farm",
            ),
            # External invites have farm=NULL, and in Postgres NULL != NULL,
            # so the constraint above would never fire for them. This one does.
            models.UniqueConstraint(
                fields=["phone_number"],
                condition=models.Q(status="PENDING", farm__isnull=True),
                name="unique_pending_external_invite_per_phone",
            ),
        ]

    def __str__(self):
        return f"Invite: {self.full_name} ({self.phone_number}) — {self.status}"

    # --- Creation ---

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=self.DEFAULT_VALIDITY_DAYS)
        if not self.pin_hash:
            raise ValueError("Call issue_pin() before saving an Invitation.")
        self.phone_number = UserManager.normalize_phone(self.phone_number)
        super().save(*args, **kwargs)

    def issue_pin(self, length=6):
        """
        Generate a PIN, store only its hash, return the plaintext ONCE.
        The caller is responsible for surfacing it to the issuer; it is
        never recoverable afterwards.
        """
        from django.contrib.auth.hashers import make_password

        raw_pin = generate_pin(length)
        self.pin_hash = make_password(raw_pin)
        return raw_pin

    # --- State ---

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_actionable(self):
        return self.status == self.Status.PENDING and not self.is_expired

    def check_pin(self, raw_pin):
        from django.contrib.auth.hashers import check_password

        return check_password(raw_pin, self.pin_hash)

    def revoke(self):
        if self.status != self.Status.PENDING:
            return
        self.status = self.Status.REVOKED
        self.save(update_fields=["status"])

    # --- Acceptance ---

    @transaction.atomic
    def accept(self, raw_pin):
        """
        Convert a pending invitation into a real User (+ FarmMembership).

        Atomic: either the user, the membership, and the status change all
        land, or none of them do. A half-created worker with no farm access
        would be worse than a clean failure.
        """
        from farms.models import FarmMembership

        if not self.is_actionable:
            raise ValidationError("This invitation is no longer valid.")
        if not self.check_pin(raw_pin):
            raise ValidationError("Incorrect PIN.")

        user = User.objects.create_user(
            phone_number=self.phone_number,
            password=raw_pin,
            full_name=self.full_name,
            email=self.email or None,
            role=self.account_role,
        )
        # must_change_credential defaults to True — the forced rotation
        # in Task 7 is what makes the PIN non-repudiable.

        if self.farm and self.membership_role:
            FarmMembership.objects.create(
                farm=self.farm,
                user=user,
                role=self.membership_role,
                invited_by=self.invited_by,
            )

        self.status = self.Status.ACCEPTED
        self.accepted_at = timezone.now()
        self.accepted_user = user
        self.save(update_fields=["status", "accepted_at", "accepted_user"])
        return user