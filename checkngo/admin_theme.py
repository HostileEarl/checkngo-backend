"""
CheckN Go admin theme — django-unfold configuration.

Everything Unfold needs that isn't a plain setting lives here, so
settings.py only holds the UNFOLD dict:

- COLORS: the app's palette as Unfold colour scales. Unfold paints its
  buttons, active nav and focus rings with primary-600 and white text, so
  primary-600 is the olive (#4F6334, 6.6:1 with white). The lime
  (#C4F188) sits at primary-300 and is used for the main Save button and
  highlights through checkngo-admin.css, always with ink text.
- SIDEBAR_NAVIGATION: the same hubs the old admin_hubs.py built (People,
  Farms, Birds & Sales, Resources, System), now as Unfold's sidebar.
  Each link is hidden unless the user may view that model.
- dashboard_callback: the numbers and quick actions on the admin home.

Nothing here touches a model at import time: settings.py imports this
module, so model imports stay inside the functions.
"""

from django.templatetags.static import static
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

# --- palette ---------------------------------------------------------------

# Olive scale with the brand lime at 300. 600 = secondary #4F6334.
PRIMARY = {
    "50": "#F6FAEE",
    "100": "#EDF6DD",
    "200": "#DDF2BE",
    "300": "#C4F188",  # brand lime (primary in the app)
    "400": "#9CC567",
    "500": "#6F8A4A",
    "600": "#4F6334",  # brand olive (secondary in the app)
    "700": "#43552C",
    "800": "#374624",
    "900": "#2A361C",
    "950": "#1A2211",
}

# Warm neutrals around the app's ink #1A1614 and background #F4F4F4.
BASE = {
    "50": "#F9F9F8",
    "100": "#F4F4F4",  # app background
    "200": "#E4E4E4",
    "300": "#D3D1CF",
    "400": "#A8A4A1",
    "500": "#7A7572",
    "600": "#5E5A57",  # ink muted
    "700": "#45413E",
    "800": "#2E2A28",
    "900": "#1A1614",  # ink
    "950": "#0F0D0C",
}


# --- permissions -------------------------------------------------------------


def can_view(app_label, model_name):
    """Sidebar permission callback: show a link only if the model is viewable."""

    def check(request):
        user = request.user
        return user.has_perm(f"{app_label}.view_{model_name}") or user.has_perm(
            f"{app_label}.change_{model_name}"
        )

    return check


def can_create_owner(request):
    # Mirrors FarmOwnerAccountAdmin: governed by accounts.add_user.
    return request.user.has_perm("accounts.add_user")


def _item(title, icon, app_label, model_name, permission=None):
    return {
        "title": title,
        "icon": icon,
        "link": reverse_lazy(f"admin:{app_label}_{model_name}_changelist"),
        "permission": permission or can_view(app_label, model_name),
    }


# --- sidebar -----------------------------------------------------------------

SIDEBAR_NAVIGATION = [
    {
        "title": _("Home"),
        "items": [
            {
                "title": _("Dashboard"),
                "icon": "space_dashboard",
                "link": reverse_lazy("admin:index"),
            },
        ],
    },
    {
        "title": _("People"),
        "separator": True,
        "collapsible": False,
        "items": [
            _item(_("Farm owner accounts"), "badge", "accounts", "farmowneraccount", can_create_owner),
            _item(_("Users"), "person", "accounts", "user"),
            {
                **_item(_("Invitations"), "mail", "accounts", "invitation"),
                "badge": "checkngo.admin_theme.pending_invitations_badge",
            },
            _item(_("Farm memberships"), "group", "farms", "farmmembership"),
            _item(_("Buyer links"), "handshake", "partners", "farmpartnerlink"),
        ],
    },
    {
        "title": _("Farms"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Farms"), "agriculture", "farms", "farm"),
            _item(_("Houses"), "warehouse", "production", "house"),
            _item(_("Ownership history"), "history", "farms", "farmownershiphistory"),
        ],
    },
    {
        "title": _("Birds & Sales"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Batches"), "egg", "production", "batch"),
            _item(_("Daily records"), "edit_calendar", "production", "dailyrecord"),
            _item(_("Weight samples"), "scale", "production", "weightsample"),
            _item(_("Record corrections"), "rule", "production", "recordcorrection"),
            _item(_("Sales"), "sell", "production", "saleevent"),
            _item(_("Harvests"), "inventory", "production", "harvest"),
        ],
    },
    {
        "title": _("Resources"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Feed deliveries"), "local_shipping", "production", "feeddelivery"),
            _item(_("Inventory items"), "inventory_2", "production", "inventoryitem"),
            _item(_("Stock in"), "move_to_inbox", "production", "inventorystockin"),
            _item(_("Usage logs"), "outbox", "production", "inventoryusagelog"),
            _item(_("Routine tasks"), "checklist", "production", "tasktemplate"),
            _item(_("Task completions"), "task_alt", "production", "taskcompletion"),
        ],
    },
    {
        "title": _("System"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("SMS log"), "sms", "notifications", "smslog"),
            _item(_("Login attempts"), "lock", "axes", "accessattempt"),
            _item(_("Login failures"), "gpp_bad", "axes", "accessfailurelog"),
            _item(_("Access log"), "list_alt", "axes", "accesslog"),
            _item(_("Issued tokens"), "key", "token_blacklist", "outstandingtoken"),
            _item(_("Revoked tokens"), "key_off", "token_blacklist", "blacklistedtoken"),
            _item(_("Groups"), "groups", "auth", "group"),
        ],
    },
]


# --- callbacks -----------------------------------------------------------------


def pending_invitations_badge(request):
    from accounts.models import Invitation

    count = Invitation.objects.filter(status=Invitation.Status.PENDING).count()
    return count or None


def dashboard_callback(request, context):
    """Numbers and quick actions for templates/admin/index.html."""
    from accounts.models import Invitation, User
    from farms.models import Farm
    from production.models import Batch

    owners = User.objects.filter(role=User.Role.OWNER)
    owners_in_setup = owners.exclude(farm_memberships__is_active=True).distinct()

    context.update(
        {
            "kpis": [
                {
                    "label": _("Active farms"),
                    "value": Farm.objects.filter(is_active=True).count(),
                    "link": reverse_lazy("admin:farms_farm_changelist") + "?is_active__exact=1",
                },
                {
                    "label": _("Farm owners"),
                    "value": owners.count(),
                    "link": reverse_lazy("admin:accounts_farmowneraccount_changelist"),
                },
                {
                    "label": _("Owners still in setup"),
                    "value": owners_in_setup.count(),
                    "link": reverse_lazy("admin:accounts_user_changelist")
                    + "?role__exact=OWNER&membership=none",
                },
                {
                    "label": _("Pending invitations"),
                    "value": Invitation.objects.filter(
                        status=Invitation.Status.PENDING
                    ).count(),
                    "link": reverse_lazy("admin:accounts_invitation_changelist")
                    + "?status__exact=PENDING",
                },
                {
                    "label": _("Active batches"),
                    "value": Batch.objects.filter(status=Batch.Status.ACTIVE).count(),
                    "link": reverse_lazy("admin:production_batch_changelist")
                    + "?status__exact=ACTIVE",
                },
            ],
            "can_add_owner_account": can_create_owner(request),
            "recent_owners": owners.order_by("-date_joined")[:5],
        }
    )
    return context


def brand_static(path):
    """Lazy static() for UNFOLD image settings (resolved per request)."""
    return lambda request: static(path)
