"""
Group dispatcher: bundle multiple resource tools under a single MCP tool.

Configured via ``settings.FRIESE_MCP_DISPATCH_GROUPS``::

    FRIESE_MCP_DISPATCH_GROUPS = {
        "dcim": ["device", "rack", "interface"],
        "ipam": ["ipaddress", "prefix", "vlan"],
    }

Each group becomes ONE MCP tool (e.g. ``"dcim"``) that accepts
``{"resource": "device", "action": "list", "params": {...}}``.  The dispatcher
routes to the already-registered flat tool ``device.list`` via
:meth:`~friese_mcp.registry.ToolRegistry.dispatch`, which keeps schema
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

from django.http import HttpRequest

if TYPE_CHECKING:
    from friese_mcp.registry import ToolRegistry

logger = logging.getLogger(__name__)

_TIER_RANK: dict[str, int] = {"read": 0, "read_write": 1, "admin": 2}


def _resolve_request_tier(request: HttpRequest) -> str:
    """
    Return the effective permission tier for *request*.

    Delegates to :func:`friese_mcp.registry._resolve_request_tier` so group
    dispatchers apply the same caller-tier rules — including the
    ``FRIESE_MCP_RESOLVE_TIER`` callable hook and ``FRIESE_MCP_TOKEN_TIER_MAP``
    role map — as ``@mcp_dispatcher`` and the registry itself.
    """
    # Local import: avoids a hard module-load cycle with registry, which
    # imports backends.* lazily.  Cheap at call time.
    from friese_mcp.registry import (  # pylint: disable=import-outside-toplevel
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
) -> dict[str, Any]:
    """
    Return the structured help payload for a group dispatcher.

    The response shape is::

        {
            "help": True,
            "group": "dcim",
            "resources": {
                "device": ["list", "retrieve", ...],
                "rack":   ["list", ...],
            },
        }

    When *max_tier* is supplied, the action lists are filtered to only the
    actions visible at or below that tier — matching the filtering applied to
    ``tools/list`` so unauthenticated callers cannot enumerate write/admin
    actions via ``action="help"``.
    """
    max_rank = _TIER_RANK.get(max_tier, 2) if max_tier is not None else 2
    resources: dict[str, list[str]] = {}
    for tool_name in tool_names:
        if "." not in tool_name:
            continue
        resource, action = tool_name.split(".", 1)
        entry = registry.get_entry(tool_name)
        if entry is None:
            continue
        if _TIER_RANK.get(entry.permission_tier, 0) > max_rank:
            continue
        resources.setdefault(resource, []).append(action)
    return {
        "help": True,
        "group": group_name,
        "resources": {r: sorted(actions) for r, actions in resources.items()},
    }


def make_group_invoke(
    group_name: str,
    tool_names: frozenset[str],
    registry: ToolRegistry,
) -> Callable[[dict[str, Any], HttpRequest], Any]:
    """
    Build the invoke callable for a group dispatcher.

    The returned function accepts ``(arguments, request)`` and:

    * Returns the help tree when ``action`` is missing or equal to ``"help"``.
    * Routes ``{"resource": R, "action": A, "params": P}`` to the registered
      tool ``f"{R}.{A}"`` via ``registry.dispatch(request, name, P)`` —
      reusing the registry's schema validation, tier enforcement, and
      argument normalisation.
    * Raises :exc:`LookupError` for resource/action pairs not in the group,
      with a ``difflib`` "did you mean?" suggestion against known resources.
    """

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> Any:
        action: str | None = arguments.get("action")
        resource: str | None = arguments.get("resource")
        # Accept both nested {action, resource, params: {...}} and flat
        # {action, resource, key: val} forms — same convention as
        # @mcp_dispatcher's invoke (see backends/dispatcher.py).
        params: dict[str, Any] = arguments.get("params") or {
            k: v for k, v in arguments.items() if k not in ("action", "resource")
        }

        if action is None or action == "help":
            return build_group_help(
                group_name,
                sorted(tool_names),
                registry,
                max_tier=_resolve_request_tier(request),
            )

        if resource is None:
            raise ValueError(
                f"resource is required for non-help actions on group {group_name!r}"
            )

        target_name = f"{resource}.{action}"
        if target_name not in tool_names:
            available_resources = sorted({t.split(".", 1)[0] for t in tool_names})
            matches = difflib.get_close_matches(resource, available_resources, n=1)
            hint = f" Did you mean resource={matches[0]!r}?" if matches else ""
            raise LookupError(
                f"Unknown tool {target_name!r} in group {group_name!r}.{hint}"
            )

        return registry.dispatch(request, target_name, params)

    return invoke
