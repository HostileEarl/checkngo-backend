"""
Groups the admin index and nav sidebar into CheckN Go hubs.

Labels and grouping only. Every model entry is the dict Django's own
AdminSite.get_app_list builds, so URLs and permission checks (including
ModelAdmin overrides such as FarmOwnerAccountAdmin.has_add_permission) are
Django's, unchanged. A model the user may not see is simply absent from
Django's list, so it is absent from its hub too; a hub left empty is
dropped.

The per-app page (/admin/<app_label>/) still gets Django's default list,
so those URLs keep working. Any registered model not named below lands in
System, so nothing disappears when a model is added.

Installed from checkngo/urls.py with `install(admin.site)`, before
`admin.site.urls` is read.
"""

from django.urls import reverse

# (hub id, title, [(app_label, ModelName), ...]) — order is display order.
HUBS = [
    (
        "people",
        "People",
        [
            ("accounts", "FarmOwnerAccount"),
            ("accounts", "User"),
            ("accounts", "Invitation"),
            ("farms", "FarmMembership"),
            ("partners", "FarmPartnerLink"),
        ],
    ),
    (
        "farms",
        "Farms",
        [
            ("farms", "Farm"),
            ("production", "House"),
            ("farms", "FarmOwnershipHistory"),
        ],
    ),
    (
        "birds-sales",
        "Birds & Sales",
        [
            ("production", "Batch"),
            ("production", "DailyRecord"),
            ("production", "WeightSample"),
            ("production", "RecordCorrection"),
            ("production", "SaleEvent"),
            ("production", "Harvest"),
        ],
    ),
    (
        "resources",
        "Resources",
        [
            ("production", "FeedDelivery"),
            ("production", "InventoryItem"),
            ("production", "InventoryStockIn"),
            ("production", "InventoryUsageLog"),
            ("production", "TaskTemplate"),
            ("production", "TaskCompletion"),
        ],
    ),
    (
        "system",
        "System",
        [
            ("notifications", "SmsLog"),
            ("axes", "AccessAttempt"),
            ("axes", "AccessFailureLog"),
            ("axes", "AccessLog"),
            ("token_blacklist", "OutstandingToken"),
            ("token_blacklist", "BlacklistedToken"),
            ("auth", "Group"),
        ],
    ),
]

FALLBACK_HUB = "system"

# Display names changed here, not in model Meta, so no migration.
NAME_OVERRIDES = {
    ("partners", "FarmPartnerLink"): "Buyer links",
    ("production", "FeedDelivery"): "Feed deliveries",  # Django: "Feed deliverys"
}


def _models_by_key(app_list):
    """{(app_label, ModelName): model dict} from Django's app list."""
    return {
        (app["app_label"], model["object_name"]): model
        for app in app_list
        for model in app["models"]
    }


def build_hubs(app_list, index_url):
    """Regroup Django's app list into the hubs above."""
    models = _models_by_key(app_list)
    placed = set()
    hubs = []

    for hub_id, title, keys in HUBS:
        entries = []
        for key in keys:
            if key in models:
                entries.append(key)
                placed.add(key)
        hubs.append((hub_id, title, entries))

    # Anything registered but not listed goes to System, in Django's order.
    leftovers = [key for key in models if key not in placed]
    for hub_id, _title, entries in hubs:
        if hub_id == FALLBACK_HUB:
            entries.extend(leftovers)

    result = []
    for hub_id, title, entries in hubs:
        if not entries:
            continue
        hub_models = []
        for key in entries:
            model = models[key]
            if key in NAME_OVERRIDES:
                model = {**model, "name": NAME_OVERRIDES[key]}
            hub_models.append(model)
        result.append(
            {
                "name": title,
                "app_label": f"hub-{hub_id}",
                # The caption links to the hub's own section on the index.
                # A fragment never appears in request.path, so app_list.html's
                # "current-app" test stays false instead of matching every hub.
                "app_url": f"{index_url}#hub-{hub_id}",
                "has_module_perms": True,
                "models": hub_models,
                "is_system": hub_id == "system",
            }
        )
    return result


def install(site):
    """Wrap `site.get_app_list` (hubs) and `site.index` (quick actions)."""
    django_get_app_list = site.get_app_list
    django_index = site.index

    def get_app_list(request, app_label=None):
        if app_label is not None:
            return django_get_app_list(request, app_label)
        return build_hubs(
            django_get_app_list(request),
            reverse("admin:index", current_app=site.name),
        )

    def index(request, extra_context=None):
        # The ModelAdmins decide: FarmOwnerAccount's add permission is
        # accounts.add_user, not add_farmowneraccount, so the template's
        # `perms` would get it wrong. Django's model dicts already carry
        # add_url / admin_url only when the ModelAdmin allows it.
        models = _models_by_key(django_get_app_list(request))
        owner = models.get(("accounts", "FarmOwnerAccount"), {})
        invitation = models.get(("accounts", "Invitation"), {})
        context = {
            "can_add_owner_account": bool(owner.get("add_url")),
            "can_view_invitations": bool(invitation.get("admin_url")),
            **(extra_context or {}),
        }
        return django_index(request, extra_context=context)

    site.get_app_list = get_app_list
    site.index = index
