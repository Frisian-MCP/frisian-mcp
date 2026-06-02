"""
Group dispatcher: bundle multiple resource tools under a single MCP tool.

Configured via ``settings.FRISIAN_MCP_DISPATCH_GROUPS``::

    FRISIAN_MCP_DISPATCH_GROUPS = {
        "catalog": ["product", "category", "tag"],
        "orders":  ["order", "line_item"],
    }

Each group becomes ONE MCP tool (e.g. ``"catalog"``) that accepts
``{"resource": "product", "action": "list", "params": {...}}``.  The dispatcher
routes to the already-registered flat tool ``device.list`` via
:meth:`~frisian_mcp.registry.ToolRegistry.dispatch`, which keeps schema
validation and tier enforcement in one place.

The grouped flat tools remain in the registry (so direct invocation still
works for advanced callers and the dispatcher can route to them) but are
hidden from ``tools/list`` so that MCP client context windows are not
overwhelmed by the full tool list.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.http import HttpRequest

if TYPE_CHECKING:
    from frisian_mcp.registry import ToolRegistry

logger = logging.getLogger(__name__)

_TIER_RANK: dict[str, int] = {"read": 0, "read_write": 1, "admin": 2}


def _parse_tool_name(
    tool_name: str,
    sep: str,
    resource_prefixes: frozenset[str] | None,
) -> tuple[str, str] | None:
    """
    Split *tool_name* into ``(resource, action)`` using the configured separator.

    When *resource_prefixes* is supplied, prefix matching is used so that
    multi-word resources (e.g. ``location_type``) are correctly identified
    even when the separator is ``"_"``.  Falls back to a plain ``split`` when
    no prefixes are available.  Returns ``None`` when the name cannot be parsed.
    """
    if resource_prefixes:
        for prefix in resource_prefixes:
            if tool_name.startswith(f"{prefix}{sep}"):
                return prefix, tool_name[len(prefix) + len(sep):]
        return None
    parts = tool_name.split(sep, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else None


def _resolve_request_tier(request: HttpRequest) -> str:
    """
    Return the effective permission tier for *request*.

    Delegates to :func:`frisian_mcp.registry._resolve_request_tier` so group
    dispatchers apply the same caller-tier rules — including the
    ``FRISIAN_MCP_RESOLVE_TIER`` callable hook and ``FRISIAN_MCP_TOKEN_TIER_MAP``
    role map — as ``@mcp_dispatcher`` and the registry itself.
    """
    # Local import: avoids a hard module-load cycle with registry, which
    # imports backends.* lazily.  Cheap at call time.
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        _resolve_request_tier as _registry_resolve,
    )

    return _registry_resolve(request)


def build_group_input_schema() -> dict[str, Any]:
    """
    Return the inputSchema for a group dispatcher tool.

    The schema accepts a free-form ``resource`` and ``action`` plus a nested
    ``params`` object forwarded to the underlying tool.  Concrete resource and
    action names are intentionally NOT enumerated in the schema — the value of
    a group dispatcher is precisely that the schema stays small regardless of
    how many tools it bundles.  Callers discover the catalogue via
    ``action="help"``.
    """
    return {
        "type": "object",
        "properties": {
            "resource": {
                "type": "string",
                "description": (
                    "Resource name to invoke (e.g. 'device'). "
                    "Use action='help' to list available resources."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "Action to invoke on the resource (e.g. 'list', 'retrieve'). "
                    "Use action='help' for the full resource/action tree."
                ),
            },
            "params": {
                "type": "object",
                "additionalProperties": True,
                "description": "Parameters forwarded to the underlying tool.",
            },
        },
    }


def build_group_help(
    group_name: str,
    tool_names: list[str],
    registry: ToolRegistry,
    max_tier: str | None = None,
    hints: dict[str, str] | None = None,
    resource: str | None = None,
    resource_prefixes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """
    Return the structured help payload for a group dispatcher.

    Full-group response (no *resource*)::

        {
            "help": True,
            "group": "catalog",
            "resources": {
                "product":  ["list", "retrieve", ...],
                "category": ["list", ...],
            },
            "hints": {           # only present when FRISIAN_MCP_TOOL_HINTS has entries
                "product.create": "Requires a category to exist first.",
            },
        }

    Resource-scoped response (when *resource* is supplied)::

        {
            "help": True,
            "group": "catalog",
            "resource": "product",
            "actions": ["create", "list", "retrieve"],
            "hints": {
                "product.create": "Requires a category to exist first.",
            },
        }

    When *max_tier* is supplied, the action lists are filtered to only the
    actions visible at or below that tier — matching the filtering applied to
    ``tools/list`` so unauthenticated callers cannot enumerate write/admin
    actions via ``action="help"``.

    Args:
        group_name: Name of the dispatcher group (e.g. ``"catalog"``).
        tool_names: Ordered list of tool names in the group
            (e.g. ``["product.list", "product.create", "category.list"]``).
        registry: The active tool registry used to look up permission tiers.
        max_tier: When supplied, actions whose tier rank exceeds this value
            are hidden (mirrors the ``tools/list`` filtering).
        hints: Operator-supplied hint strings keyed by tool name, pre-filtered
            to tools in this group by the caller.  Omit or pass ``None`` to
            suppress the ``"hints"`` key entirely.
        resource: When supplied, return a resource-scoped view listing only
            that resource's actions and its matching hints.
        resource_prefixes: When supplied (by :func:`make_group_invoke` via
            ``FRISIAN_MCP_DISPATCH_GROUPS``), used for prefix-aware splitting
            of tool names so that multi-word resources (e.g. ``location_type``)
            are correctly identified even when the separator is ``"_"``.

    Returns:
        A ``dict`` whose ``"help"`` key is ``True``.

    """
    sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
    max_rank = _TIER_RANK.get(max_tier, 2) if max_tier is not None else 2
    resources_map: dict[str, list[str]] = {}
    for tool_name in tool_names:
        parsed = _parse_tool_name(tool_name, sep, resource_prefixes)
        if parsed is None:
            continue
        entry = registry.get_entry(tool_name)
        if entry is None:
            continue
        if _TIER_RANK.get(entry.permission_tier, 0) > max_rank:
            continue
        resources_map.setdefault(parsed[0], []).append(parsed[1])

    if resource is not None:
        # Resource-scoped view: just show this resource's actions + its hints.
        payload: dict[str, Any] = {
            "help": True,
            "group": group_name,
            "resource": resource,
            "actions": sorted(resources_map.get(resource, [])),
        }
        if hints:
            resource_hints = {k: v for k, v in hints.items() if k.startswith(f"{resource}{sep}")}
            if resource_hints:
                payload["hints"] = resource_hints
        return payload

    payload = {
        "help": True,
        "group": group_name,
        "resources": {r: sorted(acts) for r, acts in resources_map.items()},
    }
    if hints:
        payload["hints"] = hints
    return payload


def make_group_invoke(
    group_name: str,
    tool_names: frozenset[str],
    registry: ToolRegistry,
    resource_prefixes: frozenset[str] | None = None,
) -> Callable[[dict[str, Any], HttpRequest], Any]:
    """
    Build the invoke callable for a group dispatcher.

    The returned function accepts ``(arguments, request)`` and:

    * Returns the help tree when ``action`` is missing or equal to ``"help"``.
    * Routes ``{"resource": R, "action": A, "params": P}`` to the registered
      tool ``f"{R}{sep}{A}"`` (where *sep* is ``FRISIAN_MCP_TOOL_NAME_SEPARATOR``)
      via ``registry.dispatch(request, name, P)`` — reusing the registry's
      schema validation, tier enforcement, and argument normalisation.
    * Raises :exc:`LookupError` for resource/action pairs not in the group,
      with a ``difflib`` "did you mean?" suggestion against known resources.

    Args:
        group_name: Dispatcher group name (e.g. ``"catalog"``).
        tool_names: Frozenset of the flat tool names bundled in this group.
        registry: The active :class:`~frisian_mcp.registry.ToolRegistry`.
        resource_prefixes: The resource prefix strings that were used to
            select *tool_names* (from ``FRISIAN_MCP_DISPATCH_GROUPS``).  When
            supplied, passed through to :func:`build_group_help` for
            prefix-aware name splitting (avoids ambiguity for multi-word
            resources when the separator is ``"_"``).

    """

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> Any:
        sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
        action: str | None = arguments.get("action")
        resource: str | None = arguments.get("resource")
        # Accept both nested {action, resource, params: {...}} and flat
        # {action, resource, key: val} forms — same convention as
        # @mcp_dispatcher's invoke (see backends/dispatcher.py).
        params: dict[str, Any] = arguments.get("params") or {
            k: v for k, v in arguments.items() if k not in ("action", "resource")
        }

        if action is None or action == "help":
            raw_hints: dict[str, str] = (
                getattr(settings, "FRISIAN_MCP_TOOL_HINTS", None) or {}
            )
            group_hints = {k: v for k, v in raw_hints.items() if k in tool_names}
            return build_group_help(
                group_name,
                sorted(tool_names),
                registry,
                max_tier=_resolve_request_tier(request),
                hints=group_hints or None,
                resource=resource,
                resource_prefixes=resource_prefixes,
            )

        if resource is None:
            raise ValueError(
                f"resource is required for non-help actions on group {group_name!r}"
            )

        target_name = f"{resource}{sep}{action}"
        if target_name not in tool_names:
            available_resources = sorted(
                resource_prefixes
                if resource_prefixes is not None
                else {t.split(sep, 1)[0] for t in tool_names}
            )
            matches = difflib.get_close_matches(resource, available_resources, n=1)
            hint = f" Did you mean resource={matches[0]!r}?" if matches else ""
            raise LookupError(
                f"Unknown tool {target_name!r} in group {group_name!r}.{hint}"
            )

        return registry.dispatch(request, target_name, params)

    return invoke
