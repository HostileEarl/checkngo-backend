# partners/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import FarmPartnerLink


@admin.register(FarmPartnerLink)
class FarmPartnerLinkAdmin(ModelAdmin):
    """Read-only. A buyer link is created/archived/restored through the API."""

    list_display = [
        "display_name",
        "farm",
        "link_type",
        "is_active",
        "linked_at",
        "linked_by",
    ]
    list_filter = ["link_type", "is_active", "farm"]
    search_fields = ["business_name", "partner__full_name", "partner__phone_number"]
    readonly_fields = ["linked_at", "deactivated_at"]
    autocomplete_fields = ["partner", "farm", "linked_by"]
    list_select_related = ["partner", "farm", "linked_by"]
    date_hierarchy = "linked_at"

    @admin.display(description="Partner")
    def display_name(self, obj):
        return obj.business_name or obj.partner.full_name

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False