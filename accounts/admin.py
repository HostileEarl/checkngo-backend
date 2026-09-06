# accounts/admin.py
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count, Prefetch, Q
from django.utils.html import format_html

from farms.models import FarmMembership

from .models import FarmOwnerAccount, Invitation, User
from .services import issue_owner_account


class UserFarmMembershipInline(admin.TabularInline):
    """
    Read-only view of the user's active farm scope.

    Memberships are created and revoked from the Farms section (or via the
    invite flow), never typed in here — this inline exists so an
    administrator can see someone's reach without leaving the user page.
    """

    model = FarmMembership
    fk_name = "user"
    extra = 0
    can_delete = False
    verbose_name_plural = "Farm memberships (read-only — manage from Farms)"
    fields = ["farm", "role", "is_active", "joined_at", "invited_by"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class NoMembershipFilter(admin.SimpleListFilter):
    """
    Owners who have registered but not yet been attached to a farm — the
    signal that they are still sitting in the setup wizard.
    """

    title = "farm membership"
    parameter_name = "membership"

    def lookups(self, request, model_admin):
        return [
            ("none", "No active membership"),
            ("some", "Has an active membership"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "none":
            return queryset.filter(_active_membership_count=0)
        if self.value() == "some":
            return queryset.filter(_active_membership_count__gt=0)
        return queryset


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
        "membership_count",
        "member_of",
        "is_active",
        "date_joined",
    ]
    list_filter = [
        "role",
        "is_active",
        "must_change_credential",
        "is_staff",
        NoMembershipFilter,
    ]
    search_fields = ["full_name", "phone_number", "email"]
    readonly_fields = ["date_joined", "last_login", "credential_changed_at"]
    date_hierarchy = "date_joined"
    inlines = [UserFarmMembershipInline]

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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            _active_membership_count=Count(
                "farm_memberships",
                filter=Q(farm_memberships__is_active=True),
                distinct=True,
            )
        )
        return qs.prefetch_related(
            Prefetch(
                "farm_memberships",
                queryset=FarmMembership.objects.filter(is_active=True).select_related(
                    "farm"
                ),
                to_attr="_active_memberships",
            )
        )

    @admin.display(description="Credential")
    def credential_status(self, obj):
        if obj.must_change_credential:
            return format_html('<span style="color:{};">{}</span>', "#c0392b", "Not yet rotated")
        return format_html('<span style="color:{};">{}</span>', "#27ae60", "Rotated")

    @admin.display(description="Active farms", ordering="_active_membership_count")
    def membership_count(self, obj):
        return getattr(obj, "_active_membership_count", 0)

    @admin.display(description="Member of")
    def member_of(self, obj):
        memberships = getattr(obj, "_active_memberships", None)
        if memberships is None:
            memberships = list(
                obj.farm_memberships.filter(is_active=True).select_related("farm")
            )
        if not memberships:
            return format_html('<span style="color:{};">{}</span>', "#7f8c8d", "— none —")
        return ", ".join(
            f"{m.farm.name} ({m.get_role_display()})" for m in memberships
        )


class FarmOwnerCreationForm(forms.ModelForm):
    """
    The add form for the owner funnel: identity only, no password. The PIN is
    generated in ``issue_owner_account`` at save time.
    """

    class Meta:
        model = FarmOwnerAccount
        fields = ["full_name", "phone_number", "email"]

    def clean_phone_number(self):
        # Match the rest of the system, which stores E.164 with display
        # formatting stripped. Normalising here also means the ModelForm's
        # uniqueness check runs against the value we will actually save.
        return User.objects.normalize_phone(self.cleaned_data.get("phone_number", ""))


@admin.register(FarmOwnerAccount)
class FarmOwnerAccountAdmin(admin.ModelAdmin):
    """
    "Create Farm Owner" — issues the login PIN automatically.

    Add-only. The changelist is a read-only funnel scoped to OWNER accounts;
    real edits and deactivation happen under Accounts → Users. Mirrors the
    invitation flow: system PIN, shown once, forced rotation on first login,
    and no farm/membership so the owner lands in the setup wizard.
    """

    form = FarmOwnerCreationForm
    fields = ["full_name", "phone_number", "email"]

    list_display = [
        "full_name",
        "phone_number",
        "credential_state",
        "in_setup_wizard",
        "date_joined",
    ]
    search_fields = ["full_name", "phone_number", "email"]
    ordering = ["full_name"]
    date_hierarchy = "date_joined"

    # --- scope + annotations -------------------------------------------------

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(role=User.Role.OWNER)
            .annotate(
                _active_membership_count=Count(
                    "farm_memberships",
                    filter=Q(farm_memberships__is_active=True),
                    distinct=True,
                )
            )
        )

    @admin.display(description="Credential")
    def credential_state(self, obj):
        if obj.must_change_credential:
            return format_html(
                '<span style="color:{};">{}</span>', "#c0392b", "Not yet rotated"
            )
        return format_html('<span style="color:{};">{}</span>', "#27ae60", "Rotated")

    @admin.display(description="In setup wizard", boolean=True)
    def in_setup_wizard(self, obj):
        return getattr(obj, "_active_membership_count", 0) == 0

    # --- creation ----------------------------------------------------------

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)

        user, raw_pin = issue_owner_account(
            full_name=form.cleaned_data["full_name"],
            phone_number=form.cleaned_data["phone_number"],
            email=form.cleaned_data.get("email") or None,
        )
        # Point the admin's in-memory object at the row we just created so the
        # post-save redirect, log entry, and success message resolve. Nothing
        # else writes this instance.
        obj.pk = user.pk
        request._issued_owner_pin = raw_pin

    def response_add(self, request, obj, post_url_continue=None):
        pin = getattr(request, "_issued_owner_pin", None)
        if pin:
            self.message_user(
                request,
                f"One-time PIN for {obj.full_name}: {pin}  —  give it to the "
                f"owner now. It is not stored and cannot be shown again; they "
                f"must change it on first login.",
                level=messages.WARNING,
            )
        return super().response_add(request, obj, post_url_continue)

    # --- permissions -----------------------------------------------------

    def has_add_permission(self, request):
        # Track the same permission that governs adding a User.
        return request.user.has_perm("accounts.add_user")

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("accounts.add_user")

    def has_module_permission(self, request):
        return request.user.has_perm("accounts.add_user")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    list_select_related = ["farm"]
    date_hierarchy = "created_at"
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
