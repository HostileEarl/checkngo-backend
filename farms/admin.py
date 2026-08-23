# farms/admin.py
from django.contrib import admin

from .models import Farm, FarmMembership, FarmOwnershipHistory


class FarmMembershipInline(admin.TabularInline):
    model = FarmMembership
    extra = 0
    fields = ["user", "role", "is_active", "joined_at", "invited_by"]
    readonly_fields = ["joined_at", "invited_by"]
    fk_name = "farm"


@admin.register(Farm)
class FarmAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "municipality", "province", "is_active", "created_at"]
    list_filter = ["is_active", "province"]
    search_fields = ["name", "owner__full_name"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [FarmMembershipInline]


@admin.register(FarmMembership)
class FarmMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "farm", "role", "is_active", "joined_at", "invited_by"]
    list_filter = ["role", "is_active", "farm"]
    search_fields = ["user__full_name", "user__phone_number", "farm__name"]
    readonly_fields = ["joined_at", "deactivated_at"]
    autocomplete_fields = ["user", "farm", "invited_by"]


@admin.register(FarmOwnershipHistory)
class FarmOwnershipHistoryAdmin(admin.ModelAdmin):
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
    readonly_fields = [f.name for f in FarmOwnershipHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False