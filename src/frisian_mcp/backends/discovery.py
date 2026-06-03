"""
DRFSyncDiscovery — default MCP tool discovery backend.

Scans the Django URL resolver tree at startup, finds every DRF ViewSet
action that has not been excluded with ``@mcp_ignore``, and produces a
:class:`~frisian_mcp.backends.base.ToolDefinition` for each one.

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

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.relations import ManyRelatedField, RelatedField, SlugRelatedField
from rest_framework.serializers import ListSerializer
from rest_framework.viewsets import ViewSetMixin

try:
    from django_filters.rest_framework import (  # pylint: disable=import-error
        DjangoFilterBackend as _DjangoFilterBackend,
    )
except ImportError:
    _DjangoFilterBackend = None

try:
    from rest_framework.renderers import JSONRenderer as _JSONRenderer  # noqa: I001  # pylint: disable=ungrouped-imports
except ImportError:  # pragma: no cover
    _JSONRenderer = None  # type: ignore[assignment,misc]

from frisian_mcp.backends.base import BaseDiscoveryBackend, ToolDefinition

logger = logging.getLogger(__name__)

# Matches URL path parameters in both regex (``(?P<pk>[^/.]+)``) and
# path-converter (``<pk>`` / ``<int:pk>``) syntax.
_PARAM_RE = re.compile(r"\(\?P<[^>]+>[^)]+\)|<[^>]+>")

# Regex anchors that appear in raw Django URL pattern strings (``re_path``
# regexes such as ``^products/$``).  PKG-23: stripped from ``url_path`` before
# the value is stored on a :class:`ToolDefinition` so downstream consumers
# can do prefix / equality / display logic without tripping over regex
# syntax (``api/catalog/^products/$`` → ``api/catalog/products/``).
_REGEX_ANCHOR_RE = re.compile(r"[\^\$]")

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

# Object form for FK / related-field references.  Host serializers
# (DRF's PrimaryKeyRelatedField, SlugRelatedField, custom natural-key/PK
# hybrids that accept either form, etc.) accept several shapes — id, pk,
# slug, or natural-key-name — depending on the field configuration.  The
# dispatcher schema accepts any of them as additional properties so the
# host serializer makes the final decision.
_FK_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "pk": {"type": "string"},
        "slug": {"type": "string"},
        "name": {"type": "string"},
    },
    "additionalProperties": True,
}

# A single FK reference accepts either a bare string (UUID, slug, or natural
# key) OR an object form (see above).  Used as the schema for single
# RelatedField writes and as the items schema for ManyRelatedField arrays.
_FK_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string"},
        _FK_OBJECT_SCHEMA,
    ]
}

# Actions that accept a detail identifier (pk / id) as the primary argument.
_DETAIL_ACTIONS: frozenset[str] = frozenset({"retrieve", "update", "partial_update", "destroy"})


def _tool_name_separator() -> str:
    """Return the configured tool name separator (default ``'_'``)."""
    return getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")

# URL path segments that represent API versioning or routing prefixes rather than resource names.
# _resource_from_path skips these when searching for the resource name from the right.
# Override via FRISIAN_MCP_VERSION_SEGMENTS (list) or FRISIAN_MCP_VERSION_SEGMENT_PATTERN (regex).
_DEFAULT_VERSION_SEGMENTS: frozenset[str] = frozenset(
    {"api", "rest", "v1", "v2", "v3", "v4", "v5"}
)


def _is_version_segment(segment: str) -> bool:
    r"""
    Return ``True`` when *segment* is a versioning or routing prefix to skip.

    Resolution order:

    1. ``FRISIAN_MCP_VERSION_SEGMENT_PATTERN`` (a regex string) — when set,
       the segment is matched against this pattern.  Useful for schemes that
       can't be enumerated up front, e.g. ``r"^(api|rest|v\d+|\d{4}-\d{2})$"``
       to accept any ``v<N>`` and date-based versions like ``2024-01``.

    2. ``FRISIAN_MCP_VERSION_SEGMENTS`` (a list of strings) — when set,
       replaces the built-in set entirely.  Use when the host has a small,
       known set of routing prefixes (e.g. ``["api", "internal", "svc"]``).

    3. :data:`_DEFAULT_VERSION_SEGMENTS` — fallback when neither setting is
       configured (``api``, ``rest``, ``v1`` … ``v5``).

    The *segment* passed in is already normalised (hyphens → underscores) by
    :func:`_resource_from_path`, so patterns and lists should use underscores.
    """
    pattern: str | None = getattr(settings, "FRISIAN_MCP_VERSION_SEGMENT_PATTERN", None)
    if pattern is not None:
        return bool(re.fullmatch(pattern, segment))
    custom: list[str] | None = getattr(settings, "FRISIAN_MCP_VERSION_SEGMENTS", None)
    if custom is not None:
        return segment in frozenset(custom)
    return segment in _DEFAULT_VERSION_SEGMENTS

# Actions that do not require request body data (read-only actions).
_READ_ONLY_ACTIONS: frozenset[str] = frozenset({"list", "retrieve"})

# Standard actions that carry a request body and benefit from serializer introspection.
# bulk_update / bulk_partial_update operate on the list route and carry a body of
# [{id, ...field...}, ...] — the same serializer class is used so introspection works.
_BODY_ACTIONS: frozenset[str] = frozenset(
    {"create", "update", "partial_update", "bulk_update", "bulk_partial_update"}
)

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
            logger.exception("frisian_mcp discovery failed")
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

        # Skip UI ViewSets — those that explicitly declare renderer_classes with no
        # JSONRenderer subclass.  Some frameworks use a BrowsableAPIRenderer subclass
        # (not TemplateHTMLRenderer) for their UI ViewSets, so the previous
        # TemplateHTMLRenderer check missed them.  The safe discriminator: if
        # renderer_classes is explicitly set on the
        # class and contains NO JSONRenderer subclass, the ViewSet produces only HTML
        # and is not a REST API surface.  ViewSets that use the api_settings default
        # (renderer_classes not explicitly set) are left untouched.
        if _JSONRenderer is not None:
            explicit_renderers: list[type] | None = getattr(cls, "renderer_classes", None)
            if explicit_renderers:
                has_json = any(
                    isinstance(r, type) and issubclass(r, _JSONRenderer)
                    for r in explicit_renderers
                )
                if not has_json:
                    logger.debug(
                        "frisian_mcp: skipping %s — no JSONRenderer in "
                        "renderer_classes (UI ViewSet)",
                        cls.__name__,
                    )
                    return

        actions: dict[str, str] = getattr(view_func, "actions", {})
        # The full URL path is needed (a) to derive the resource name when
        # no basename is set, and (b) for PKG-22 collision-resolution: the
        # merge step prefers tools whose URL path contains '/api/' so an
        # API ViewSet wins over a UI ViewSet that happens to share the
        # same model object name.  PKG-23: regex anchors (``^`` and ``$``)
        # from ``re_path``-style patterns are stripped so the stored value
        # is a clean path string suitable for prefix / equality / display.
        full_path = _REGEX_ANCHOR_RE.sub("", prefix + str(pattern.pattern))
        # Prefer the router-assigned basename (set in initkwargs by DRF's DefaultRouter /
        # SimpleRouter) — it is always the correct resource name regardless of URL shape.
        # Fall back to path-based derivation for hand-written URL confs without a router.
        basename: str | None = getattr(view_func, "initkwargs", {}).get("basename")
        if basename:
            resource = str(basename).replace("-", "_")
        else:
            resource = _resource_from_path(full_path)
            logger.warning(
                "frisian_mcp: basename not set for %s; falling back to path-derived resource %r. "
                "Set basename in your router or URL conf to avoid ambiguous tool names.",
                cls.__name__,
                resource,
            )

        include_actions, exclude_actions = _action_filters(cls)

        for http_method, action_name in actions.items():
            if include_actions is not None and action_name not in include_actions:
                logger.debug(
                    "frisian_mcp: skipping %s.%s — not in mcp_include_actions",
                    cls.__name__,
                    action_name,
                )
                continue
            if action_name in exclude_actions:
                logger.debug(
                    "frisian_mcp: skipping %s.%s — listed in mcp_exclude_actions",
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
            _write_http = {"post", "put", "patch", "delete"}
            is_write = http_method in _write_http
            permission_tier = "read_write" if is_write else "read"

            tool_name = f"{resource}{_tool_name_separator()}{action_name}"
            input_schema = self.get_input_schema(cls, action_name)
            _apply_required_overrides(input_schema, tool_name)

            if is_write:
                input_schema = {
                    **input_schema,
                    "properties": {
                        **input_schema.get("properties", {}),
                        "verify": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Return the full serialised response instead of the lean "
                                "confirmation envelope. High token cost — use only for "
                                "field-level verification after a write."
                            ),
                        },
                    },
                }

            tools.append(
                ToolDefinition(
                    name=tool_name,
                    description=_action_description(cls, action_name, resource),
                    input_schema=input_schema,
                    permission_classes=(),
                    source="auto",
                    view_class=cls,
                    action=action_name,
                    permission_tier=permission_tier,
                    url_path=full_path,
                    is_write=is_write,
                )
            )
            logger.debug("frisian_mcp discovered tool %s.%s", resource, action_name)

    def _schema_from_viewset(self, view_class: type, action: str) -> dict[str, Any]:
        """Attempt to derive a JSON Schema from a ViewSet's serializer."""
        try:
            viewset = view_class()
            # Use a minimal stub request so that get_serializer_class() implementations
            # that inspect self.request.method or self.request.user do not raise
            # AttributeError.  The stub carries the most common write-action method
            # (POST) and an anonymous user to avoid any auth-dependent branching.
            viewset.request = types.SimpleNamespace(
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
                "frisian_mcp: schema derivation failed for %s.%s — falling back to empty schema. "
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
                "frisian_mcp: custom action %s.%s has no typed parameters — "
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
            "frisian_mcp: signature introspection failed for %s.%s",
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
            elif (
                _DjangoFilterBackend is not None
                and issubclass(backend_class, _DjangoFilterBackend)
            ) or (
                _DjangoFilterBackend is None and name == "DjangoFilterBackend"
            ):
                properties.update(_filterset_properties(view_class))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "frisian_mcp: filter backend introspection failed for %s on %s",
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
            # base_filters is the standard attribute (declared + auto-generated from Meta.fields).
            # declared_filters is the fallback for custom FilterSet subclasses that only populate
            # explicit declarations without auto-generating from Meta.
            filters_dict: dict[str, Any] = (
                getattr(filterset_class, "base_filters", None)
                or getattr(filterset_class, "declared_filters", None)
                or {}
            )
            for field_name, filter_obj in filters_dict.items():
                if field_name not in properties:
                    label: Any = getattr(filter_obj, "label", None)
                    properties[field_name] = {
                        "type": "string",
                        "description": str(label) if label else f"Filter by {field_name}",
                    }
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "frisian_mcp: filterset_class introspection failed for %s",
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
    from right to left, skipping versioning/routing prefixes recognised by
    :func:`_is_version_segment` (defaults: ``api``, ``rest``, ``v1`` … ``v5``;
    override via ``FRISIAN_MCP_VERSION_SEGMENTS`` or
    ``FRISIAN_MCP_VERSION_SEGMENT_PATTERN``).  Returns the first non-version
    segment found, or ``"unknown"`` when none exists.

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
        if not _is_version_segment(name):
            return name
    return "unknown"


def _action_description(view_class: type, action: str, resource: str | None = None) -> str:
    """
    Build a human-readable tool description from the ViewSet and action.

    Resolution order:

    1. First non-empty line of the action method's docstring — specific and
       authored by the developer.
    2. Generic label derived from *resource* (or the class name when *resource*
       is not supplied) and the action name.

    Args:
        view_class: The DRF ViewSet class.
        action: The action name (e.g. ``"list"``, ``"summary"``).
        resource: The canonical resource name already computed by the caller
            (e.g. ``"users"``).  When ``None`` the name is derived from the
            ViewSet class name as a fallback — useful for direct calls outside
            the discovery pipeline (e.g. tests).

    Returns:
        A non-empty, non-null description string.

    """
    # Priority 1: action method docstring.
    action_method = getattr(view_class, action, None)
    if action_method is not None:
        doc: str = getattr(action_method, "__doc__", None) or ""
        first_line = doc.strip().split("\n")[0].strip().rstrip(".")
        if first_line:
            return first_line

    # Priority 2: generic label from the canonical resource name.
    # When resource is not supplied (e.g. direct test calls), derive from the
    # class name.  Guard against an empty result (e.g. a class named "ViewSet")
    # by falling back to the full class name.
    if resource is None:
        resource = (
            view_class.__name__.replace("ViewSet", "") or view_class.__name__
        )
    action_labels: dict[str, str] = {
        "list": f"List {resource} objects",
        "retrieve": f"Retrieve a {resource} object by ID",
        "create": f"Create a new {resource} object",
        "update": f"Replace a {resource} object by ID",
        "partial_update": f"Partially update a {resource} object by ID",
        "destroy": f"Delete a {resource} object by ID",
    }
    return action_labels.get(action, f"Invoke {view_class.__name__}.{action}")


def _field_to_schema(field: Any) -> dict[str, Any]:
    """
    Map a DRF :class:`~rest_framework.fields.Field` to a JSON Schema fragment.

    Handles four cases that the simple class-name table cannot:

    * :class:`~rest_framework.relations.ManyRelatedField` (M2M) — emits
      ``{"type": "array", "items": _FK_ITEM_SCHEMA}`` so callers can submit
      arrays of bare keys or dict references.
    * :class:`~rest_framework.relations.RelatedField` (FK, including
      ``PrimaryKeyRelatedField``, ``SlugRelatedField``, ``HyperlinkedRelatedField``)
      — emits :data:`_FK_ITEM_SCHEMA` so either bare-string or dict form
      passes the dispatcher.
    * :class:`~rest_framework.serializers.ListSerializer` (write-many nested
      serializers) — emits ``{"type": "array"}`` with a permissive object
      item schema.
    * Tag-style M2M fields (``django-taggit-serializer``'s
      ``TagListSerializerField`` / ``TagSerializerField`` and similar custom
      fields) detected by class-name to avoid an optional dependency import.

    Falls back to :data:`_FIELD_TO_JSON_TYPE` lookup keyed on the class name
    for plain scalar fields.
    """
    if isinstance(field, ManyRelatedField):
        # When the child relation is a SlugRelatedField, each item is a bare
        # slug string — not a UUID or dict form.  Emit an array-of-strings
        # schema so the normalization layer does not incorrectly wrap items.
        child = getattr(field, "child_relation", None)
        if isinstance(child, SlugRelatedField):
            return {"type": "array", "items": {"type": "string"}}
        # ContentTypeField (class name: "ContentTypeField") accepts bare
        # "app_label.model" strings — same contract as SlugRelatedField.
        # Detected by class name to avoid a hard import from the host app.
        child_class_name = type(child).__name__ if child is not None else ""
        if "ContentType" in child_class_name:
            return {"type": "array", "items": {"type": "string"}}
        return {"type": "array", "items": dict(_FK_ITEM_SCHEMA)}
    if isinstance(field, SlugRelatedField):
        # SlugRelatedField accepts a bare slug string — the host serializer
        # handles the slug→object lookup internally.  Emitting the full
        # oneOf FK schema would cause the pre-flight normalization in
        # SyncInvocation to incorrectly wrap "my-slug" as {"name": "my-slug"}.
        return {"type": "string"}
    if isinstance(field, RelatedField):
        # ContentTypeField accepts bare "app_label.model" strings (not dict form).
        if "ContentType" in type(field).__name__:
            return {"type": "string"}
        return dict(_FK_ITEM_SCHEMA)
    if isinstance(field, ListSerializer):
        return {"type": "array", "items": {"type": "object"}}

    field_class_name = type(field).__name__
    # Tag-style M2M fields (django-taggit-serializer's TagListSerializerField
    # and similar host-app subclasses) wrap a list of tag-name strings.
    # Class-name match keeps taggit an optional dependency.
    if "Tag" in field_class_name and field_class_name.endswith(
        ("SerializerField", "ListSerializerField")
    ):
        return {"type": "array", "items": {"type": "string"}}

    return {"type": _FIELD_TO_JSON_TYPE.get(field_class_name, "string")}


def _infer_required(field: Any, field_name: str) -> bool:
    """
    Infer whether a ``RelatedField`` that declares ``required=False`` is effectively required.

    Inspects the underlying Django model field to detect the create/partial_update
    mismatch — serializers often set ``required=False`` so one serializer works for
    both verbs, but the model field may be ``NOT NULL`` / no default.

    Many DRF host apps set ``required=False`` on FK serializer fields so the
    same serializer works for both ``create`` and ``partial_update`` (PATCH).
    The model field, however, may be ``NOT NULL`` with no default, meaning any
    ``create`` that omits the field will fail at the DB layer with a cryptic
    constraint error.  This function catches that mismatch so the dispatcher
    schema marks the field as required.

    Returns ``True`` when **all** of the following hold:

    * *field* is a :class:`~rest_framework.relations.RelatedField` but NOT a
      :class:`~rest_framework.relations.SlugRelatedField` (slug fields work
      with bare strings and are handled separately).
    * The field has a Django QuerySet with an accessible ``.model`` attribute.
    * The corresponding model field is ``NOT NULL`` (``null=False``) and has
      no Django-level default (``has_default()`` returns ``False``).

    Falls back to ``False`` on any introspection failure so that a non-standard
    queryset or computed field never raises during discovery.

    The repeated paragraph above is intentional — it duplicates the lead-in for
    the docstring body so both the module-level and inline readers get full
    context.  DRF apps commonly set ``required=False`` on FK fields so the same
    serializer works for both ``create`` and ``partial_update`` (PATCH).  The
    model field, however, may be ``NOT NULL`` with no default, meaning any
    ``create`` that omits the field will fail at the DB layer with a cryptic
    constraint error.  This function catches that
    mismatch so the dispatcher schema marks the field as required.

    Returns ``True`` when **all** of the following hold:

    * *field* is a :class:`~rest_framework.relations.RelatedField` but NOT a
      :class:`~rest_framework.relations.SlugRelatedField` (slug fields work
      with bare strings and are handled separately).
    * The field has a Django QuerySet with an accessible ``.model`` attribute.
    * The corresponding model field is ``NOT NULL`` (``null=False``) and has
      no Django-level default (``has_default()`` returns ``False``).

    Falls back to ``False`` on any introspection failure so that a non-standard
    queryset or computed field never raises during discovery.
    """
    if not isinstance(field, RelatedField) or isinstance(field, SlugRelatedField):
        return False
    queryset = getattr(field, "queryset", None)
    if queryset is None or not hasattr(queryset, "model"):
        return False
    model = queryset.model
    # Use field.source when set (e.g. source="device_role"); fall back to the
    # serializer field name which matches the model attribute in the common case.
    source = getattr(field, "source", None) or field_name
    try:
        model_field = model._meta.get_field(source)  # pylint: disable=protected-access
        return (
            not getattr(model_field, "null", True)
            and not model_field.has_default()
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _apply_required_overrides(schema: dict[str, Any], tool_name: str) -> None:
    """
    Apply ``FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES`` to *schema* in place.

    Reads the operator-supplied override dict from Django settings and merges
    any extra required field names into ``schema["required"]``.  Fields already
    present in ``required`` are silently deduplicated.  No-ops when the setting
    is absent, empty, or has no entry for *tool_name*.

    This is an escape hatch for FK fields that introspection cannot detect as
    required (e.g. computed properties, GenericForeignKeys, or fields whose
    queryset is resolved at runtime rather than declared on the serializer).

    Args:
        schema: The JSON Schema dict produced by :func:`get_input_schema`.
            Modified in place.
        tool_name: The fully-qualified tool name (``"resource.action"``).

    """
    overrides: dict[str, list[str]] = (
        getattr(settings, "FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES", None) or {}
    )
    extra = [f for f in overrides.get(tool_name, []) if f not in schema.get("required", [])]
    if extra:
        current = list(schema.get("required", []))
        schema["required"] = sorted(set(current) | set(extra))


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
        prop: dict[str, Any] = _field_to_schema(field)
        help_text = getattr(field, "help_text", None)
        if help_text:
            prop["description"] = str(help_text)
        properties[field_name] = prop
        # PKG-25: also mark a field required when the serializer says required=False
        # but the underlying model field is NOT NULL with no default — a common
        # pattern in host apps that share one serializer for create and partial_update.
        if getattr(field, "required", False) or _infer_required(field, field_name):
            required.append(field_name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
