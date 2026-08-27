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
from typing import TYPE_CHECKING, Any, NoReturn

from django.conf import settings
from django.http import HttpRequest

from frisian_mcp.backends.dispatcher import _reject_misplaced_continuation_token
from frisian_mcp.backends.invocation import _LIST_BODY_KEYS
from frisian_mcp.negotiation import NEGOTIATION_PROTOCOL_ONLY_KEY, _merge_negotiation_schema
from frisian_mcp.registry import _TIER_RANK, _caller_rank, _parse_tool_name

if TYPE_CHECKING:
    from frisian_mcp.registry import ToolRegistry

logger = logging.getLogger(__name__)


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
    # pylint: disable=import-outside-toplevel
    from frisian_mcp.registry import _resolve_request_tier as _registry_resolve

    # pylint: enable=import-outside-toplevel

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

    The response-negotiation fields are merged in before returning.  ADR-005's
    "Decision" defines redemption as re-invoking *the same tool* with a
    ``continuation_token`` and a ``mode``; for a grouped tool that same tool is
    this dispatcher, so the redemption input surface has to be reachable
    through this schema.  Without the merge the probe envelope advertises
    ``available_modes`` while the published schema gives the caller no legal
    slot to send the token back — the token is minted and can never be
    redeemed.
    """
    schema: dict[str, Any] = {
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
                "description": (
                    # The 'continuation_token' placement rule is NOT repeated
                    # here: _NEGOTIATION_PROPERTIES already publishes "Place at
                    # the TOP LEVEL of arguments — NOT inside 'params'." into
                    # this same schema via the flat merge, so the reader was
                    # being told twice (CR-7).
                    #
                    # What follows is NOT redundant and must stay: nothing else
                    # in the schema says these four keys are top-level *only*
                    # on a continuation call.  The negotiation field
                    # descriptions are deliberately shape-neutral and cannot
                    # carry it, and all four collide with real host data.
                    "Parameters forwarded to the underlying tool."
                    " 'mode', 'page', 'page_size' and 'filter_keys' are"
                    " top-level ONLY on a continuation call (i.e. alongside a"
                    " 'continuation_token'); otherwise they are ordinary action"
                    " parameters and belong here."
                ),
            },
            "lite": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Suppress instructional scaffolding in the response "
                    "(action listings, hint text, parameter descriptions). "
                    "On failure the tool schema is re-included in the error."
                ),
            },
        },
    }
    return _merge_negotiation_schema(schema)


def _required_fields_by_action(
    resource: str,
    actions: list[str],
    registry: ToolRegistry,
    sep: str,
) -> dict[str, list[str]]:
    """
    Return ``{action: [required field names]}`` for *resource*, derived facts only.

    **Read from the registered ``input_schema``, never re-derived from the
    serializer.**  That schema is the artifact ``ToolRegistry.dispatch``
    validates against, so what ``help`` promises and what validation enforces
    are the same object read twice.  Introspecting DRF a second time here would
    create a source that can disagree with the validator, and "help said one
    thing, validation wanted another" is a worse defect than the silence this
    replaces.

    Actions with no required fields are omitted rather than mapped to an empty
    list: ``help`` should carry information, not padding, and an absent key
    already means "nothing is required".

    ``continuation_token`` and its four negotiation companions are protocol
    fields, not action parameters, and never appear in an action's ``required``.
    No filtering is needed for them.
    """
    requires: dict[str, list[str]] = {}
    for action in actions:
        entry = registry.get_entry(f"{resource}{sep}{action}")
        if entry is None:
            continue
        schema = getattr(entry, "input_schema", None)
        if not isinstance(schema, dict):
            continue
        required = schema.get("required")
        if isinstance(required, list) and required:
            requires[action] = sorted(str(name) for name in required)
    return requires


def _bulk_params_disclosure(actions: list[str]) -> dict[str, Any] | None:
    """
    Return the accepted wrapper shape for ``bulk_*`` actions, or ``None``.

    ``params`` is typed ``object``, so the natural array form is rejected by
    schema validation before the host is reached.  The list has to be wrapped
    in a single key — and until now nothing in ``help`` or the published schema
    named one, which made every ``bulk_*`` action advertised and effectively
    uncallable.

    The key set is **derived from the constant the request path actually uses**
    (:data:`frisian_mcp.backends.invocation._LIST_BODY_KEYS`) rather than
    restated here, for the same reason the required list is read off the
    registered schema: a restatement is a second source that can drift from the
    behaviour it describes.
    """
    if not any(action.startswith("bulk") for action in actions):
        return None
    accepted = sorted(_LIST_BODY_KEYS)
    # "objects" is the convention both call sites use in their own examples;
    # fall back to the first accepted key so this cannot raise if the set moves.
    canonical = "objects" if "objects" in accepted else accepted[0]
    return {
        "wrap_list_in_one_of": accepted,
        "example": {canonical: [{"...": "..."}]},
    }


def build_group_help(  # pylint: disable=too-many-locals
    group_name: str,
    tool_names: list[str],
    registry: ToolRegistry,
    max_tier: str | None = None,
    hints: dict[str, str] | None = None,
    resource: str | None = None,
    resource_prefixes: frozenset[str] | None = None,
    entry_filter: Callable[[Any], bool] | None = None,
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
        entry_filter: Optional callable applied to each ``_ToolEntry`` after
            tier filtering.  Return ``False`` to exclude a tool from the help
            listing.  Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to
            hide tools the requesting user lacks Django permission for, so
            ``action="help"`` respects the same filtering as ``tools/list``.

    Returns:
        A ``dict`` whose ``"help"`` key is ``True``.

    """
    sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
    # H7: an unrecognised caller tier previously defaulted to rank 2 (admin)
    # here, so a garbled or denied tier listed the ENTIRE group in help — the
    # opposite of every other gate in the package.  ``_caller_rank`` ranks an
    # unknown value below read, so it now hides everything.  ``max_tier=None``
    # still means "no cap".
    max_rank = _caller_rank(max_tier) if max_tier is not None else 2
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
        if entry_filter is not None and not entry_filter(entry):
            continue
        resources_map.setdefault(parsed[0], []).append(parsed[1])

    def _filter_hints(raw: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in raw.items():
            hint_entry = registry.get_entry(k)
            if hint_entry is None:
                # Unknown tool — only include when no capability filter is active.
                if entry_filter is None:
                    out[k] = v
                continue
            if _TIER_RANK.get(hint_entry.permission_tier, 0) > max_rank:
                continue
            if entry_filter is not None and not entry_filter(hint_entry):
                continue
            out[k] = v
        return out

    if resource is not None:
        # Resource-scoped view: just show this resource's actions + its hints.
        resource_actions = sorted(resources_map.get(resource, []))
        payload: dict[str, Any] = {
            "help": True,
            "group": group_name,
            "resource": resource,
            "actions": resource_actions,
        }
        requires = _required_fields_by_action(resource, resource_actions, registry, sep)
        if requires:
            payload["requires"] = requires
        bulk_params = _bulk_params_disclosure(resource_actions)
        if bulk_params:
            payload["bulk_params"] = bulk_params
        if hints:
            resource_hints = _filter_hints(
                {k: v for k, v in hints.items() if k.startswith(f"{resource}{sep}")}
            )
            if resource_hints:
                payload["hints"] = resource_hints
        return payload

    payload = {
        "help": True,
        "group": group_name,
        "resources": {r: sorted(acts) for r, acts in resources_map.items()},
    }
    if hints:
        filtered = _filter_hints(hints)
        if filtered:
            payload["hints"] = filtered
    return payload


def make_group_invoke(  # pylint: disable=too-many-locals
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
        _reject_misplaced_continuation_token(arguments)
        # Accept both nested {action, resource, params: {...}} and flat
        # {action, resource, key: val} forms — same convention as
        # @mcp_dispatcher's invoke (see backends/dispatcher.py).
        #
        # continuation_token is excluded from the flat sweep because it is never
        # a legitimate action parameter (T6).  Note the exclusion set is NOT the
        # class dispatcher's: a group call also carries `resource`, which must
        # stay out of params or every flat-form group call breaks.  The other
        # negotiation fields are deliberately NOT excluded — they collide with
        # real host data (a model field named `mode`; DRF's own page/page_size)
        # and must reach the action untouched.
        params: dict[str, Any] = arguments.get("params") or {
            k: v
            for k, v in arguments.items()
            if k not in ("action", "resource", NEGOTIATION_PROTOCOL_ONLY_KEY)
        }

        if action is None or action == "help":
            raw_hints: dict[str, str] = getattr(settings, "FRISIAN_MCP_TOOL_HINTS", None) or {}
            group_hints = {k: v for k, v in raw_hints.items() if k in tool_names}
            # Apply Django-permission entry filter when FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY
            # is enabled, so action="help" respects the same filtering as tools/list.
            perm_entry_filter = getattr(request, "_mcp_perm_entry_filter", None)
            return build_group_help(
                group_name,
                sorted(tool_names),
                registry,
                max_tier=_resolve_request_tier(request),
                hints=group_hints or None,
                resource=resource,
                resource_prefixes=resource_prefixes,
                entry_filter=perm_entry_filter,
            )

        if resource is None:
            raise ValueError(f"resource is required for non-help actions on group {group_name!r}")

        target_name = f"{resource}{sep}{action}"

        def _raise_unknown_tool() -> NoReturn:
            # H3: the suggestion candidates pass the same capability lens as
            # tools/list.  They did not, and the consequence was measurable: a
            # caller holding nothing saw an empty tools/list and still got
            # "Did you mean resource='secret'?" — discovery removed the
            # resource, the error surface handed it back with the correct
            # spelling.
            #
            # H9 adds the TIER lens beside it, because the capability lens alone
            # left the same hole open one dimension over.  This function is
            # reached two ways: the `target_name not in tool_names` branch below,
            # which runs BEFORE the `_mcp_max_tier` check, and that check itself,
            # which calls here deliberately so an above-ceiling tool raises "the
            # exact error a never-registered action gets".  Filtering on
            # capability only, that promise did not survive the hint:
            #
            #     never registered   ->  Unknown tool 'widget_list' in group 'svc'.
            #     above the ceiling  ->  Unknown tool 'secret_list' in group 'svc'.
            #                            Did you mean resource='secret'?
            #
            # The absence error named the resource it was hiding, so the two
            # cases were trivially distinguishable — a tier oracle inside the
            # error written to prevent one (V11-20/F3).
            #
            # Both lenses are skipped when they do not apply: `caps is None`
            # means permission-aware discovery is off or the caller is
            # unrestricted, and `_mcp_max_tier is None` is an uncapped legacy
            # mount, which keeps `registry.dispatch`'s tier-error behaviour
            # unchanged rather than acquiring a new one here.
            caps = getattr(request, "_mcp_capabilities", None)
            max_tier = getattr(request, "_mcp_max_tier", None)
            visible_names = tool_names
            if caps is not None or max_tier is not None:
                # pylint: disable=import-outside-toplevel
                from frisian_mcp.contrib.permissions.base import entry_is_visible

                caller_rank = _caller_rank(_resolve_request_tier(request))

                def _is_visible(name: str) -> bool:
                    entry = registry.get_entry(name)
                    if entry is None:
                        return False
                    if caps is not None and not entry_is_visible(entry, caps):
                        return False
                    if max_tier is not None and caller_rank < _TIER_RANK.get(
                        entry.permission_tier, 0
                    ):
                        return False
                    return True

                visible_names = frozenset(name for name in tool_names if _is_visible(name))

            # Parse with the prefix-aware splitter, not ``split(sep, 1)``.  A
            # naive split reduces ``location_type_list`` to ``location``, which
            # then intersects to nothing against a configured prefix set naming
            # ``location_type`` — so a multi-word resource had no visible prefix,
            # could not enter the action branch, and produced no hint at all on
            # either axis.  ``_parse_tool_name`` is the same helper the routing
            # path uses, so the hint vocabulary and the routing vocabulary
            # cannot disagree about where a name divides.
            _parsed = (
                (name, _parse_tool_name(name, sep, resource_prefixes)) for name in visible_names
            )
            visible_prefixes = {parsed[0] for _name, parsed in _parsed if parsed is not None}
            if resource_prefixes is not None:
                # Configured prefixes are the display vocabulary, but only those
                # still backed by a visible member may be named.
                visible_prefixes &= set(resource_prefixes)

            # H9 (axis): this fires whenever the COMPOSED name misses, which
            # includes a correct resource paired with a wrong action.  Matching
            # on the resource in that case returns it as its own best match, so
            # the hint named back the half the caller got right and said nothing
            # about the half they got wrong — "Did you mean resource='item'?" in
            # answer to resource='item', action='lst'.  Pick the axis the caller
            # actually missed.
            if resource in visible_prefixes:
                # Action candidates come from the same parse, so a multi-word
                # resource yields its real actions rather than a suffix that
                # still carries part of the resource name.
                candidates = sorted(
                    parsed[1]
                    for parsed in (
                        _parse_tool_name(name, sep, resource_prefixes) for name in visible_names
                    )
                    if parsed is not None and parsed[0] == resource
                )
                matches = difflib.get_close_matches(action, candidates, n=1)
                hint = f" Did you mean action={matches[0]!r}?" if matches else ""
            else:
                matches = difflib.get_close_matches(resource, sorted(visible_prefixes), n=1)
                hint = f" Did you mean resource={matches[0]!r}?" if matches else ""
            raise LookupError(f"Unknown tool {target_name!r} in group {group_name!r}.{hint}")

        if target_name not in tool_names:
            _raise_unknown_tool()

        _target_entry = registry.get_entry(target_name)

        # V11-20 (F3): the route tier cap defines what exists on this door; the
        # permission-aware member filter below only refines what exists.  An
        # above-ceiling action must therefore be absent HERE — before the
        # filter can name it in a permission error, and before the inner
        # registry.dispatch can raise a differently-shaped ToolNotFoundError —
        # using the exact error a never-registered action gets, so the two are
        # indistinguishable at invoke just as they are in discovery.  Uncapped
        # legacy mounts keep registry.dispatch's tier-error behaviour.
        if (
            _target_entry is not None
            and getattr(request, "_mcp_max_tier", None) is not None
            and _caller_rank(_resolve_request_tier(request))
            < _TIER_RANK.get(_target_entry.permission_tier, 0)
        ):
            _raise_unknown_tool()

        # Strip frisian-mcp protocol params before the underlying tool sees params.
        # `verify` is a write-path flag; `lite` is a protocol-level flag on all calls.
        # DRF serializers may reject unknown fields.  views.py reads both from the
        # original top-level arguments before dispatch.
        if _target_entry is not None:
            _strip = {"lite"}
            if _target_entry.is_write:
                _strip.add("verify")
            if _strip.intersection(params):
                params = {k: v for k, v in params.items() if k not in _strip}

        # Enforce capability filter at dispatch time so that callers who already
        # know a tool's name cannot bypass FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY
        # by calling the group dispatcher directly.  The filter is the same
        # callable that tools/list uses — only fires when the feature is enabled.
        _perm_filter = getattr(request, "_mcp_perm_entry_filter", None)
        if (
            _perm_filter is not None
            and _target_entry is not None
            and not _perm_filter(_target_entry)
        ):
            raise PermissionError(
                f"You do not have permission to use "
                f"{resource!r}/{action!r} in group {group_name!r}."
            )

        return registry.dispatch(request, target_name, params)

    return invoke
