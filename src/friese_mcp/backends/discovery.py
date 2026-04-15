"""
DRFSyncDiscovery — default MCP tool discovery backend.

Scans the Django URL resolver tree at startup, finds every DRF ViewSet
action that has not been excluded with ``@mcp_ignore``, and produces a
:class:`~friese_mcp.backends.base.ToolDefinition` for each one.

Tool names follow the ``{resource}.{action}`` convention, e.g.
``"users.list"`` or ``"orders.retrieve"``.  The resource name is derived
from the last literal path segment of the URL pattern.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import BasePermission
from rest_framework.viewsets import ViewSetMixin

from friese_mcp.backends.base import BaseDiscoveryBackend, ToolDefinition

logger = logging.getLogger(__name__)

# Matches URL path parameters in both regex (``(?P<pk>[^/.]+)``) and
# path-converter (``<pk>`` / ``<int:pk>``) syntax.
_PARAM_RE = re.compile(r"\(\?P<[^>]+>[^)]+\)|<[^>]+>")

# Map DRF field class names to JSON Schema primitive types.
_FIELD_TO_JSON_TYPE: dict[str, str] = {
    "CharField": "string",
    "EmailField": "string",
    "URLField": "string",
    "SlugField": "string",
    "RegexField": "string",
    "UUIDField": "string",
    "FilePathField": "string",
    "IPAddressField": "string",
    "DateField": "string",
    "DateTimeField": "string",
    "TimeField": "string",
    "DurationField": "string",
    "IntegerField": "integer",
    "SmallIntegerField": "integer",
    "BigIntegerField": "integer",
    "FloatField": "number",
    "DecimalField": "number",
    "BooleanField": "boolean",
    "NullBooleanField": "boolean",
    "ListField": "array",
    "DictField": "object",
    "JSONField": "object",
}

# Actions that accept a detail identifier (pk / id) as the primary argument.
_DETAIL_ACTIONS: frozenset[str] = frozenset({"retrieve", "update", "partial_update", "destroy"})

# Actions that do not require request body data (read-only actions).
_READ_ONLY_ACTIONS: frozenset[str] = frozenset({"list", "retrieve"})

# Standard actions that carry a request body and benefit from serializer introspection.
_BODY_ACTIONS: frozenset[str] = frozenset({"create", "update", "partial_update"})


class DRFSyncDiscovery(BaseDiscoveryBackend):
    """
    Default synchronous discovery backend for DRF ViewSets.

    Walks the entire Django URL resolver tree and registers every ViewSet
    action that has not been marked with ``@mcp_ignore``.  Custom ViewSet
    base classes are supported as long as they inherit from
    :class:`~rest_framework.viewsets.ViewSetMixin`.
    """

    def discover_tools(self) -> list[ToolDefinition]:
        """
        Scan URL patterns and return a :class:`ToolDefinition` per action.

        Each (ViewSet class, action) pair is registered exactly once even
        when the same ViewSet appears in multiple URL patterns (e.g. list
        route ``/users/`` and detail route ``/users/<pk>/``).
        """
        tools: list[ToolDefinition] = []
        seen: set[tuple[type, str]] = set()
        try:
            resolver = get_resolver()
            self._scan(resolver.url_patterns, "", seen, tools)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("friese_mcp discovery failed")
        return tools

    def get_input_schema(self, view_class: type, action: str) -> dict[str, Any]:
        """
        Derive a JSON Schema for a ViewSet action from its serializer.

        For detail actions (retrieve, update, partial_update, destroy), adds
        a required ``"id"`` property.  For write actions (create, update,
        partial_update), attempts to introspect the serializer fields.

        Falls back to ``{"type": "object"}`` when serializer introspection
        fails.

        Args:
            view_class: A DRF ViewSet class.
            action: The action name (e.g. ``"list"``, ``"create"``).

        Returns:
            A JSON Schema describing the expected tool arguments.

        """
        schema: dict[str, Any] = {"type": "object", "properties": {}}

        # Detail actions always accept an id/pk.
        if action in _DETAIL_ACTIONS:
            schema["properties"]["id"] = {"type": "integer", "description": "Object identifier"}
            if action != "partial_update":
                schema["required"] = ["id"]

        # Body-carrying actions: try to derive additional properties from serializer.
        # Only introspect the serializer for actions that send a request body —
        # standard write actions (create, update, partial_update) and custom
        # @action methods whose HTTP mapping includes a body-carrying method.
        # Excludes: destroy (DELETE, no body), list/retrieve (GET), custom GET actions.
        action_func = getattr(view_class, action, None)
        action_mapping: dict[str, str] = getattr(action_func, "mapping", {})
        has_body = action in _BODY_ACTIONS or (
            action_mapping and any(m in ("post", "put", "patch") for m in action_mapping)
        )
        if has_body:
            serializer_schema = self._schema_from_viewset(view_class, action)
            existing = schema.setdefault("properties", {})
            existing.update(serializer_schema.get("properties", {}))
            # Merge required lists, preserving id if present.
            # partial_update (PATCH) makes all body fields optional — skip required merge.
            if action != "partial_update":
                extra_required: list[str] = serializer_schema.get("required", [])
                if extra_required:
                    current_required: list[str] = schema.get("required", [])
                    merged = list({*current_required, *extra_required})
                    schema["required"] = merged

        return schema

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scan(
        self,
        patterns: list[URLPattern | URLResolver],
        prefix: str,
        seen: set[tuple[type, str]],
        tools: list[ToolDefinition],
    ) -> None:
        """Recursively walk URL patterns and collect ViewSet tools."""
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                self._scan(
                    pattern.url_patterns,
                    prefix + str(pattern.pattern),
                    seen,
                    tools,
                )
            elif isinstance(pattern, URLPattern):
                self._process_pattern(pattern, prefix, seen, tools)

    def _process_pattern(
        self,
        pattern: URLPattern,
        prefix: str,
        seen: set[tuple[type, str]],
        tools: list[ToolDefinition],
    ) -> None:
        """Inspect a single URL pattern and register its ViewSet actions."""
        view_func = pattern.callback
        cls: type | None = getattr(view_func, "cls", None)
        if cls is None or not issubclass(cls, ViewSetMixin):
            return
        if getattr(cls, "_mcp_ignore", False):
            return

        actions: dict[str, str] = getattr(view_func, "actions", {})
        # Prefer the router-assigned basename (set in initkwargs by DRF's DefaultRouter /
        # SimpleRouter) — it is always the correct resource name regardless of URL shape.
        # Fall back to path-based derivation for hand-written URL confs without a router.
        basename: str | None = getattr(view_func, "initkwargs", {}).get("basename")
        if basename:
            resource = str(basename).replace("-", "_")
        else:
            full_path = prefix + str(pattern.pattern)
            resource = _resource_from_path(full_path)

        for _http_method, action_name in actions.items():
            if (cls, action_name) in seen:
                continue
            action_method = getattr(cls, action_name, None)
            if action_method is not None and getattr(action_method, "_mcp_ignore", False):
                continue

            seen.add((cls, action_name))
            perm_classes: tuple[type[BasePermission], ...] = tuple(
                getattr(cls, "permission_classes", [])
            )

            tools.append(
                ToolDefinition(
                    name=f"{resource}.{action_name}",
                    description=_action_description(cls, action_name),
                    input_schema=self.get_input_schema(cls, action_name),
                    permission_classes=perm_classes,
                    source="auto",
                    view_class=cls,
                    action=action_name,
                )
            )
            logger.debug("friese_mcp discovered tool %s.%s", resource, action_name)

    def _schema_from_viewset(self, view_class: type, action: str) -> dict[str, Any]:
        """Attempt to derive a JSON Schema from a ViewSet's serializer."""
        try:
            viewset = view_class()
            viewset.request = None
            viewset.format_kwarg = None
            viewset.action = action
            serializer_class = viewset.get_serializer_class()
            return _schema_from_serializer(serializer_class)
        except Exception:  # pylint: disable=broad-exception-caught
            return {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resource_from_path(path: str) -> str:
    """
    Extract the resource name from a URL pattern string.

    Takes the last non-empty literal segment (stripping regex anchors and
    parameter placeholders) and converts hyphens to underscores.

    Examples::

        _resource_from_path("^api/v1/users/$") == "users"
        _resource_from_path("^orders/(?P<pk>[^/.]+)/$") == "orders"

    """
    # Strip regex anchors globally (^$ may appear mid-string in router-generated patterns
    # such as "api/^items/$" when a URLResolver prefix concatenates with a nested pattern).
    clean = _PARAM_RE.sub("", path).replace("^", "").replace("$", "").strip("/")
    parts = [p for p in clean.split("/") if p and not p.startswith("(")]
    name = parts[-1] if parts else "unknown"
    return name.replace("-", "_")


def _action_description(view_class: type, action: str) -> str:
    """Build a human-readable tool description from the ViewSet and action."""
    # ViewSetMixin defines `basename = None` as a class attribute, so getattr's
    # default never fires — use `or` to fall back when the attribute is falsy.
    resource = getattr(view_class, "basename", None) or view_class.__name__.replace("ViewSet", "")
    action_labels: dict[str, str] = {
        "list": f"List {resource} objects",
        "retrieve": f"Retrieve a {resource} object by ID",
        "create": f"Create a new {resource} object",
        "update": f"Replace a {resource} object by ID",
        "partial_update": f"Partially update a {resource} object by ID",
        "destroy": f"Delete a {resource} object by ID",
    }
    return action_labels.get(action, f"Invoke {view_class.__name__}.{action}")


def _schema_from_serializer(serializer_class: type) -> dict[str, Any]:
    """Convert a DRF serializer class to a JSON Schema properties dict."""
    try:
        serializer = serializer_class()
    except Exception:  # pylint: disable=broad-exception-caught
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for field_name, field in serializer.fields.items():
        if getattr(field, "read_only", False):
            continue
        field_type_name = type(field).__name__
        json_type = _FIELD_TO_JSON_TYPE.get(field_type_name, "string")
        prop: dict[str, Any] = {"type": json_type}
        help_text = getattr(field, "help_text", None)
        if help_text:
            prop["description"] = str(help_text)
        properties[field_name] = prop
        if getattr(field, "required", False):
            required.append(field_name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
