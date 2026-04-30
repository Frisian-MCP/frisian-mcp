"""Runtime support for @mcp_dispatcher class-based tool dispatchers."""

from __future__ import annotations

import dataclasses
import difflib
from collections.abc import Callable
from typing import Any

import jsonschema
import jsonschema.exceptions
from django.conf import settings
from django.http import HttpRequest

from friese_mcp.registry import ToolInputError

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


@dataclasses.dataclass
class DispatcherMeta:
    """Aggregated metadata for a registered @mcp_dispatcher class."""

    name: str
    description: str
    actions: dict[str, ActionEntry]


def _visible_actions(
    meta: DispatcherMeta, max_tier: str | None
) -> dict[str, ActionEntry]:
    """
    Return the subset of *meta.actions* visible at *max_tier*.

    ``max_tier=None`` returns all actions (back-compat for callers that have
    not opted into tier-aware filtering, e.g. internal execution paths).  Any
    recognised tier name (``"read"``, ``"read_write"``, ``"admin"``) returns
    only actions whose ``permission_tier`` rank is at or below the caller's
    tier rank.  Unknown tier strings collapse to ``"read"`` to avoid silently
    exposing privileged actions to misconfigured callers.
    """
    if max_tier is None:
        return dict(meta.actions)
    max_rank = _TIER_RANK.get(max_tier, 0)
    return {
        name: entry
        for name, entry in meta.actions.items()
        if _TIER_RANK.get(entry.permission_tier, 0) <= max_rank
    }


def _build_dispatcher_input_schema(
    meta: DispatcherMeta, max_tier: str | None = None
) -> dict[str, Any]:
    """
    Return the compact inputSchema for a dispatcher tool.

    When *max_tier* is supplied, the ``action`` enum is filtered to only the
    actions visible at or below that tier, so unauthenticated and
    lower-privilege callers never see write/admin action names in
    ``tools/list``.  When *max_tier* is ``None`` the full enum is returned
    (legacy/internal behaviour).
    """
    visible = _visible_actions(meta, max_tier)
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
                "description": "Parameters for the chosen action. See help for details.",
            },
        },
    }


def _build_help_response(
    meta: DispatcherMeta, max_tier: str | None = None
) -> dict[str, Any]:
    """
    Return the structured help payload for a dispatcher.

    When *max_tier* is supplied, only actions visible at or below that tier
    are listed — matching the filtering applied to ``tools/list`` so that
    unauthenticated callers cannot enumerate write/admin actions via
    ``action="help"``.
    """
    visible = _visible_actions(meta, max_tier)
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

    Mirrors :func:`friese_mcp.views._get_token_permission`:

    * ``request.auth is None`` → ``FRIESE_MCP_UNAUTHENTICATED_TIER``
      (default ``"read"``).
    * ``request.auth`` without a ``.permission`` attribute → ``"read"``
      (most conservative; unknown auth backends never silently expose
      higher tiers).
    * ``request.auth.permission`` set → that value.
    """
    auth_obj = getattr(request, "auth", None)
    if auth_obj is None:
        return str(getattr(settings, "FRIESE_MCP_UNAUTHENTICATED_TIER", "read"))
    return str(getattr(auth_obj, "permission", "read"))


def _make_dispatcher_invoke(
    cls: type, meta: DispatcherMeta
) -> Callable[..., dict[str, Any]]:
    """Build the invoke callable for *cls*, closing over *meta*."""
    instance = cls()
    action_map = meta.actions

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> dict[str, Any]:
        action: str | None = arguments.get("action")
        params: dict[str, Any] = arguments.get("params") or {}

        if action is None or action == "help":
            # Filter the help response to only actions the caller can see, so
            # that action="help" cannot be used to bypass tools/list-level
            # tier filtering and enumerate privileged actions.
            return _build_help_response(meta, max_tier=_resolve_request_tier(request))

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
        # * ``request.auth is None`` (unauthenticated)        → ``FRIESE_MCP_UNAUTHENTICATED_TIER``
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

        return entry.method(instance, request, params)  # type: ignore[return-value]

    return invoke
