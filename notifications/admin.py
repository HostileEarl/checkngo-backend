from django.contrib import admin

from .models import SmsLog


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    """Read-only — this table is an audit trail, not something to hand-edit."""

    list_display = ("sent_at", "purpose", "status", "recipient", "related_farm")
    list_filter = ("status", "purpose")
    search_fields = ("recipient",)
    date_hierarchy = "sent_at"
    readonly_fields = [f.name for f in SmsLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
