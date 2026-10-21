from django.contrib import admin
from unfold.admin import ModelAdmin

from accounts.models import mask_phone

from .models import SmsLog


@admin.register(SmsLog)
class SmsLogAdmin(ModelAdmin):
    """Read-only — this table is an audit trail, not something to hand-edit."""

    list_display = ("sent_at", "purpose", "status", "masked_recipient", "related_farm")
    list_filter = ("status", "purpose")
    # Search runs against the real field, never the masked display — an
    # admin typing the exact number still finds the message.
    search_fields = ("recipient",)
    date_hierarchy = "sent_at"
    readonly_fields = [f.name for f in SmsLog._meta.fields]

    @admin.display(description="Recipient")
    def masked_recipient(self, obj):
        return mask_phone(obj.recipient)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
