"""Decorators for explicit MCP tool registration and auto-discovery exclusion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from rest_framework.permissions import BasePermission

from frisian_mcp.negotiation import _merge_negotiation_schema
from frisian_mcp.registry import tool_registry
from frisian_mcp.resources import ResourceDefinition, resource_registry

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])
_AnyT = TypeVar("_AnyT")
_ClassT = TypeVar("_ClassT", bound=type)


def mcp_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    permission_classes: list[type[BasePermission]] | None = None,
    write: bool = False,
    admin: bool = False,
    capability: str | None = None,
    universal_discovery: bool = False,
) -> Callable[[_CallableT], _CallableT]:
    """
    Register the decorated callable as a named MCP tool.

    The decorated function must accept ``(arguments: dict[str, Any],
    request: HttpRequest)`` and return a JSON-serialisable value.

    Example::

        @mcp_tool(
            name="orders.cancel",
            description="Cancel an order by ID.",
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "integer"}},
                "required": ["order_id"],
            },
            permission_classes=[IsAuthenticated],
        )
        def cancel_order(
            arguments: dict[str, Any], request: HttpRequest
        ) -> dict[str, Any]:
            ...

    Args:
        name: Unique MCP tool name (e.g. ``"orders.cancel"``).
        description: Human-readable description shown in ``tools/list``.
        input_schema: JSON Schema (draft-07) for argument validation.
        permission_classes: DRF permission classes that guard this tool.
            Pass ``None`` or ``[]`` for unrestricted access.
        write: Set ``True`` to assign ``permission_tier="read_write"``.
        admin: Set ``True`` to assign ``permission_tier="admin"``.
        capability: The Django permission string a caller must hold for this
            tool to appear in ``tools/list`` under
            ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` (e.g.
            ``"orders.view_order"``).  Decorator tools have no ViewSet queryset
            for discovery to derive this from, so declaring it is the only way
            they participate in capability filtering.
        universal_discovery: Declare this tool intentionally visible to every
            caller.  Without either this or *capability*, the tool's capability
            is indeterminate and it is **hidden** when permission-aware
            discovery is on — visibility to everyone must be stated, never
            inherited from missing metadata.

    Returns:
        The original callable, unchanged, registered as a side-effect.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        tool_registry.register(
            name=name,
            fn=fn,
            description=description,
            capability=capability,
            universal_discovery=universal_discovery,
            # H2 (narrowed, CR-2): the caller's schema is published EXACTLY as
            # handed to us.  ``@mcp_tool`` deliberately does not disclose the
            # continuation call.
            #
            # H2 originally disclosed here, reasoning that anything reachable by
            # the size backstop must publish the shape the backstop mints
            # against.  The premise was right; the direction was wrong.  The
            # invariant to preserve is "never mint a token the caller cannot
            # legally return", and it is enforced on the *mint* side:
            # ``schema_discloses_continuation()`` gates the backstop in
            # ``views.py`` against this same object, so a schema that stays
            # silent never mints.  Disclosing universally bought that invariant
            # by inflating every ordinary tool's published schema by ~380 tokens
            # (measured, CR-1) — an additive cost, paid on every ``tools/list``,
            # by hosts that never asked for negotiation.
            #
            # This is not a new fallback.  ``merge_continuation_branch`` already
            # refused to disclose for any schema declaring
            # ``"additionalProperties": false`` (H18), and already documented
            # the consequence: an over-threshold response "is returned whole".
            # CR-2 widens that existing, documented path from closed schemas to
            # every ordinary ``@mcp_tool`` registration.  Whole is literally
            # whole — the full payload, with nothing pinned in the heavy cache.
            #
            # A host that wants negotiation on a hand-registered tool has an
            # explicit way to ask for it: ``@mcp_heavy``, which still discloses
            # via ``_merge_negotiation_schema`` below and is the purpose of this
            # PR.  Negotiation is opt-in for ordinary tools, not imposed on them.
            input_schema=input_schema,
            permission_classes=permission_classes,
            permission_tier="admin" if admin else "read_write" if write else "read",
        )
        return fn

    return decorator


def mcp_action(
    name: str,
    description: str,
    params: dict[str, str] | None = None,
    input_schema: dict[str, Any] | None = None,
    write: bool = False,
    admin: bool = False,
    backend_action: str | None = None,
) -> Callable[[_CallableT], _CallableT]:
    """
    Mark a method as a dispatchable action on an ``@mcp_dispatcher`` class.

    The decorator stores metadata on the function without altering its behaviour.
    ``@mcp_dispatcher`` scans for this marker when the class is decorated.

    Args:
        name: Action name used in ``action`` parameter dispatch (e.g. ``"create"``).
        description: Human-readable description shown in help-mode responses.
        params: Optional mapping of param names to human-readable hints.
        input_schema: Optional JSON Schema (draft-07) for per-call validation.
        write: Set ``True`` to assign ``permission_tier="read_write"``.
        admin: Set ``True`` to assign ``permission_tier="admin"``.
        backend_action: Django permission verb (``"view"``, ``"add"``, ``"change"``,
            ``"delete"``) for non-CRUD actions.  Required when
            ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` is on and the action name
            is not one of the standard CRUD names.

    Returns:
        The original method, unchanged, with ``_mcp_action_meta`` attached.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        fn._mcp_action_meta = {  # type: ignore[attr-defined]  # pylint: disable=protected-access
            "name": name,
            "description": description,
            "params": params or {},
            "input_schema": input_schema,
            "permission_tier": "admin" if admin else "read_write" if write else "read",
            "backend_action": backend_action,
        }
        return fn

    return decorator


def mcp_dispatcher(
    name: str,
    description: str,
    permission_classes: list[type[BasePermission]] | None = None,
    capability: str | None = None,
    universal_discovery: bool = False,
) -> Callable[[_ClassT], _ClassT]:
    """
    Register a class as a named MCP dispatcher tool.

    Each method decorated with ``@mcp_action`` becomes a dispatchable action.
    The class is instantiated once at decoration time; ``request`` is passed
    per-call.  Callers may omit ``action`` or pass ``action="help"`` to receive
    a structured listing of all actions.

    Args:
        name: Unique MCP tool name (e.g. ``"tasks"``).
        description: Human-readable description shown in ``tools/list``.
        permission_classes: DRF permission classes that guard this dispatcher.
            Pass ``None`` or ``[]`` for unrestricted access.
        capability: The ``"app_label.model"`` base this dispatcher's per-action
            verbs resolve against under
            ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` (e.g.
            ``"catalog.item"``, which combines with each action's verb to give
            ``"catalog.add_item"``).  A class dispatcher has no ViewSet queryset
            to derive this from; without it every action is hidden and the
            dispatcher is dropped as an empty navigation entry point.
        universal_discovery: Publish this dispatcher and its full action enum
            to every caller.  Must be stated explicitly — an indeterminate
            dispatcher is hidden, not universally visible.

    Returns:
        The original class, unchanged, registered as a side-effect.

    """

    def decorator(cls: _ClassT) -> _ClassT:
        # pylint: disable=import-outside-toplevel
        from frisian_mcp.backends.dispatcher import (
            ActionEntry,
            DispatcherMeta,
            _build_dispatcher_input_schema,
            _make_dispatcher_invoke,
        )

        action_map: dict[str, ActionEntry] = {}
        for attr in vars(cls).values():
            if callable(attr):
                action_meta = getattr(attr, "_mcp_action_meta", None)
                if action_meta is not None:
                    entry = ActionEntry(
                        name=action_meta["name"],
                        description=action_meta["description"],
                        params=action_meta["params"],
                        input_schema=action_meta["input_schema"],
                        method=attr,
                        permission_tier=action_meta.get("permission_tier", "read"),
                        backend_action=action_meta.get("backend_action"),
                    )
                    action_map[entry.name] = entry

        meta = DispatcherMeta(name=name, description=description, actions=action_map)
        invoke_fn = _make_dispatcher_invoke(cls, meta)
        input_schema = _build_dispatcher_input_schema(meta)
        # Dispatchers are always registered as "read" so they appear in tools/list
        # for all callers — they are navigation entry points, not gated resources.
        # Per-action permission enforcement happens inside invoke_fn at dispatch time.
        # The dispatcher meta is stashed on the registry entry so that
        # ToolRegistry.list_tools(max_tier=...) can rebuild the inputSchema with
        # the action enum filtered to only the actions visible at the caller's
        # tier — preventing write/admin action names from leaking through
        # tools/list to unauthenticated callers.
        tool_registry.register(
            name=name,
            fn=invoke_fn,
            description=description,
            input_schema=input_schema,
            permission_classes=permission_classes,
            is_dispatcher=True,
            permission_tier="read",
            dispatcher_meta=meta,
            capability=capability,
            universal_discovery=universal_discovery,
        )
        return cls

    return decorator


def mcp_resource(
    uri_template: str,
    name: str,
    description: str = "",
    mime_type: str = "text/plain",
) -> Callable[[_CallableT], _CallableT]:
    """
    Register the decorated callable as an MCP resource.

    The decorated function must accept ``(uri: str, request: HttpRequest)``
    and return the resource contents as a string.

    Args:
        uri_template: Resource URI (may use ``{variable}`` placeholders).
        name: Human-readable resource name shown in ``resources/list``.
        description: Optional description shown in ``resources/list``.
        mime_type: MIME type of the returned content.  Defaults to ``"text/plain"``.

    Returns:
        The original callable, unchanged, registered as a side-effect.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        resource_registry.register(
            ResourceDefinition(
                uri_template=uri_template,
                name=name,
                fn=fn,
                description=description,
                mime_type=mime_type,
            )
        )
        return fn

    return decorator


def mcp_heavy(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    permission_classes: list[type[BasePermission]] | None = None,
    write: bool = False,
    admin: bool = False,
    capability: str | None = None,
    universal_discovery: bool = False,
) -> Callable[[_CallableT], _CallableT]:
    """
    Register the decorated callable as a heavy MCP tool with response-negotiation.

    Heavy tools use a two-call protocol to avoid overloading agent context windows:

    **Call 1 — probe** (no ``continuation_token``): the tool executes and its result is
    cached.  The caller receives a probe envelope::

        {
            "preview": "<first 200 chars of the serialised result>",
            "total_size": <byte count>,
            "available_modes": ["summary", "paginated", "filtered", "full"],
            "continuation_token": "<opaque token>"
        }

    **Call 2 — fetch** (with ``continuation_token`` + ``mode``): re-invoke the same tool
    with the token from call 1.  Available modes:

    * ``summary``   — top-level keys / first 5 list items; values truncated to 100 chars
    * ``paginated`` — one page of a list result; pass ``page`` (default 1) and
      ``page_size`` (default ``FRISIAN_MCP_HEAVY_PAGE_SIZE`` or 20)
    * ``filtered``  — result filtered to the keys named in ``filter_keys``
    * ``full``      — complete original result

    The five negotiation fields (``continuation_token``, ``mode``, ``page``,
    ``page_size``, ``filter_keys``) are automatically merged into the registered
    ``inputSchema`` so that ``tools/list`` exposes the protocol to clients.

    **Secondary backstop (v2):** ``FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD`` — a byte-count
    integer; a successful **non-write** response above that size is automatically wrapped
    in a probe envelope **when the tool's published schema discloses the continuation
    call**.  Since CR-2 that means ``@mcp_heavy`` and the dispatchers; it is deliberately
    not universal.

    The gate is disclosure, not decoration — a group or class dispatcher carries no
    ``@mcp_heavy`` decorator and still probes, because its published schema declares the
    protocol.  Conversely an ordinary ``@mcp_tool`` registration or a plain auto-discovered
    action publishes the host schema unchanged, so however large its response, it is
    **returned whole** and mints nothing.  A host wanting probe-first behaviour on such a
    tool asks for it by decorating with ``@mcp_heavy`` or mounting behind a dispatcher.

    (Writes return their lean confirmation envelope *before* this backstop is reached, and
    ``@mcp_heavy`` tools use their own probe path — so the backstop covers disclosing
    reads, list and detail, plus custom tool output.)  It defaults to ``25000`` bytes
    (~25 KB) so high-cardinality reads on those shapes probe-first out of the box; set it
    to ``None`` in Django settings to disable the backstop, or to a larger value to probe
    less often.  This setting is secondary; prefer ``@mcp_heavy`` for explicit heavy tools.

    Args:
        name: Unique MCP tool name (e.g. ``"enterprise.list_all_tools"``).
        description: Human-readable description shown in ``tools/list``.
        input_schema: JSON Schema (draft-07) for argument validation.  Negotiation
            fields are merged in automatically.
        permission_classes: DRF permission classes that guard this tool.
        write: Set ``True`` to assign ``permission_tier="read_write"``.
        admin: Set ``True`` to assign ``permission_tier="admin"``.
        capability: Django permission string the caller must hold for this tool
            to appear under ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` (H3).
            Omitted leaves the tool indeterminate, which fails closed — it is
            hidden, and ``W015`` names it at startup.
        universal_discovery: Set ``True`` to declare the tool intentionally
            visible to every caller, exempting it from capability filtering.
            Explicit by design: intent must be stated, never inferred from
            absent metadata.

    Returns:
        The original callable, unchanged, registered as a side-effect.

    Example::

        @mcp_heavy(
            name="enterprise.search_tools",
            description="Search all 150+ enterprise tools.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        def search_tools(arguments: dict, request: HttpRequest) -> dict:
            return {"tools": [...]}  # potentially very large

    """

    def decorator(fn: _CallableT) -> _CallableT:
        tool_registry.register(
            name=name,
            fn=fn,
            description=description,
            input_schema=_merge_negotiation_schema(input_schema),
            permission_classes=permission_classes,
            capability=capability,
            universal_discovery=universal_discovery,
            is_heavy=True,
            permission_tier="admin" if admin else "read_write" if write else "read",
        )
        return fn

    return decorator


def mcp_ignore(obj: _AnyT) -> _AnyT:
    """
    Mark a ViewSet class or action method so auto-discovery skips it.

    Sets ``_mcp_ignore = True`` on the target; the auto-discovery pass
    inspects this attribute before registering tools.

    Can be applied to an entire ViewSet class or to individual action methods::

        @mcp_ignore
        class InternalViewSet(ModelViewSet):
            ...

        class UserViewSet(ModelViewSet):
            @mcp_ignore
            def private_action(self, request: HttpRequest) -> Response:
                ...

    Args:
        obj: A ViewSet class or action method to exclude from MCP discovery.

    Returns:
        The original object, unchanged except for the ``_mcp_ignore`` marker.

    """
    obj._mcp_ignore = True  # type: ignore[attr-defined]  # pylint: disable=protected-access
    return obj
