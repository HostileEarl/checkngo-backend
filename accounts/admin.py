# accounts/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import Invitation, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    The stock UserAdmin references 'username' and crashes on this model.
    This one is keyed on phone_number throughout.
    """

    ordering = ["full_name"]
    list_display = [
        "full_name",
        "phone_number",
        "role",
        "credential_status",
        "is_active",
        "date_joined",
    ]
    list_filter = ["role", "is_active", "must_change_credential", "is_staff"]
    search_fields = ["full_name", "phone_number", "email"]
    readonly_fields = ["date_joined", "last_login", "credential_changed_at"]

    fieldsets = (
        (None, {"fields": ("phone_number", "password")}),
        ("Identity", {"fields": ("full_name", "email", "role")}),
        (
            "Credential",
            {
                "fields": ("must_change_credential", "credential_changed_at"),
                "description": (
                    "must_change_credential gates the API. Clearing it by hand "
                    "defeats the forced-rotation guarantee — let the user rotate "
                    "their own PIN instead."
                ),
            },
        ),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone_number", "full_name", "role", "password1", "password2"),
            },
        ),
    )

    @admin.display(description="Credential")
    def credential_status(self, obj):
        if obj.must_change_credential:
            return format_html('<span style="color:#c0392b;">Not yet rotated</span>')
        return format_html('<span style="color:#27ae60;">Rotated</span>')


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    """Read-only. An invitation is a credential handoff record, not a form."""

    list_display = [
        "full_name",
        "phone_number",
        "farm",
        "account_role",
        "status",
        "created_at",
        "expires_at",
    ]
    list_filter = ["status", "account_role", "farm"]
    search_fields = ["full_name", "phone_number", "business_name"]
    readonly_fields = [
        "phone_number",
        "full_name",
        "email",
        "business_name",
        "farm",
        "account_role",
        "membership_role",
        "token",
        "pin_hash",
        "status",
        "invited_by",
        "created_at",
        "expires_at",
        "accepted_at",
        "accepted_user",
    ]
    actions = ["revoke_invitations"]

    def has_add_permission(self, request):
        # Invitations must be issued through the API so a PIN is generated.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Revoke selected pending invitations")
    def revoke_invitations(self, request, queryset):
        count = 0
        for invitation in queryset.filter(status=Invitation.Status.PENDING):
            invitation.revoke()
            count += 1
        self.message_user(request, f"{count} invitation(s) revoked.")