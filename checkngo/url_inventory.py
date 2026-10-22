# checkngo/url_inventory.py
"""
Every API route the URLconf serves, with the view class and the permission
classes DRF will actually apply to it.

One walker, shared by the permission_matrix management command and the
tests that sweep the URLconf (farms/tests/test_farm_id_substitution.py,
docs/PERMISSION_MATRIX.md's staleness test), so the documentation and the
tests can never be looking at two different lists. It reads the same
attributes farms/tests/test_rotation_gate.py reads: initkwargs first (a
URL that passes permission_classes to as_view()), then the view class.
"""
import re
from dataclasses import dataclass

from django.urls import URLPattern, URLResolver, get_resolver

# Django's admin is session-authenticated staff tooling, not the API.
SKIPPED_NAMESPACES = {"admin"}

# The methods a route is documented and swept for. OPTIONS and HEAD are
# answered by DRF/Django themselves and carry no business rule.
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_KWARG = re.compile(r"<(?:\w+:)?(\w+)>")


@dataclass(frozen=True)
class Route:
    name: str | None
    route: str
    view_class: type | None
    permission_classes: tuple

    @property
    def kwargs(self):
        """URL kwarg names in the order they appear in the route."""
        return tuple(_KWARG.findall(self.route))

    @property
    def methods(self):
        """The methods this view implements (GET, POST, ...)."""
        if self.view_class is None:
            return ()
        return tuple(m for m in METHODS if hasattr(self.view_class, m.lower()))

    @property
    def is_farm_scoped(self):
        """Nested under one farm: /api/farms/<farm_pk>/... or /api/farms/<pk>/."""
        return self.route.startswith(("api/farms/<int:farm_pk>/", "api/farms/<int:pk>/"))


def _walk(patterns, namespace=None, prefix=""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            inner = entry.namespace
            if namespace and inner:
                inner = f"{namespace}:{inner}"
            yield from _walk(entry.url_patterns, inner or namespace, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            name = entry.name
            if namespace and name:
                name = f"{namespace}:{name}"
            yield namespace, name, prefix + str(entry.pattern), entry.callback


def api_routes():
    """Every non-admin route, in URLconf order."""
    routes = []
    for namespace, name, route, callback in _walk(get_resolver().url_patterns):
        if namespace and namespace.split(":")[0] in SKIPPED_NAMESPACES:
            continue
        view_cls = getattr(callback, "view_class", None)
        initkwargs = getattr(callback, "view_initkwargs", {}) or {}
        permissions = initkwargs.get(
            "permission_classes", getattr(view_cls, "permission_classes", ())
        )
        routes.append(Route(name, route, view_cls, tuple(permissions)))
    return routes
