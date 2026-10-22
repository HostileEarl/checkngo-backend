# accounts/admin.py
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.contrib.admin.options import ActionLocation
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import AccessRequest, FarmOwnerAccount, Invitation, User, mask_phone
from .services import issue_owner_account


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
class UserAdmin(BaseUserAdmin, ModelAdmin):
    """
    The stock UserAdmin references 'username' and crashes on this model.
    This one is keyed on phone_number throughout.
    """

    # Unfold's styled versions of Django's auth forms.
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    ordering = ["full_name"]
    list_display = [
        "full_name",
        "masked_phone_number",
        "role",
        "credential_status",
        "membership_count",
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
    # Search runs against the real field, never the masked display — an
    # admin typing the exact number still finds the person. Email stays
    # searchable (not displayed anywhere — see masked_phone_number and the
    # fieldsets below) since a search box reveals nothing by itself.
    search_fields = ["full_name", "phone_number", "email"]
    readonly_fields = ["date_joined", "last_login"]
    date_hierarchy = "date_joined"

    # Account maintenance only: identity, role, and the permission flags.
    # No email (personal detail) and no farm data (memberships, credential
    # rotation timestamps) — see this app's docstring for why.
    fieldsets = (
        (None, {"fields": ("phone_number", "password")}),
        ("Identity", {"fields": ("full_name", "role")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser")}),
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
        return super().get_queryset(request).annotate(
            _active_membership_count=Count(
                "farm_memberships",
                filter=Q(farm_memberships__is_active=True),
                distinct=True,
            )
        )

    @admin.display(description="Phone number")
    def masked_phone_number(self, obj):
        return mask_phone(obj.phone_number)

    @admin.display(description="Credential")
    def credential_status(self, obj):
        if obj.must_change_credential:
            return format_html('<span class="{}">{}</span>', "status-danger", "Not yet rotated")
        return format_html('<span class="{}">{}</span>', "status-success", "Rotated")

    @admin.display(description="Active farms", ordering="_active_membership_count")
    def membership_count(self, obj):
        """A bare count — which farms, not shown here (see Farm memberships)."""
        return getattr(obj, "_active_membership_count", 0)


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
class FarmOwnerAccountAdmin(ModelAdmin):
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
        "masked_phone_number",
        "credential_state",
        "in_setup_wizard",
        "date_joined",
    ]
    search_fields = ["full_name", "phone_number", "email"]
    ordering = ["full_name"]
    date_hierarchy = "date_joined"

    @admin.display(description="Phone number")
    def masked_phone_number(self, obj):
        return mask_phone(obj.phone_number)

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
                '<span class="{}">{}</span>', "status-danger", "Not yet rotated"
            )
        return format_html('<span class="{}">{}</span>', "status-success", "Rotated")

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


class EffectiveStatusFilter(admin.SimpleListFilter):
    """
    Filter on the status a person would actually see: a PENDING row past
    expires_at counts as Expired, not Pending, even before the daily
    expire_invitations job has rewritten it.
    """

    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return Invitation.Status.choices

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        now = timezone.now()
        overdue = Q(status=Invitation.Status.PENDING, expires_at__lte=now)
        if value == Invitation.Status.PENDING:
            return queryset.filter(status=value, expires_at__gt=now)
        if value == Invitation.Status.EXPIRED:
            return queryset.filter(Q(status=value) | overdue)
        return queryset.filter(status=value)


# Same pill classes as the other admin status columns (checkngo-admin.css).
INVITATION_STATUS_PILL = {
    Invitation.Status.PENDING: "status-warning",
    Invitation.Status.ACCEPTED: "status-success",
    Invitation.Status.REVOKED: "status-danger",
    Invitation.Status.EXPIRED: "status-muted",
}


@admin.register(Invitation)
class InvitationAdmin(ModelAdmin):
    """Read-only. An invitation is a credential handoff record, not a form."""

    list_display = [
        "full_name",
        "masked_phone_number",
        "farm",
        "account_role",
        "effective_status",
        "created_at",
        "expires_at",
    ]
    list_filter = [EffectiveStatusFilter, "account_role", "farm"]
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

    @admin.display(description="Phone number")
    def masked_phone_number(self, obj):
        return mask_phone(obj.phone_number)

    @admin.display(description="Status", ordering="status")
    def effective_status(self, obj):
        status = Invitation.Status(obj.effective_status)
        return format_html(
            '<span class="{}">{}</span>', INVITATION_STATUS_PILL[status], status.label
        )

    @admin.action(description="Revoke selected pending invitations")
    def revoke_invitations(self, request, queryset):
        count = 0
        for invitation in queryset.filter(status=Invitation.Status.PENDING):
            invitation.revoke()
            count += 1
        self.message_user(request, f"{count} invitation(s) revoked.")


ACCESS_REQUEST_STATUS_PILL = {
    AccessRequest.Status.NEW: "status-warning",
    AccessRequest.Status.CONTACTED: "status-muted",
    AccessRequest.Status.APPROVED: "status-success",
    AccessRequest.Status.DECLINED: "status-danger",
}


@admin.register(AccessRequest)
class AccessRequestAdmin(ModelAdmin):
    """
    Landing-page "Request access" submissions. Read-only apart from the
    status actions: what the requester typed is kept as typed.

    The list masks the phone number and leaves out the email, like every
    other changelist. The detail page shows both in full — it exists so
    the administrator can contact the person.
    """

    list_display = [
        "farm_name",
        "full_name",
        "masked_phone_number",
        "location",
        "role",
        "status_pill",
        "created_at",
        "handled_at",
    ]
    list_filter = ["status", "created_at", "role"]
    search_fields = ["full_name", "farm_name", "location", "phone_number"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
    readonly_fields = [
        "full_name",
        "phone_number",
        "email",
        "farm_name",
        "location",
        "houses",
        "role",
        "message",
        "consent",
        "status",
        "created_at",
        "handled_at",
    ]
    actions = ["mark_contacted", "mark_approved", "mark_declined", "delete_handled"]

    def has_add_permission(self, request):
        # Requests only ever come from the public form.
        return False

    def has_delete_permission(self, request, obj=None):
        # Retention: a handled request may be deleted; a NEW one may not,
        # so nobody loses a request before it has been answered.
        if obj is not None and obj.handled_at is None:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request, action_location=ActionLocation.CHANGE_LIST):
        # Replaced by delete_handled, which skips NEW requests.
        actions = super().get_actions(request, action_location)
        actions.pop("delete_selected", None)
        return actions

    @admin.display(description="Phone number")
    def masked_phone_number(self, obj):
        return mask_phone(obj.phone_number)

    @admin.display(description="Status", ordering="status")
    def status_pill(self, obj):
        status = AccessRequest.Status(obj.status)
        return format_html(
            '<span class="{}">{}</span>', ACCESS_REQUEST_STATUS_PILL[status], status.label
        )

    def _mark(self, request, queryset, status):
        count = queryset.update(status=status, handled_at=timezone.now())
        self.message_user(
            request, f"{count} request(s) marked {AccessRequest.Status(status).label.lower()}."
        )

    @admin.action(description="Mark contacted", permissions=["change"])
    def mark_contacted(self, request, queryset):
        self._mark(request, queryset, AccessRequest.Status.CONTACTED)

    @admin.action(description="Mark approved", permissions=["change"])
    def mark_approved(self, request, queryset):
        self._mark(request, queryset, AccessRequest.Status.APPROVED)

    @admin.action(description="Mark declined", permissions=["change"])
    def mark_declined(self, request, queryset):
        self._mark(request, queryset, AccessRequest.Status.DECLINED)

    @admin.action(description="Delete selected handled requests", permissions=["delete"])
    def delete_handled(self, request, queryset):
        handled = queryset.exclude(handled_at=None)
        kept = queryset.count() - handled.count()
        if kept:
            self.message_user(
                request,
                f"{kept} new request(s) kept — mark them handled before deleting.",
                messages.WARNING,
            )
        if not handled.exists():
            return None
        # Django's own confirm-then-delete flow, on the handled rows only.
        return delete_selected(self, request, handled)


# --- third-party admins, re-registered with the Unfold theme -------------------
# Django's Group and the axes / simplejwt models register plain ModelAdmins.
# Re-register them on Unfold's base so every page in the admin looks the same.
# Same list columns, filters and permissions — only the base class changes.
# This file loads after auth, axes and token_blacklist (INSTALLED_APPS order).

from axes.admin import AccessAttemptAdmin, AccessFailureLogAdmin, AccessLogAdmin  # noqa: E402
from axes.models import AccessAttempt, AccessFailureLog, AccessLog  # noqa: E402
from rest_framework_simplejwt.token_blacklist.admin import (  # noqa: E402
    BlacklistedTokenAdmin,
    OutstandingTokenAdmin,
)
from rest_framework_simplejwt.token_blacklist.models import (  # noqa: E402
    BlacklistedToken,
    OutstandingToken,
)


def _retheme(model, base_admin):
    if not admin.site.is_registered(model):
        return
    admin.site.unregister(model)
    themed = type(f"Themed{base_admin.__name__}", (base_admin, ModelAdmin), {})
    admin.site.register(model, themed)


_retheme(Group, BaseGroupAdmin)
_retheme(AccessAttempt, AccessAttemptAdmin)
_retheme(AccessLog, AccessLogAdmin)
_retheme(AccessFailureLog, AccessFailureLogAdmin)
_retheme(OutstandingToken, OutstandingTokenAdmin)
_retheme(BlacklistedToken, BlacklistedTokenAdmin)
