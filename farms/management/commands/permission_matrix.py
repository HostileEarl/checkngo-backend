# farms/management/commands/permission_matrix.py
"""
Write docs/PERMISSION_MATRIX.md: who may call each API route, per method.

The table is derived, not typed. The URLconf is walked
(checkngo/url_inventory.py); each route's permission classes are read off
the view; each role's cell comes from those classes' own role sets
(FarmScopedPermission.read_roles / allowed_roles) and from what the
non-farm classes admit (IsAuthenticated, HasRotatedCredential, AllowAny).
The worker's house scope comes from BatchScopedMixin.enforce_house_write_scope
and from the views listed in HOUSE_SCOPED below, which check it in code.

Rules a permission class cannot express (a manager inviting workers only,
the owner-only archive inside a view, the 24-hour lock) are hand-written
in NOTES. farms/tests/test_permission_matrix.py fails when the committed
file is out of date, when a NOTES entry names a route or method that no
longer exists, and when a "no" cell in the table is not actually refused
by the running app.

    python manage.py permission_matrix           # rewrite the file
    python manage.py permission_matrix --check   # exit 1 if it is stale
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated

from checkngo.url_inventory import api_routes
from farms.models import FarmMembership
from farms.permissions import (
    CanRegisterFarm,
    FarmScopedPermission,
    HasRotatedCredential,
    IsExternalPartner,
)
from partners.permissions import IsLinkedPartner
from production.mixins import BatchScopedMixin

OUTPUT = Path(settings.BASE_DIR) / "docs" / "PERMISSION_MATRIX.md"

COLUMNS = [
    ("owner", "Owner"),
    ("manager", "Manager"),
    ("worker", "Worker (assigned house)"),
    ("worker_elsewhere", "Worker (unassigned house)"),
    ("buyer", "Buyer"),
    ("anonymous", "Anonymous"),
    ("unrotated", "Unrotated PIN"),
]

YES, NO, PER_RECORD = "yes", "no", "per record"

_MEMBER_ROLES = {
    "owner": FarmMembership.Role.OWNER,
    "manager": FarmMembership.Role.MANAGER,
    "worker": FarmMembership.Role.WORKER,
    "worker_elsewhere": FarmMembership.Role.WORKER,
}
_ALL_MEMBERS = set(_MEMBER_ROLES.values())

# Views that check the worker's house assignment in code rather than
# through BatchScopedMixin. Method -> what an unassigned worker gets.
HOUSE_SCOPED = {
    "production:task-completion-list": {"POST": NO},
    "production:task-completion-detail": {"DELETE": NO},
    "production:task-completion-bulk-sync": {"POST": PER_RECORD},
}

_LOCK = "Refused once the record is older than 24 hours (server clock)."
_BULK = (
    "Per-record results: 200 when all landed, 207 with failed[] otherwise. "
    "At most 60 records."
)

# Rules the permission classes cannot express. URL name -> method -> note.
NOTES = {
    "accounts:login": {"POST": "Phone + PIN. An unrotated PIN logs in so it can be rotated."},
    "accounts:token-refresh": {"POST": "Needs a valid refresh token; rotation blacklists the old one."},
    "accounts:logout": {"POST": "Open to an unrotated PIN by design."},
    "accounts:me": {"GET": "Open to an unrotated PIN by design: the app reads must_change_credential here."},
    "accounts:credential-change": {"POST": "The PIN rotation itself, so open to an unrotated PIN."},
    "accounts:invitation-accept": {"POST": "Public: the invitee has no account yet. Needs the invitation token and PIN."},
    "accounts:access-request-create": {"POST": "Public landing-page form."},
    "farms:farm-list": {
        "GET": "Only farms where the caller has an active membership.",
        "POST": "Owner accounts only (User.role OWNER); the caller becomes the new farm's OWNER. Everyone else: 403.",
    },
    "farms:farm-invitations": {
        "GET": "A manager never sees buyer invitations.",
        "POST": "A manager may invite workers only (CanInviteRole, 403 otherwise).",
    },
    "farms:invitation-revoke": {
        "POST": "A manager may revoke worker invitations only, never manager-level or buyer ones.",
    },
    "farms:member-revoke": {
        "POST": "A manager cannot revoke a manager. The owner cannot be revoked. Nobody revokes themselves.",
    },
    "farms:member-houses": {
        "PATCH": "Houses must be this farm's and not archived. An empty list lifts the restriction (any house).",
    },
    "farms:farm-archive": {"POST": "After archiving, non-owners lose access and the owner keeps read-only access."},
    "farms:farm-reactivate": {
        "POST": "Not membership-scoped: looks the farm up by Farm.owner == caller (404 for anyone else), so it works on an archived farm.",
    },
    "farms:ownership-transfer": {"POST": "Deletes the outgoing owner's membership."},
    "production:house-archive": {"POST": "Refused while the house holds an active batch."},
    "production:batch-terminate": {"POST": "ACTIVE batches only (404 otherwise). A reason is required."},
    "production:daily-list": {
        "POST": "Same client UUID again = update, under the 24-hour lock. A different UUID for a date already recorded = 400.",
    },
    "production:daily-bulk-sync": {"POST": _BULK},
    "production:daily-detail": {"PATCH": _LOCK, "PUT": _LOCK},
    "production:daily-correct": {
        "POST": "The audited way past the 24-hour lock: reason of 15+ characters, permanent RecordCorrection row. ACTIVE batches only.",
    },
    "production:weight-detail": {"PATCH": _LOCK, "PUT": _LOCK},
    "production:sale-list": {"POST": "Same client UUID again = update, under the 24-hour lock."},
    "production:sale-bulk-sync": {"POST": _BULK},
    "production:sale-detail": {"PATCH": _LOCK, "PUT": _LOCK},
    "production:payment-list": {
        "POST": "Append-only: no edit or delete exists. A mistake is reversed with a negative entry. Accepted after the batch closes.",
    },
    "production:batch-close": {"POST": "ACTIVE batches with at least one sale only."},
    "production:inventory-item-detail": {"DELETE": "Only for an item with no history; otherwise archive it."},
    "production:inventory-usage-list": {
        "POST": "Farm-level, not house-scoped. Same client UUID again = update, under the 24-hour lock.",
    },
    "production:inventory-usage-bulk-sync": {"POST": _BULK},
    "production:inventory-usage-detail": {"PATCH": _LOCK, "PUT": _LOCK},
    "production:task-completion-list": {
        "POST": "A routine item already ticked for that house and day is absorbed: 200 with the existing tick.",
    },
    "production:task-completion-detail": {"DELETE": "The un-tick. " + _LOCK},
    "production:task-completion-bulk-sync": {"POST": _BULK},
    "analytics:alerts": {"GET": "Each role sees only the alerts addressed to it; low-stock alerts are owner/manager only."},
    "partners:farm-partners": {"POST": "Owner only: who the farm transacts with."},
    "partners:my-partner-farms": {"GET": "The caller's own links. Staff get an empty list."},
    "partners:my-purchases": {"GET": "The buyer's own purchases; no price or revenue. Staff get an empty list."},
    "partners:my-payments": {"GET": "The buyer's own payments; no balance. Staff get an empty list."},
    "schema": {"GET": "Public API documentation; no user or farm data."},
    "swagger-ui": {"GET": "Public API documentation."},
    "redoc": {"GET": "Public API documentation."},
}


def _class_cell(permission, method, column):
    """What one permission class says about one caller. True / False."""
    if isinstance(permission, type) and issubclass(permission, FarmScopedPermission):
        if column in ("buyer", "anonymous", "unrotated"):
            return False
        roles = permission.read_roles if method in SAFE_METHODS else permission.allowed_roles
        return _MEMBER_ROLES[column] in (roles or _ALL_MEMBERS)
    if permission in (IsExternalPartner, IsLinkedPartner):
        return column == "buyer"
    if permission is CanRegisterFarm:
        # The account's own role, not a farm membership: an owner account.
        return method in SAFE_METHODS or column == "owner"
    if permission is HasRotatedCredential:
        return column not in ("anonymous", "unrotated")
    if permission is IsAuthenticated:
        return column != "anonymous"
    if permission is AllowAny:
        return True
    raise CommandError(
        f"permission_matrix does not know {permission!r}. Teach _class_cell what it admits."
    )


def _house_scope(route, method):
    """What a worker writing against a house they are not assigned to gets, if limited."""
    if route.name in HOUSE_SCOPED:
        return HOUSE_SCOPED[route.name].get(method)
    view = route.view_class
    if method not in SAFE_METHODS and isinstance(view, type) and issubclass(view, BatchScopedMixin):
        return NO if view.enforce_house_write_scope else PER_RECORD
    return None


def _throttle_note(route):
    rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
    throttles = getattr(route.view_class, "throttle_classes", ()) or ()
    parts = [f"{rates[t.scope]} ({t.scope})" for t in throttles if getattr(t, "scope", None) in rates]
    return f"Throttled: {', '.join(parts)}." if parts else ""


def matrix_rows():
    """(route, method, {column: cell}, note) for every route and method, URLconf order."""
    rows = []
    for route in api_routes():
        for method in route.methods:
            cells = {}
            for column, _ in COLUMNS:
                allowed = all(_class_cell(p, method, column) for p in route.permission_classes)
                cells[column] = YES if allowed else NO
            scope = _house_scope(route, method)
            if scope and cells["worker_elsewhere"] == YES:
                cells["worker_elsewhere"] = scope
            note = " ".join(
                part for part in (NOTES.get(route.name, {}).get(method, ""), _throttle_note(route)) if part
            )
            rows.append((route, method, cells, note))
    return rows


HEADER = """\
# Permission matrix

Generated by `python manage.py permission_matrix` from the URLconf and the
permission classes. Do not edit by hand: `farms/tests/test_permission_matrix.py`
fails when this file is out of date, and checks every "no" below against
the running app.

How each cell is derived:

- **Owner / Manager / Worker** come from the view's permission classes. For
  farm-scoped classes (`FarmScopedPermission` and its subclasses) the cell
  is the class's own `read_roles` (GET) or `allowed_roles` (POST, PUT,
  PATCH, DELETE); an empty set means every active member.
- **Worker (assigned house)** is a worker writing against a house they are
  assigned to, or a worker with no assignments (an empty assignment means
  every house). **Worker (unassigned house)** is a worker writing against a
  house outside their assignments: `no` is a 403 for the whole request,
  `per record` means a bulk sync returns those records in `failed[]` of a
  207. Reads are never house-scoped.
- **Buyer** is a consumer account linked to the farm. Buyers hold no farm
  membership, so every farm-scoped route refuses them.
- **Anonymous** has no token: `no` is a 401.
- **Unrotated PIN** is a signed-in user who has not yet replaced their
  one-time PIN (`must_change_credential`): `no` is a 403 with "You must
  change your initial PIN before continuing."

Applies to every farm-scoped route and is not repeated per row: on an
**archived** farm only the owner keeps access, read-only (GET); every other
member is refused. A `yes` means the permission layer admits the caller;
the Notes column lists what the view checks after that.
"""


def render():
    head = "| URL pattern | Method | Permission classes | " + " | ".join(t for _, t in COLUMNS) + " | Notes |"
    rule = "|" + "---|" * (len(COLUMNS) + 4)
    lines = [HEADER, head, rule]
    for route, method, cells, note in matrix_rows():
        permissions = ", ".join(getattr(p, "__name__", repr(p)) for p in route.permission_classes) or "(none)"
        lines.append(
            f"| `/{route.route}` | {method} | {permissions} | "
            + " | ".join(cells[c] for c, _ in COLUMNS)
            + f" | {note} |"
        )
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Write docs/PERMISSION_MATRIX.md from the URLconf and permission classes."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Exit 1 if the file is out of date.")

    def handle(self, *args, check=False, **options):
        content = render()
        if check:
            current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
            if current != content:
                raise CommandError(f"{OUTPUT} is out of date. Run: python manage.py permission_matrix")
            self.stdout.write(self.style.SUCCESS(f"{OUTPUT} is up to date."))
            return
        OUTPUT.write_text(content, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {OUTPUT} ({content.count(chr(10))} lines)."))
