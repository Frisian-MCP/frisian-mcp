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

import inspect
import logging
import re
import types
import typing
from typing import Any

from django.contrib.auth.models import AnonymousUser
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

# URL path segments that represent API versioning or routing prefixes rather than resource names.
# _resource_from_path skips these when searching for the resource name from the right.
_VERSION_SEGMENTS: frozenset[str] = frozenset({"api", "rest", "v1", "v2", "v3", "v4", "v5"})

# Actions that do not require request body data (read-only actions).
_READ_ONLY_ACTIONS: frozenset[str] = frozenset({"list", "retrieve"})

# Standard actions that carry a request body and benefit from serializer introspection.
_BODY_ACTIONS: frozenset[str] = frozenset({"create", "update", "partial_update"})

# All six standard DRF actions.  Custom @action methods are those NOT in this set.
_STANDARD_ACTIONS: frozenset[str] = frozenset(
    {"list", "create", "retrieve", "update", "partial_update", "destroy"}
)

# Parameter names that DRF injects into action methods and should never be surfaced
# as tool arguments (used by _schema_from_action_signature).
_SKIP_PARAMS: frozenset[str] = frozenset({"self", "request", "pk", "format", "args", "kwargs"})

# Python type annotation → JSON Schema primitive.  Used when introspecting custom-action
# signatures to derive query-parameter types from parameter annotations.
_ANNOTATION_TO_JSON_TYPE: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


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

    def get_input_schema(  # pylint: disable=too-many-locals
        self, view_class: type, action: str
    ) -> dict[str, Any]:
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
            # Accept both integer and string IDs so that UUID-keyed models work
            # without schema validation errors.  jsonschema validates the caller's
            # value against the declared type; hardcoding "integer" rejects valid
            # UUID strings before the ViewSet even sees them.
            schema["properties"]["id"] = {
                "anyOf": [{"type": "integer"}, {"type": "string"}],
                "description": "Object identifier (integer PK or UUID string)",
            }
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

        # Filter/search/ordering parameters for list actions.
        if action == "list":
            filter_props = _schema_from_filter_backends(view_class)
            if filter_props:
                schema.setdefault("properties", {}).update(filter_props)

        # Custom read-only @action methods (not in _STANDARD_ACTIONS, no body): derive
        # query parameters from the action method's function signature.  Typed parameters
        # (e.g. ``format: str = "csv"``) become optional schema properties.  When no
        # typed parameters are found, a DEBUG log guides developers to add @mcp_tool.
        if action not in _STANDARD_ACTIONS and not has_body and action_func is not None:
            sig_schema = _schema_from_action_signature(action_func, view_class, action)
            if sig_schema.get("properties"):
                existing_props = schema.setdefault("properties", {})
                existing_props.update(sig_schema["properties"])
                extra_req: list[str] = sig_schema.get("required", [])
                if extra_req:
                    current_req: list[str] = schema.get("required", [])
                    schema["required"] = list({*current_req, *extra_req})

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

    def _process_pattern(  # pylint: disable=too-many-locals
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
            logger.warning(
                "friese_mcp: basename not set for %s; falling back to path-derived resource %r. "
                "Set basename in your router or URL conf to avoid ambiguous tool names.",
                cls.__name__,
                resource,
            )

        include_actions, exclude_actions = _action_filters(cls)

        for _http_method, action_name in actions.items():
            if include_actions is not None and action_name not in include_actions:
                logger.debug(
                    "friese_mcp: skipping %s.%s — not in mcp_include_actions",
                    cls.__name__,
                    action_name,
                )
                continue
            if action_name in exclude_actions:
                logger.debug(
                    "friese_mcp: skipping %s.%s — listed in mcp_exclude_actions",
                    cls.__name__,
                    action_name,
                )
                continue
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
            # Use a minimal stub request so that get_serializer_class() implementations
            # that inspect self.request.method or self.request.user do not raise
            # AttributeError.  The stub carries the most common write-action method
            # (POST) and an anonymous user to avoid any auth-dependent branching.
            viewset.request = types.SimpleNamespace(  # type: ignore[assignment]
                method="POST",
                auth=None,
                META={},
                user=AnonymousUser(),
            )
            viewset.format_kwarg = None
            viewset.action = action
            serializer_class = viewset.get_serializer_class()
            return _schema_from_serializer(serializer_class)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "friese_mcp: schema derivation failed for %s.%s — falling back to empty schema. "
                "Check that get_serializer_class() does not require a real request object.",
                view_class.__name__,
                action,
            )
            return {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _schema_from_action_signature(
    action_func: Any,
    view_class: type,
    action: str,
) -> dict[str, Any]:
    """
    Derive a JSON Schema from a custom action method's function signature.

    Extracts non-self, non-DRF-internal parameters and maps their annotations
    to JSON Schema types.  Parameters with defaults become optional; those
    without become required.  Falls back to an empty schema on any failure.

    Parameters named ``self``, ``request``, ``pk``, ``format``, or ``args``/
    ``kwargs`` variadic placeholders are always skipped.

    Args:
        action_func: The unbound ViewSet action method.
        view_class: The ViewSet class (used for logging only).
        action: The action name (used for logging only).

    Returns:
        A ``{"type": "object", "properties": {...}}`` dict, or an empty-
        properties schema when no typed parameters are found.

    """
    try:
        sig = inspect.signature(action_func)
        # get_type_hints() resolves PEP 563 string annotations (from __future__ import
        # annotations) to their actual types, enabling correct JSON type mapping.
        try:
            hints: dict[str, Any] = typing.get_type_hints(action_func)
        except Exception:  # pylint: disable=broad-exception-caught
            hints = {}

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in _SKIP_PARAMS:
                continue
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            resolved_type: Any = hints.get(param_name)
            json_type: str = _ANNOTATION_TO_JSON_TYPE.get(resolved_type, "string")
            prop: dict[str, Any] = {"type": json_type}
            properties[param_name] = prop

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        if not properties:
            logger.debug(
                "friese_mcp: custom action %s.%s has no typed parameters — "
                "schema is bare. Annotate parameters or add @mcp_tool for a richer schema.",
                view_class.__name__,
                action,
            )
            return {"type": "object", "properties": {}}

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug(
            "friese_mcp: signature introspection failed for %s.%s",
            view_class.__name__,
            action,
        )
        return {"type": "object", "properties": {}}


def _schema_from_filter_backends(view_class: type) -> dict[str, Any]:
    """
    Derive query-parameter properties from a ViewSet's DRF filter backends.

    Handles the three standard DRF filter backends without importing them as
    hard dependencies — detection is by class name so that subclasses and
    third-party re-exports also match:

    * ``SearchFilter`` — adds a ``search`` string parameter.
    * ``OrderingFilter`` — adds an ``ordering`` string parameter; if the
      ViewSet declares ``ordering_fields`` (not ``"__all__"``), the property
      also carries an ``enum`` of valid ``field`` / ``-field`` variants.
    * ``DjangoFilterBackend`` — adds one string parameter per filterset field
      derived from ``filterset_fields`` (list or dict) or ``filterset_class``
      ``base_filters``.  django-filter is an optional dependency; the backend
      is identified by class name so the package need not be installed.

    Returns an empty dict when no filter backends are declared or introspection
    fails.

    Args:
        view_class: A DRF ViewSet class.

    Returns:
        A dict of ``{field_name: json_schema_property}`` pairs, or ``{}``
        if no filterable parameters were found.

    """
    filter_backends: list[type] = getattr(view_class, "filter_backends", [])
    properties: dict[str, Any] = {}

    for backend_class in filter_backends:
        try:
            name: str = getattr(backend_class, "__name__", "")
            if name == "SearchFilter":
                properties["search"] = {
                    "type": "string",
                    "description": "Full-text search term",
                }
            elif name == "OrderingFilter":
                prop: dict[str, Any] = {
                    "type": "string",
                    "description": (
                        "Comma-separated fields to sort by. "
                        "Prefix a field name with - for descending order."
                    ),
                }
                ordering_fields: Any = getattr(view_class, "ordering_fields", None)
                if ordering_fields and ordering_fields != "__all__":
                    variants: list[str] = []
                    for field in ordering_fields:
                        variants.append(str(field))
                        variants.append(f"-{field}")
                    prop["enum"] = variants
                properties["ordering"] = prop
            elif name == "DjangoFilterBackend":
                properties.update(_filterset_properties(view_class))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "friese_mcp: filter backend introspection failed for %s on %s",
                getattr(backend_class, "__name__", repr(backend_class)),
                view_class.__name__,
            )

    return properties


def _filterset_properties(view_class: type) -> dict[str, Any]:
    """
    Extract filter field names from a ViewSet's filterset configuration.

    Checks ``filterset_fields`` first (list or dict), then falls back to
    ``filterset_class.base_filters``.  Returns an empty dict on any failure.

    Args:
        view_class: A DRF ViewSet class with django-filter integration.

    Returns:
        A ``{field_name: {"type": "string", "description": ...}}`` dict.

    """
    properties: dict[str, Any] = {}

    filterset_fields: Any = getattr(view_class, "filterset_fields", None)
    if filterset_fields is not None:
        field_names: list[str] = (
            list(filterset_fields.keys())
            if isinstance(filterset_fields, dict)
            else list(filterset_fields)
        )
        for field_name in field_names:
            properties[field_name] = {
                "type": "string",
                "description": f"Filter by {field_name}",
            }

    filterset_class: Any = getattr(view_class, "filterset_class", None)
    if filterset_class is not None:
        try:
            for field_name, filter_obj in filterset_class.base_filters.items():
                if field_name not in properties:
                    label: Any = getattr(filter_obj, "label", None)
                    properties[field_name] = {
                        "type": "string",
                        "description": str(label) if label else f"Filter by {field_name}",
                    }
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "friese_mcp: filterset_class introspection failed for %s",
                view_class.__name__,
            )

    return properties


def _action_filters(
    view_class: type,
) -> tuple[frozenset[str] | None, frozenset[str]]:
    """
    Read per-ViewSet MCP surface area control attributes.

    Returns a ``(include_actions, exclude_actions)`` pair:

    * ``include_actions`` is a ``frozenset`` of explicitly allowed action names, or
      ``None`` when ``mcp_include_actions`` is absent (all actions allowed by default).
    * ``exclude_actions`` is a ``frozenset`` of suppressed action names (may be empty).

    These attributes are read from the ViewSet class directly; they are not
    inherited from ``ViewSetMixin`` and must be declared on the concrete class.
    """
    raw_include: list[str] | None = getattr(view_class, "mcp_include_actions", None)
    raw_exclude: list[str] | None = getattr(view_class, "mcp_exclude_actions", None)
    include: frozenset[str] | None = frozenset(raw_include) if raw_include is not None else None
    exclude: frozenset[str] = frozenset(raw_exclude or [])
    return include, exclude


def _resource_from_path(path: str) -> str:
    """
    Extract the resource name from a URL pattern string.

    Strips regex anchors and parameter placeholders, then scans path segments
    from right to left, skipping known versioning/routing prefixes defined in
    :data:`_VERSION_SEGMENTS` (``api``, ``rest``, ``v1`` … ``v5``).  Returns
    the first non-version segment found, or ``"unknown"`` when none exists.

    Examples::

        _resource_from_path("^api/v1/users/$") == "users"
        _resource_from_path("^orders/(?P<pk>[^/.]+)/$") == "orders"
        _resource_from_path("api/v1/") == "unknown"

    """
    # Strip regex anchors globally (^$ may appear mid-string in router-generated patterns
    # such as "api/^items/$" when a URLResolver prefix concatenates with a nested pattern).
    clean = _PARAM_RE.sub("", path).replace("^", "").replace("$", "").strip("/")
    parts = [p for p in clean.split("/") if p and not p.startswith("(")]
    for segment in reversed(parts):
        name = segment.replace("-", "_")
        if name not in _VERSION_SEGMENTS:
            return name
    return "unknown"


def _action_description(view_class: type, action: str) -> str:
    """
    Build a human-readable tool description from the ViewSet and action.

    Resolution order:

    1. First non-empty line of the action method's docstring — specific and
       authored by the developer.
    2. Generic label derived from the class name and action — used when no
       docstring is present or the action is not in the standard label map.
    """
    # Priority 1: action method docstring.
    action_method = getattr(view_class, action, None)
    if action_method is not None:
        doc: str = getattr(action_method, "__doc__", None) or ""
        first_line = doc.strip().split("\n")[0].strip().rstrip(".")
        if first_line:
            return first_line

    # Priority 2: generic label from class name.
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
