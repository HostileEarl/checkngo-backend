# farms/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Farm, FarmMembership, FarmOwnershipHistory


class FarmMembershipInline(TabularInline):
    """Who works here and in what role — nothing operational. Read-only."""

    model = FarmMembership
    extra = 0
    can_delete = False
    verbose_name_plural = "Memberships (read-only — manage from Farm memberships)"
    fields = ["user", "role"]
    readonly_fields = fields
    fk_name = "farm"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Farm)
class FarmAdmin(ModelAdmin):
    """
    Identity only — no owner, address, or production/business figures.
    Ownership history has its own page (Farms → Ownership history);
    operational data (houses, batches, sales...) has no admin page at
    all — see production/admin.py.
    """

    list_display = ["name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    # Owner, address, municipality and province are deliberately absent —
    # not personal data, but operational/business detail this page does
    # not surface. Changing the owner goes through the ownership-transfer
    # flow (farms.models.FarmOwnershipHistory), not a plain field edit.
    fields = ["name", "is_active", "created_at", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    inlines = [FarmMembershipInline]

    def has_add_permission(self, request):
        # A farm is created through the owner's setup wizard (the "owner"
        # field above is required and deliberately not on this form) —
        # never by typing a bare name into the admin.
        return False

    def get_urls(self):
        # System → Evaluation farms (superusers only): the guided-test farms.
        # Ahead of the stock URLs, whose <object_id>/ pattern would match first.
        from .evaluation_admin import evaluation_urls

        return evaluation_urls(self) + super().get_urls()


@admin.register(FarmMembership)
class FarmMembershipAdmin(ModelAdmin):
    list_display = ["user", "farm", "role", "is_active", "joined_at", "invited_by"]
    list_filter = ["role", "is_active", "farm"]
    search_fields = ["user__full_name", "user__phone_number", "farm__name"]
    readonly_fields = ["joined_at", "deactivated_at"]
    autocomplete_fields = ["user", "farm", "invited_by"]
    list_select_related = ["user", "farm", "invited_by"]
    date_hierarchy = "joined_at"


@admin.register(FarmOwnershipHistory)
class FarmOwnershipHistoryAdmin(ModelAdmin):
    """
    Fully read-only. The model's save() raises on any update — an audit
    record that can be edited is not evidence. The admin reflects that
    rather than letting a user discover it as a 500.
    """

    list_display = [
        "farm",
        "from_owner_name",
        "to_owner_name",
        "transferred_at",
        "performed_by",
    ]
    list_filter = ["farm", "transferred_at"]
    search_fields = ["farm__name", "from_owner_name", "to_owner_name"]
    list_select_related = ["farm", "performed_by"]
    date_hierarchy = "transferred_at"
    readonly_fields = [f.name for f in FarmOwnershipHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
