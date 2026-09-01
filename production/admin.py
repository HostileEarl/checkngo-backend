# production/admin.py
from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
    RecordCorrection,
    WeightSample,
)


@admin.register(House)
class HouseAdmin(admin.ModelAdmin):
    list_display = ["name", "farm", "capacity", "occupancy", "is_active"]
    list_filter = ["farm", "is_active"]
    search_fields = ["name", "farm__name"]

    @admin.display(description="Current batch")
    def occupancy(self, obj):
        batch = obj.current_batch
        if batch:
            return format_html('<span style="color:#c0392b;">{}</span>', batch.batch_code)
        return format_html('<span style="color:#27ae60;">Empty</span>')


class DailyRecordInline(admin.TabularInline):
    model = DailyRecord
    extra = 0
    max_num = 0  # view-only; entry belongs in the app
    fields = [
        "record_date",
        "mortality_disease",
        "mortality_heat",
        "mortality_culled",
        "mortality_unknown",
        "feed_kg",
        "recorded_by",
    ]
    readonly_fields = fields
    ordering = ["-record_date"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = [
        "batch_code",
        "house",
        "status",
        "initial_bird_count",
        "birds_alive",
        "mortality_pct",
        "fcr_display",
        "start_date",
    ]
    list_filter = ["status", "house__farm", "house"]
    search_fields = ["batch_code", "breed"]
    readonly_fields = [
        "id",
        "total_mortality",
        "total_feed_kg",
        "terminated_at",
        "terminated_by",
        "created_at",
    ]
    inlines = [DailyRecordInline]
    actions = ["recalculate_totals_action"]

    fieldsets = (
        (None, {"fields": ("id", "house", "batch_code", "breed", "status")}),
        ("Placement", {"fields": ("initial_bird_count", "start_date", "expected_harvest_date")}),
        (
            "Running totals",
            {
                "fields": ("total_mortality", "total_feed_kg"),
                "description": (
                    "Denormalized for chart performance. The daily records are "
                    "the source of truth — use the recalculate action if these "
                    "ever look wrong."
                ),
            },
        ),
        ("Termination", {"fields": ("termination_reason", "terminated_at", "terminated_by")}),
        ("Meta", {"fields": ("created_by", "created_at")}),
    )

    @admin.display(description="Alive")
    def birds_alive(self, obj):
        return obj.current_bird_count

    @admin.display(description="Mortality")
    def mortality_pct(self, obj):
        rate = obj.mortality_rate
        colour = "#27ae60" if rate < 5 else "#e67e22" if rate < 8 else "#c0392b"
        return format_html('<span style="color:{};">{}%</span>', colour, f"{rate:.2f}")

    @admin.display(description="FCR")
    def fcr_display(self, obj):
        fcr = obj.feed_conversion_ratio
        return str(fcr) if fcr is not None else "—"

    @admin.action(description="Recalculate totals from daily records")
    def recalculate_totals_action(self, request, queryset):
        for batch in queryset:
            batch.recalculate_totals()
        self.message_user(request, f"Recalculated {queryset.count()} batch(es).")


@admin.register(DailyRecord)
class DailyRecordAdmin(admin.ModelAdmin):
    """
    Editable only inside the 24-hour window, matching the API.

    The admin does not run serializers, so without this override it would be
    a bypass around the lock the whole audit trail depends on.
    """

    list_display = [
        "record_date",
        "batch",
        "mortality_total",
        "feed_kg",
        "recorded_by",
        "lock_state",
    ]
    list_filter = ["batch__house__farm", "batch", "record_date"]
    date_hierarchy = "record_date"
    readonly_fields = ["id", "created_at", "updated_at", "sync_delay_seconds"]
    ordering = ["-record_date"]

    @admin.display(description="Deaths")
    def mortality_total(self, obj):
        return obj.mortality

    @admin.display(description="Editable")
    def lock_state(self, obj):
        if obj.is_editable:
            return format_html('<span style="color:#e67e22;">Open</span>')
        return format_html('<span style="color:#7f8c8d;">Locked</span>')

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.is_editable

    def has_delete_permission(self, request, obj=None):
        # Recorded days are not removable. Same rule as the API.
        return False


@admin.register(WeightSample)
class WeightSampleAdmin(admin.ModelAdmin):
    list_display = ["sample_date", "batch", "average_grams", "birds_weighed", "recorded_by"]
    list_filter = ["batch__house__farm", "batch"]
    readonly_fields = ["id", "created_at", "updated_at"]

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.is_editable

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Harvest)
class HarvestAdmin(admin.ModelAdmin):
    """Read-only. A harvest closes a batch and fixes its FCR permanently."""

    list_display = [
        "batch",
        "harvest_date",
        "birds_harvested",
        "total_weight_kg",
        "revenue",
        "fcr_display",
    ]
    list_filter = ["harvest_date", "batch__house__farm"]
    readonly_fields = [f.name for f in Harvest._meta.fields]

    @admin.display(description="FCR")
    def fcr_display(self, obj):
        fcr = obj.batch.feed_conversion_ratio
        return str(fcr) if fcr is not None else "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FeedDelivery)
class FeedDeliveryAdmin(admin.ModelAdmin):
    list_display = [
        "delivery_date",
        "farm",
        "feed_type",
        "quantity_kg",
        "unit_cost",
        "total_cost",
        "supplier_link",
    ]
    list_filter = ["feed_type", "farm", "delivery_date"]
    search_fields = ["invoice_ref"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "delivery_date"
    
class InventoryStockInInline(admin.TabularInline):
    model = InventoryStockIn
    extra = 0
    fields = ["stock_in_date", "quantity", "note", "recorded_by"]
    readonly_fields = ["recorded_by"]
    ordering = ["-stock_in_date"]


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ["name", "farm", "unit", "low_stock_threshold", "level", "is_active"]
    list_filter = ["farm", "is_active"]
    search_fields = ["name", "farm__name"]
    readonly_fields = ["created_by", "created_at"]
    inlines = [InventoryStockInInline]

    @admin.display(description="On hand")
    def level(self, obj):
        qty = obj.current_quantity
        colour = "#c0392b" if qty <= 0 else "#e67e22" if qty <= obj.low_stock_threshold else "#27ae60"
        return format_html('<span style="color:{};">{} {}</span>', colour, qty, obj.unit)


@admin.register(InventoryStockIn)
class InventoryStockInAdmin(admin.ModelAdmin):
    list_display = ["stock_in_date", "item", "quantity", "recorded_by"]
    list_filter = ["item__farm", "item", "stock_in_date"]
    date_hierarchy = "stock_in_date"
    readonly_fields = ["created_at"]


@admin.register(InventoryUsageLog)
class InventoryUsageLogAdmin(admin.ModelAdmin):
    """Editable only inside the 24-hour window, matching the API and DailyRecordAdmin."""

    list_display = ["usage_date", "item", "quantity_used", "recorded_by", "lock_state"]
    list_filter = ["item__farm", "item", "usage_date"]
    date_hierarchy = "usage_date"
    readonly_fields = ["id", "created_at", "updated_at", "sync_delay_seconds"]
    ordering = ["-usage_date"]

    @admin.display(description="Editable")
    def lock_state(self, obj):
        if obj.is_editable:
            return format_html('<span style="color:#e67e22;">Open</span>')
        return format_html('<span style="color:#7f8c8d;">Locked</span>')

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.is_editable

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RecordCorrection)
class RecordCorrectionAdmin(admin.ModelAdmin):
    """Fully read-only. The model's save() raises on update."""

    list_display = [
        "record_date",
        "batch",
        "changed_summary",
        "corrected_by_name",
        "corrected_at",
    ]
    list_filter = ["batch__house__farm", "batch", "corrected_at"]
    search_fields = ["reason", "corrected_by_name", "batch__batch_code"]
    readonly_fields = [f.name for f in RecordCorrection._meta.fields]
    date_hierarchy = "corrected_at"

    @admin.display(description="Fields changed")
    def changed_summary(self, obj):
        return ", ".join(obj.changed_fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False