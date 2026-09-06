# farms/admin.py
from django.contrib import admin

from production.models import Batch

from .models import Farm, FarmMembership, FarmOwnershipHistory


class FarmMembershipInline(admin.TabularInline):
    model = FarmMembership
    extra = 0
    fields = ["user", "role", "is_active", "joined_at", "invited_by"]
    readonly_fields = ["joined_at", "invited_by"]
    fk_name = "farm"
    autocomplete_fields = ["user"]


class FarmOwnershipHistoryInline(admin.TabularInline):
    """
    Read-only ownership ledger, shown on the farm so the audit trail does
    not require a separate page. The rows are immutable at the model level;
    this inline reflects that rather than offering an edit form that 500s.
    """

    model = FarmOwnershipHistory
    extra = 0
    can_delete = False
    verbose_name_plural = "Ownership history (read-only)"
    fields = [
        "from_owner_name",
        "to_owner_name",
        "transferred_at",
        "performed_by",
        "note",
    ]
    readonly_fields = fields
    ordering = ["-transferred_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Farm)
class FarmAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "owner",
        "municipality",
        "province",
        "house_count",
        "active_batch_count",
        "birds_alive",
        "is_active",
        "created_at",
    ]
    list_filter = ["is_active", "province"]
    search_fields = ["name", "owner__full_name"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    inlines = [FarmMembershipInline, FarmOwnershipHistoryInline]

    def get_queryset(self, request):
        # house_count / active_batch_count / birds_alive all walk
        # houses -> batches. Prefetching that once keeps the changelist at a
        # fixed query count instead of an N+1 per farm; the alternative of
        # annotating both counts in one query multiplies the joined rows.
        return (
            super()
            .get_queryset(request)
            .select_related("owner")
            .prefetch_related("houses__batches")
        )

    @admin.display(description="Houses")
    def house_count(self, obj):
        return len(obj.houses.all())

    def _active_batches(self, obj):
        return [
            batch
            for house in obj.houses.all()
            for batch in house.batches.all()
            if batch.status == Batch.Status.ACTIVE
        ]

    @admin.display(description="Active batches")
    def active_batch_count(self, obj):
        return len(self._active_batches(obj))

    @admin.display(description="Birds alive")
    def birds_alive(self, obj):
        return sum(batch.current_bird_count for batch in self._active_batches(obj))


@admin.register(FarmMembership)
class FarmMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "farm", "role", "is_active", "joined_at", "invited_by"]
    list_filter = ["role", "is_active", "farm"]
    search_fields = ["user__full_name", "user__phone_number", "farm__name"]
    readonly_fields = ["joined_at", "deactivated_at"]
    autocomplete_fields = ["user", "farm", "invited_by"]
    list_select_related = ["user", "farm", "invited_by"]
    date_hierarchy = "joined_at"


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
    list_select_related = ["farm", "performed_by"]
    date_hierarchy = "transferred_at"
    readonly_fields = [f.name for f in FarmOwnershipHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
