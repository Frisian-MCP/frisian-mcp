"""Runtime support for @mcp_dispatcher class-based tool dispatchers."""

# pylint: disable=cyclic-import

from __future__ import annotations

import dataclasses
import difflib
from collections.abc import Callable, Container
from typing import Any, NoReturn

import jsonschema
import jsonschema.exceptions
from django.http import HttpRequest

from frisian_mcp.negotiation import NEGOTIATION_PROTOCOL_ONLY_KEY, _merge_negotiation_schema
from frisian_mcp.registry import _TIER_RANK, ToolInputError, _caller_rank


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
        max_rank = _caller_rank(max_tier)
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
    params_description = (
        f"Action-specific parameters. {param_hints}."
        " 'continuation_token' is never an action parameter — it is always a"
        " top-level sibling of 'action' and 'params'. 'mode', 'page',"
        " 'page_size' and 'filter_keys' are top-level ONLY on a continuation"
        " call (i.e. alongside a 'continuation_token'); otherwise they are"
        " ordinary action parameters and belong here."
    )
    schema: dict[str, Any] = {
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
    # ADR-005 "Decision" — "Subsequent data is fetched by re-invoking the same
    # tool with the `continuation_token` and a `mode`" — makes this tool the
    # redemption surface, so its published schema has to admit those fields.
    #
    # Cited by quoted phrase, not line number: ADR-005 carries an Amendments
    # section and its line numbering shifts whenever it is amended.  This
    # comment previously cited "line 73", which is the *heaviness hint* clause
    # about tool selection — a real requirement, but not the authority for the
    # redemption input surface.
    #
    # Until T6 the merge was applied on the @mcp_heavy path only, so dispatcher
    # tools — and the FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD backstop, which reads
    # this same schema — advertised `available_modes` in the probe envelope
    # while never telling the agent where to put the fields.
    return _merge_negotiation_schema(schema)


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

    Returns ``None`` — meaning *publish every action* — only when permission-aware
    discovery is disabled or the user is unrestricted, i.e. when there is no
    filtering to do at all.  Everything else goes through the shared lens.

    Called by the ``action="help"`` branch of dispatcher ``invoke``, by the
    unknown-action suggestion candidates, and by the per-request schema re-emit,
    so all three agree with the ``tools/list`` action enum.

    H3: this used to end ``return None`` when the entry had no
    ``perm_app_label``/``perm_model`` — so on an indeterminate dispatcher
    ``tools/list`` denied every action while these three published every action.
    Not a gap: the exact inverse of the ruling, on the same request.  It also
    had no ``universal_discovery`` branch and no ``capability`` fallback, so an
    operator following W015's own advice got a filtered ``tools/list`` and an
    unfiltered ``action="help"`` on the same dispatcher.
    """
    caps: Container[str] | None = getattr(request, "_mcp_capabilities", None)
    if caps is None:
        return None
    # Local imports avoid circular deps (registry imports dispatcher lazily).
    from frisian_mcp.contrib.permissions.base import (  # pylint: disable=import-outside-toplevel
        build_action_filter,
        deny_all_actions,
    )
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        tool_registry,
    )

    entry = tool_registry.get_entry(tool_name)
    if entry is None:
        # An unresolvable tool is indeterminate, not unrestricted.
        return deny_all_actions
    return build_action_filter(entry, caps)


def _reject_misplaced_continuation_token(arguments: dict[str, Any]) -> None:
    """
    Raise when ``continuation_token`` is nested inside ``params`` (T6).

    ``params`` is by ADR-002 the action-specific passthrough to the underlying
    ViewSet, so a token placed there is forwarded to the filterset, which
    correctly rejects it as an unknown filter field.  That error is accurate but
    useless: it tells the agent the key is not a filter, never that the key is
    real and simply in the wrong place.  Since the response-negotiation protocol
    is the only teaching surface an agent has mid-negotiation, name the correct
    placement explicitly instead.

    Only ``continuation_token`` is checked.  It is the one negotiation field that
    can never be a legitimate filter, so misplacement is unambiguous.  ``mode``,
    ``page``, ``page_size`` and ``filter_keys`` all collide with real host data
    and are left alone — see :data:`~frisian_mcp.negotiation.NEGOTIATION_PROTOCOL_ONLY_KEY`.

    Shared by the class dispatcher and the group dispatcher, whose top-level
    argument sets differ (``action``/``params`` vs ``resource``/``action``/
    ``params``).  The message therefore names only ``params`` — the one place
    the token must not be, and the one key common to both shapes — and tells the
    caller to move it out rather than restating a full argument list that would
    be wrong for one of them.
    """
    nested = arguments.get("params")
    if isinstance(nested, dict) and NEGOTIATION_PROTOCOL_ONLY_KEY in nested:
        raise ToolInputError(
            f"{NEGOTIATION_PROTOCOL_ONLY_KEY!r} was sent inside 'params', where it is"
            " treated as an action filter and rejected. It is a response-negotiation"
            " field and belongs at the TOP LEVEL of arguments, not inside 'params'."
            " Re-send the call exactly as before, but move 'continuation_token' out of"
            " 'params' to the top level. 'mode' is optional and may be added beside it"
            " ('summary'|'paginated'|'filtered'|'full')."
            " Omitting 'mode' returns one bounded page of a list result, or the whole"
            " object for a non-list result; pass 'full' explicitly for the complete"
            " dataset."
        )


def _make_dispatcher_invoke(cls: type, meta: DispatcherMeta) -> Callable[..., dict[str, Any]]:
    """Build the invoke callable for *cls*, closing over *meta*."""
    instance = cls()
    action_map = meta.actions

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> dict[str, Any]:
        action: str | None = arguments.get("action")
        _reject_misplaced_continuation_token(arguments)
        # Accept both nested {action, params: {...}} and flat {action, key: val} forms.
        # Schema-driven agents (GPT function-calling) pass args flat; reasoning agents
        # use the params wrapper. Fall back to flat when params is absent or empty.
        #
        # continuation_token is excluded from the flat sweep because it is never a
        # legitimate action parameter (T6).  In practice views.py short-circuits a
        # top-level continuation_token before dispatch, so this is belt-and-braces —
        # but it keeps the flat form from ever reclassifying a protocol key as a
        # filter.  The other negotiation fields are deliberately NOT excluded: they
        # collide with real host data (Nautobot's dcim.Interface.mode; DRF's
        # page/page_size) and must pass through to the action untouched.
        params: dict[str, Any] = arguments.get("params") or {
            k: v for k, v in arguments.items() if k not in ("action", NEGOTIATION_PROTOCOL_ONLY_KEY)
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

        def _raise_unknown_action(name: str) -> NoReturn:
            # On a tier-capped route the did-you-mean candidates come from the
            # caller-VISIBLE action set (same tier + Django-permission lens as
            # help and the tools/list enum) — a suggestion computed over the
            # full map would name a hidden action back to the caller inside
            # the very error that denies it exists (V11-20/F3).  Uncapped
            # legacy hosts keep the full-map hint unchanged.
            #
            # H3: the CAPABILITY lens applies on every mount, not only capped
            # ones.  Gating it on `_mcp_max_tier` meant a perm-aware host on an
            # uncapped mount still suggested actions the caller cannot see —
            # the same shape as the group 404 hint, which fires before its own
            # tier check.  Tier filtering keeps the legacy uncapped behaviour
            # (max_tier=None returns every tier), so only the capability half
            # changes here.
            _capped = getattr(request, "_mcp_max_tier", None) is not None
            _perm_filter = _build_perm_action_filter_from_request(request, meta.name)
            if _capped or _perm_filter is not None:
                candidates = list(
                    _visible_actions(
                        meta,
                        _resolve_request_tier(request) if _capped else None,
                        action_filter=_perm_filter,
                    )
                )
            else:
                candidates = list(action_map)
            matches = difflib.get_close_matches(name, candidates, n=1)
            hint = f" Did you mean: {matches[0]!r}?" if matches else ""
            raise LookupError(f"Unknown action {name!r}.{hint}")

        if action not in action_map:
            _raise_unknown_action(action)

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
        caller_rank = _caller_rank(caller_tier)
        action_rank = _TIER_RANK.get(entry.permission_tier, 0)
        if caller_rank < action_rank:
            if getattr(request, "_mcp_max_tier", None) is not None:
                # V11-20 (F3): on a tier-capped route the ceiling defines which
                # actions exist; discovery already hides this one, so invoke
                # must report the same absence a never-registered action gets —
                # a tier error here would confirm the action exists and leak
                # the elevation path.  Same rule as registry.dispatch's
                # capped-tool ToolNotFoundError.
                _raise_unknown_action(action)
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
