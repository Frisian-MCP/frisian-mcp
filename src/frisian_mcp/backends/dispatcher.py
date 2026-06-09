"""Runtime support for @mcp_dispatcher class-based tool dispatchers."""

# pylint: disable=cyclic-import

from __future__ import annotations

import dataclasses
import difflib
from collections.abc import Callable
from typing import Any

import jsonschema
import jsonschema.exceptions
from django.http import HttpRequest

from frisian_mcp.registry import ToolInputError

_TIER_RANK: dict[str, int] = {"read": 0, "read_write": 1, "admin": 2}


@dataclasses.dataclass
class ActionEntry:
    """Metadata and callable for a single dispatcher action."""

    name: str
    description: str
    params: dict[str, str]
    input_schema: dict[str, Any] | None
    method: Callable[..., Any]
    permission_tier: str = "read"
    backend_action: str | None = None


@dataclasses.dataclass
class DispatcherMeta:
    """Aggregated metadata for a registered @mcp_dispatcher class."""

    name: str
    description: str
    actions: dict[str, ActionEntry]


def _visible_actions(
    meta: DispatcherMeta,
    max_tier: str | None,
    action_filter: Callable[[str, ActionEntry], bool] | None = None,
) -> dict[str, ActionEntry]:
    """
    Return the subset of *meta.actions* visible at *max_tier*.

    ``max_tier=None`` returns all actions (back-compat for callers that have
    not opted into tier-aware filtering, e.g. internal execution paths).  Any
    recognised tier name (``"read"``, ``"read_write"``, ``"admin"``) returns
    only actions whose ``permission_tier`` rank is at or below the caller's
    tier rank.  Unknown tier strings collapse to ``"read"`` to avoid silently
    exposing privileged actions to misconfigured callers.

    *action_filter*, when supplied, is applied after tier filtering.  It
    receives ``(action_name, action_entry)`` and should return ``False`` to
    hide an action.  Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to
    strip write/delete actions from the enum when the user lacks the
    corresponding Django permission.
    """
    if max_tier is None:
        candidates = dict(meta.actions)
    else:
        max_rank = _TIER_RANK.get(max_tier, 0)
        candidates = {
            name: entry
            for name, entry in meta.actions.items()
            if _TIER_RANK.get(entry.permission_tier, 0) <= max_rank
        }
    if action_filter is None:
        return candidates
    return {name: entry for name, entry in candidates.items() if action_filter(name, entry)}


def _build_dispatcher_input_schema(
    meta: DispatcherMeta,
    max_tier: str | None = None,
    action_filter: Callable[[str, ActionEntry], bool] | None = None,
) -> dict[str, Any]:
    """
    Return the compact inputSchema for a dispatcher tool.

    When *max_tier* is supplied, the ``action`` enum is filtered to only the
    actions visible at or below that tier, so unauthenticated and
    lower-privilege callers never see write/admin action names in
    ``tools/list``.  When *max_tier* is ``None`` the full enum is returned
    (legacy/internal behaviour).

    *action_filter* applies an additional predicate after tier filtering —
    used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to hide actions for
    which the requesting user lacks the corresponding Django permission.
    """
    visible = _visible_actions(meta, max_tier, action_filter=action_filter)
    # Build a self-documenting params description so agents that only read the
    # top-level schema (and don't call help) can see per-action parameter names.
    param_hints = "; ".join(
        f"{name}: {{{', '.join(entry.params.keys())}}}" if entry.params else f"{name}: (no params)"
        for name, entry in visible.items()
    )
    params_description = f"Action-specific parameters. {param_hints}."
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(visible.keys()),
                "description": (
                    "Operation to perform. Omit or use 'help' to list all available"
                    " actions and their required parameters."
                ),
            },
            "params": {
                "type": "object",
                "additionalProperties": True,
                "description": params_description,
            },
        },
    }


def _build_help_response(
    meta: DispatcherMeta,
    max_tier: str | None = None,
    action_filter: Callable[[str, ActionEntry], bool] | None = None,
) -> dict[str, Any]:
    """
    Return the structured help payload for a dispatcher.

    When *max_tier* is supplied, only actions visible at or below that tier
    are listed — matching the filtering applied to ``tools/list`` so that
    unauthenticated callers cannot enumerate write/admin actions via
    ``action="help"``.  When *action_filter* is supplied it is applied on top
    of tier filtering to hide actions the requesting user lacks Django
    permission for, mirroring the ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY``
    filtering applied to the action enum in ``tools/list``.
    """
    visible = _visible_actions(meta, max_tier, action_filter=action_filter)
    return {
        "help": True,
        "dispatcher": meta.name,
        "actions": [
            {
                "name": e.name,
                "description": e.description,
                "params": e.params,
                "input_schema": e.input_schema,
            }
            for e in visible.values()
        ],
    }


def _resolve_request_tier(request: HttpRequest) -> str:
    """
    Return the effective permission tier for *request*.

    Delegates to :func:`frisian_mcp.registry._resolve_request_tier` so the full
    resolution chain (``FRISIAN_MCP_RESOLVE_TIER`` callable, token attribute,
    ``FRISIAN_MCP_TOKEN_TIER_MAP`` role map, fallback) is applied in one place.
    Retained as a thin module-local shim so callers in this module need not
    take a cross-module dependency.
    """
    # Local import: registry imports backends.dispatcher lazily inside
    # ToolRegistry.list_tools() to avoid a hard cycle, so reaching the other
    # direction at module load would create one.  Resolving here at call time
    # is cheap and keeps both modules importable in any order.
    # pylint: disable=import-outside-toplevel
    from frisian_mcp.registry import _resolve_request_tier as _registry_resolve

    # pylint: enable=import-outside-toplevel
    return _registry_resolve(request)


def _build_perm_action_filter_from_request(
    request: HttpRequest,
    tool_name: str,
) -> Callable[[str, ActionEntry], bool] | None:
    """
    Build a Django-permission action filter for *tool_name* using capabilities cached on *request*.

    Returns ``None`` when permission-aware discovery is disabled, the user is unrestricted,
    or the tool has no ``perm_app_label``/``perm_model`` metadata.
    Called by the ``action="help"`` branch of dispatcher ``invoke`` so that help responses
    respect the same permission filtering as the ``tools/list`` action enum.
    """
    caps: frozenset[str] | None = getattr(request, "_mcp_capabilities", None)
    if caps is None:
        return None
    # Local imports avoid circular deps (registry imports dispatcher lazily).
    from frisian_mcp.contrib.permissions.base import (  # pylint: disable=import-outside-toplevel
        _DRF_ACTION_TO_PERM_VERB,
    )
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        tool_registry,
    )

    entry = tool_registry.get_entry(tool_name)
    if entry is None or not entry.perm_app_label or not entry.perm_model:
        return None
    app_label: str = entry.perm_app_label
    model: str = entry.perm_model

    def action_filter(action_name: str, action_entry: ActionEntry) -> bool:
        verb = action_entry.backend_action or _DRF_ACTION_TO_PERM_VERB.get(action_name, "view")
        return f"{app_label}.{verb}_{model}" in caps

    return action_filter


def _make_dispatcher_invoke(cls: type, meta: DispatcherMeta) -> Callable[..., dict[str, Any]]:
    """Build the invoke callable for *cls*, closing over *meta*."""
    instance = cls()
    action_map = meta.actions

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> dict[str, Any]:
        action: str | None = arguments.get("action")
        # Accept both nested {action, params: {...}} and flat {action, key: val} forms.
        # Schema-driven agents (GPT function-calling) pass args flat; reasoning agents
        # use the params wrapper. Fall back to flat when params is absent or empty.
        params: dict[str, Any] = arguments.get("params") or {
            k: v for k, v in arguments.items() if k != "action"
        }

        if action is None or action == "help":
            # Filter the help response to only actions the caller can see, so
            # that action="help" cannot be used to bypass tools/list-level
            # tier filtering and enumerate privileged actions.  The Django-permission
            # action filter is also applied when FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY
            # is on, matching the enum filtering in tools/list.
            perm_action_filter = _build_perm_action_filter_from_request(request, meta.name)
            return _build_help_response(
                meta,
                max_tier=_resolve_request_tier(request),
                action_filter=perm_action_filter,
            )

        if action not in action_map:
            matches = difflib.get_close_matches(action, action_map.keys(), n=1)
            hint = f" Did you mean: {matches[0]!r}?" if matches else ""
            raise LookupError(f"Unknown action {action!r}.{hint}")

        entry = action_map[action]

        # Action-level permission tier check. Dispatchers always appear in
        # tools/list (tier="read") but individual actions may require higher
        # permissions. Check here rather than at tools/list time.
        #
        # The caller's effective tier is resolved via :func:`_resolve_request_tier`,
        # which handles all three cases uniformly:
        #
        # * ``request.auth is None`` (unauthenticated)        → ``FRISIAN_MCP_UNAUTHENTICATED_TIER``
        #   (default ``"read"``)
        # * ``request.auth`` without ``.permission`` attr     → ``"read"`` (most conservative)
        # * ``request.auth.permission`` set                   → that value
        #
        # The previous implementation only enforced when ``auth.permission`` was
        # truthy, which silently let unauthenticated callers invoke write/admin
        # actions — a critical authorisation bypass.
        caller_tier = _resolve_request_tier(request)
        caller_rank = _TIER_RANK.get(caller_tier, 0)
        action_rank = _TIER_RANK.get(entry.permission_tier, 0)
        if caller_rank < action_rank:
            raise PermissionError(
                f"Action {action!r} requires {entry.permission_tier!r} permission; "
                f"caller has {caller_tier!r} permission."
            )

        if entry.input_schema is not None:
            try:
                jsonschema.validate(instance=params, schema=entry.input_schema)
            except jsonschema.exceptions.ValidationError as exc:
                raise ToolInputError(
                    f"Invalid params for action {action!r}: {exc.message}"
                ) from exc

        return entry.method(instance, request, params)  # type: ignore[no-any-return]

    return invoke
