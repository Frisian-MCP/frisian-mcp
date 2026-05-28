"""Thread-safe registry of MCP tools with JSON Schema validation and permission enforcement."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import jsonschema
import jsonschema.exceptions
from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import HttpRequest
from django.utils.module_loading import import_string
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)

_TIER_RANK: dict[str, int] = {"read": 0, "read_write": 1, "admin": 2}

#: Single-key argument dicts whose value is a list are treated as bulk-create
#: (or bulk-update/destroy) bodies.  When detected in :meth:`ToolRegistry.dispatch`
#: the required-field schema validation is skipped — the host serializer validates
#: each item individually.  Mirrors ``_LIST_BODY_KEYS`` in ``backends.invocation``.
_BULK_LIST_BODY_KEYS: frozenset[str] = frozenset(
    {"objects", "data", "items", "_items", "body"}
)

#: Recognised role-keys for ``FRIESE_MCP_TOKEN_TIER_MAP`` lookup.  Probed in
#: this order against ``request.user`` attributes — the first match wins.
_TOKEN_TIER_MAP_ROLE_PROBES: tuple[tuple[str, str], ...] = (
    ("superuser", "is_superuser"),
    ("staff", "is_staff"),
)


def _resolve_tier_hook() -> Callable[[Any], str | None] | None:
    """
    Resolve ``settings.FRIESE_MCP_RESOLVE_TIER`` to a callable.

    Accepts either an already-callable object or a dotted import path.  Returns
    ``None`` when the setting is absent or the path cannot be imported (the
    failure is logged at ERROR level so misconfigured deployments are visible
    without raising at request time).
    """
    raw = getattr(settings, "FRIESE_MCP_RESOLVE_TIER", None)
    if raw is None:
        return None
    if callable(raw):
        return raw  # type: ignore[no-any-return]
    if isinstance(raw, str):
        try:
            return import_string(raw)  # type: ignore[no-any-return]
        except (ImportError, AttributeError):
            logger.exception(
                "FRIESE_MCP_RESOLVE_TIER %r could not be imported; ignoring", raw
            )
            return None
    logger.error(
        "FRIESE_MCP_RESOLVE_TIER must be a callable or dotted-path string, got %r", type(raw)
    )
    return None


def _resolve_tier_from_role_map(request: Any) -> str | None:
    """
    Map a request's user role to a tier via ``FRIESE_MCP_TOKEN_TIER_MAP``.

    The static map keys ``superuser``, ``staff``, and ``default`` are matched
    against ``request.user`` attributes (``is_superuser``, ``is_staff``).  The
    ``default`` entry applies to any authenticated user that did not match a
    higher-privilege role.  Unauthenticated callers do NOT receive ``default``
    — they continue to ``FRIESE_MCP_UNAUTHENTICATED_TIER`` so the existing
    anonymous-rejection contract is preserved.

    Returns ``None`` when the setting is absent or no entry matches.
    """
    role_map: dict[str, str] | None = getattr(settings, "FRIESE_MCP_TOKEN_TIER_MAP", None)
    if not role_map:
        return None
    user = getattr(request, "user", None)
    if user is None:
        return None
    for role_key, user_attr in _TOKEN_TIER_MAP_ROLE_PROBES:
        if getattr(user, user_attr, False) and role_key in role_map:
            return str(role_map[role_key])
    if "default" in role_map and getattr(user, "is_authenticated", False):
        return str(role_map["default"])
    return None


def _apply_max_tier_cap(tier: str, request: Any) -> str:
    """Clamp *tier* to ``request._mcp_max_tier`` when the cap is stricter."""
    cap: str | None = getattr(request, "_mcp_max_tier", None)
    if cap is not None and _TIER_RANK.get(tier, 0) > _TIER_RANK.get(cap, 0):
        return cap
    return tier


def _resolve_request_tier(request: Any) -> str:
    """
    Return the effective MCP permission tier for *request*.

    Resolution order — first non-``None`` result wins:

    1. ``settings.FRIESE_MCP_RESOLVE_TIER`` (callable or dotted path).  Called
       with *request*.  Returning ``None`` falls through.  Exceptions are
       logged and treated as a fall-through so a broken hook cannot break the
       gateway.
    2. ``request.auth.permission`` (the historical convention; populated by
       :class:`~friese_mcp.contrib.tokens.authentication.FrieseMcpApiKeyAuthentication`
       and OAuth tokens).
    3. ``settings.FRIESE_MCP_TOKEN_TIER_MAP`` static role map keyed by
       ``superuser`` / ``staff`` / ``default`` — see
       :func:`_resolve_tier_from_role_map`.
    4. ``settings.FRIESE_MCP_UNAUTHENTICATED_TIER`` (default ``"read"``) when
       ``request.auth is None``; otherwise ``"read"`` (most conservative — an
       authenticated request with an unknown auth backend never silently
       receives a higher tier).

    After resolution, the tier is clamped to ``request._mcp_max_tier`` when
    that attribute is set (stamped by :meth:`~friese_mcp.views.McpView.post`
    from :meth:`~friese_mcp.views.McpView._effective_max_tier`).  This applies
    the ``FRIESE_MCP_MAX_TIER`` endpoint-level cap regardless of which
    resolution path was taken — including hook, token permission, and role map.

    Defined at module level so :class:`ToolRegistry` can enforce tier at
    dispatch time without importing :mod:`friese_mcp.views` (avoiding a
    circular import).
    """
    hook = _resolve_tier_hook()
    if hook is not None:
        try:
            tier = hook(request)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("FRIESE_MCP_RESOLVE_TIER hook raised; falling through")
            tier = None
        if tier is not None:
            return _apply_max_tier_cap(str(tier), request)

    auth_obj = getattr(request, "auth", None)
    if auth_obj is not None:
        explicit = getattr(auth_obj, "permission", None)
        if explicit is not None:
            return _apply_max_tier_cap(str(explicit), request)

    role_tier = _resolve_tier_from_role_map(request)
    if role_tier is not None:
        return _apply_max_tier_cap(role_tier, request)

    if auth_obj is None:
        tier = str(getattr(settings, "FRIESE_MCP_UNAUTHENTICATED_TIER", "read"))
    else:
        tier = "read"
    return _apply_max_tier_cap(tier, request)


def _camel_to_snake(name: str) -> str:
    """Convert a camelCase or PascalCase identifier to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _normalize_argument_keys(arguments: Any) -> Any:
    """
    Recursively convert all dict keys from camelCase to snake_case.

    Controlled by the ``FRIESE_MCP_NORMALIZE_INPUT_CASE`` Django setting
    (default ``True``).  Values are passed through unchanged so that string
    field content (e.g. exercise names) is never mutated.
    """
    if not isinstance(arguments, dict):
        return arguments
    return {_camel_to_snake(k): _normalize_argument_keys(v) for k, v in arguments.items()}


class ToolNotFoundError(LookupError):
    """Raised when a requested tool name is not in the registry."""


class ToolInputError(ValueError):
    """Raised when tool arguments fail JSON Schema validation."""


class ToolInvocationError(Exception):
    """
    Raised by the tool invocation shim when the backend returns is_error=True.

    Carries the original error *content* (dict or string) so that views.py can
    forward it directly to the MCP client as an ``isError: true`` response
    instead of hiding it behind the generic "Internal tool error" fallback.
    """

    def __init__(self, content: Any) -> None:
        self.content: Any = content
        super().__init__(str(content))


class _ToolEntry:  # pylint: disable=too-many-instance-attributes
    __slots__ = (
        "description",
        "dispatcher_meta",
        "fn",
        "hidden",
        "input_schema",
        "is_dispatcher",
        "is_heavy",
        "name",
        "permission_classes",
        "permission_tier",
    )

    def __init__(  # pylint: disable=too-many-arguments
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]],
        is_dispatcher: bool = False,
        is_heavy: bool = False,
        permission_tier: str = "read",
        dispatcher_meta: Any = None,
        hidden: bool = False,
    ) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.input_schema = input_schema
        self.permission_classes = permission_classes
        self.is_dispatcher = is_dispatcher
        self.is_heavy = is_heavy
        self.permission_tier = permission_tier
        # ``dispatcher_meta`` is a ``backends.dispatcher.DispatcherMeta`` for
        # tools registered via ``@mcp_dispatcher``; ``None`` for plain
        # ``@mcp_tool`` / ``@mcp_heavy`` entries.  Typed as ``Any`` to avoid
        # a circular import between ``registry`` and ``backends.dispatcher``.
        self.dispatcher_meta = dispatcher_meta
        # ``hidden`` tools remain dispatchable by name but are excluded from
        # ``list_tools()`` output — used by FRIESE_MCP_DISPATCH_GROUPS to bury
        # bundled flat tools behind their group dispatcher.
        self.hidden = hidden


class ToolRegistry:
    """
    Thread-safe registry for MCP tools.

    Tools are registered at startup via ``@mcp_tool`` or auto-discovery and
    dispatched at request time.  The module-level :data:`tool_registry`
    singleton is the primary entry point; instantiate ``ToolRegistry`` directly
    only when an isolated registry is required (e.g. in tests).
    """

    def __init__(self) -> None:
        """Initialise an empty, unlocked registry."""
        self._tools: dict[str, _ToolEntry] = {}
        self._lock: threading.Lock = threading.Lock()

    def register(  # pylint: disable=too-many-arguments
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]] | None = None,
        is_dispatcher: bool = False,
        is_heavy: bool = False,
        permission_tier: str = "read",
        dispatcher_meta: Any = None,
        hidden: bool = False,
    ) -> None:
        """
        Register a callable as a named MCP tool.

        Args:
            name: Unique tool name (e.g. ``"users.list"``).
            fn: Callable invoked as ``fn(arguments, request)``.
            description: Human-readable description for MCP tool listing.
            input_schema: JSON Schema (draft-07) describing expected arguments.
            permission_classes: DRF ``BasePermission`` subclasses that guard
                this tool.  Pass ``None`` or ``[]`` for unrestricted access;
                authentication and authorisation remain the host app's concern.
            is_dispatcher: ``True`` when the tool was registered via
                ``@mcp_dispatcher``.
            is_heavy: ``True`` when the tool was registered via ``@mcp_heavy``
                and uses the two-call response-negotiation protocol.
            permission_tier: Minimum token permission required to see this tool
                in ``tools/list``.  One of ``"read"``, ``"read_write"``, or
                ``"admin"``.  Dispatcher tools always use ``"read"`` so they
                are always visible as entry points.
            dispatcher_meta: For dispatcher tools, the
                ``backends.dispatcher.DispatcherMeta`` capturing the action
                map.  Used by ``list_tools(max_tier=...)`` to rebuild the
                ``inputSchema.action.enum`` filtered to only the caller's
                visible actions, so write/admin action names never leak via
                ``tools/list`` to lower-privilege callers.  Typed ``Any`` to
                avoid a circular import.
            hidden: When ``True``, the tool is excluded from
                :meth:`list_tools` output but remains dispatchable by name.
                Used by ``FRIESE_MCP_DISPATCH_GROUPS`` to bury bundled flat
                tools behind their group dispatcher.

        """
        with self._lock:
            self._tools[name] = _ToolEntry(
                name=name,
                fn=fn,
                description=description,
                input_schema=input_schema,
                permission_classes=list(permission_classes or []),
                is_dispatcher=is_dispatcher,
                is_heavy=is_heavy,
                permission_tier=permission_tier,
                dispatcher_meta=dispatcher_meta,
                hidden=hidden,
            )

    def get_entry(self, name: str) -> _ToolEntry | None:
        """Return the raw ``_ToolEntry`` for *name*, or ``None`` if absent."""
        with self._lock:
            return self._tools.get(name)

    def list_dispatcher_names(self) -> frozenset[str]:
        """Return the names of all tools registered via ``@mcp_dispatcher``."""
        with self._lock:
            return frozenset(
                entry.name for entry in self._tools.values() if entry.is_dispatcher
            )

    def list_names(self) -> list[str]:
        """Return a snapshot of all currently-registered tool names."""
        with self._lock:
            return list(self._tools.keys())

    def set_hidden(self, name: str, hidden: bool = True) -> bool:
        """
        Toggle the *hidden* flag on a registered tool.

        Hidden tools remain dispatchable by name but are excluded from
        :meth:`list_tools` so they do not appear in MCP ``tools/list`` output.
        Used by ``FRIESE_MCP_DISPATCH_GROUPS`` post-processing to bury
        bundled flat tools behind their group dispatcher.

        Returns ``True`` when the flag was applied, ``False`` when *name*
        is not registered.
        """
        with self._lock:
            entry = self._tools.get(name)
            if entry is None:
                return False
            entry.hidden = hidden
            return True

    def list_tools(self, max_tier: str | None = None) -> list[dict[str, Any]]:
        """
        Return the tool listing in MCP ``tools/list`` response format.

        Args:
            max_tier: When set to ``"read"``, ``"read_write"``, or ``"admin"``,
                only tools whose ``permission_tier`` is at or below this level
                are returned.  ``None`` returns all tools (legacy/internal
                behaviour, used for cache-key generation and for callers that
                opt out of tier filtering).  Dispatcher tools always use tier
                ``"read"`` so the dispatcher itself remains visible — but its
                ``inputSchema.action.enum`` is rebuilt to expose only the
                sub-actions visible at the caller's tier.  When a dispatcher
                has zero visible actions at the caller's tier, the dispatcher
                is omitted entirely so it is not advertised as a callable
                navigation entry-point with no callable actions.

        """
        max_rank = _TIER_RANK.get(max_tier, 2) if max_tier is not None else 2

        # Lazy-import to avoid a circular dependency with backends.dispatcher,
        # which itself imports from this module.
        # pylint: disable=import-outside-toplevel
        from friese_mcp.backends.dispatcher import _build_dispatcher_input_schema

        with self._lock:
            tools: list[dict[str, Any]] = []
            for entry in self._tools.values():
                if entry.hidden:
                    continue
                if _TIER_RANK.get(entry.permission_tier, 0) > max_rank:
                    continue

                # Plain (non-dispatcher) tool: include the registered schema
                # verbatim — the entry's own permission_tier already gated it
                # above, so the schema does not need filtering.
                if not entry.is_dispatcher or entry.dispatcher_meta is None:
                    tools.append(
                        {
                            "name": entry.name,
                            "description": entry.description,
                            "inputSchema": entry.input_schema,
                        }
                    )
                    continue

                # Dispatcher: rebuild the inputSchema with the action enum
                # filtered to the caller's tier.  Hide the dispatcher entirely
                # when no actions remain visible (avoids exposing an empty
                # navigation tool that can only return help with zero actions).
                filtered_schema = _build_dispatcher_input_schema(
                    entry.dispatcher_meta, max_tier=max_tier
                )
                visible_actions = filtered_schema["properties"]["action"]["enum"]
                if max_tier is not None and not visible_actions:
                    continue
                tools.append(
                    {
                        "name": entry.name,
                        "description": entry.description,
                        "inputSchema": filtered_schema,
                    }
                )
            return tools

    def dispatch(
        self,
        request: HttpRequest,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """
        Validate, authorise, and invoke a registered tool.

        The method performs three steps in order:

        1. Look up the tool — raises :exc:`ToolNotFoundError` (a
           ``LookupError``) if absent.
        2. Validate *arguments* against the tool's JSON Schema — raises
           :exc:`ToolInputError` on failure.
        3. Evaluate each ``permission_class`` in declaration order — raises
           ``PermissionError`` on first denial.

        Args:
            request: The current Django HTTP request used for permission checks.
            name: Tool name to dispatch.
            arguments: Caller-supplied arguments validated against
                ``input_schema``.

        Returns:
            Whatever the tool callable returns.

        Raises:
            ToolNotFoundError: No tool with *name* is registered.
            ToolInputError: *arguments* fails JSON Schema validation.
            PermissionError: A permission class denies access.

        """
        with self._lock:
            entry = self._tools.get(name)

        if entry is None:
            raise ToolNotFoundError(f"No tool registered with name {name!r}")

        # IT-1: Normalize camelCase argument keys to snake_case so that MCP
        # clients (e.g. Claude) can send either convention and always reach the
        # underlying Django serializer fields.  Opt out by setting
        # FRIESE_MCP_NORMALIZE_INPUT_CASE = False in Django settings.
        if getattr(settings, "FRIESE_MCP_NORMALIZE_INPUT_CASE", True):
            arguments = _normalize_argument_keys(arguments)

        # Tier enforcement at dispatch time.  ``permission_tier`` was previously
        # only used to filter ``tools/list``; a caller who knew the tool name
        # could still invoke a write/admin tool directly.  Now the same
        # tier-rank comparison is applied at execution time so that the
        # ``tools/list`` filter cannot be bypassed by name guessing.
        #
        # Runs before argument validation so that callers who lack permission
        # receive a clear tier error rather than an argument schema error that
        # leaks the tool's input contract.
        #
        # Dispatcher tools are intentionally registered with tier="read" so
        # they remain visible as navigation entry-points; per-action tier
        # enforcement happens inside the dispatcher invoke callable.  For
        # those entries the check here is a no-op (read ≥ read), and the
        # action-level check inside ``_make_dispatcher_invoke`` is what
        # rejects unauthorised sub-actions.
        if not entry.is_dispatcher:
            caller_tier = _resolve_request_tier(request)
            caller_rank = _TIER_RANK.get(caller_tier, 0)
            tool_rank = _TIER_RANK.get(entry.permission_tier, 0)
            if caller_rank < tool_rank:
                raise PermissionError(
                    f"Tool {entry.name!r} requires {entry.permission_tier!r} permission; "
                    f"caller has {caller_tier!r} permission."
                )

        for perm_class in entry.permission_classes:
            perm = perm_class()
            if not perm.has_permission(request, None):  # type: ignore[arg-type]
                raise PermissionError(f"Permission denied by {perm_class.__name__}")

        # Dispatcher tools handle action="help" internally (same path as a missing
        # action). Skip schema validation so "help" reaches the invoke callable
        # without triggering an enum mismatch — "help" is intentionally absent from
        # the action enum in the inputSchema.
        is_dispatcher_help = entry.is_dispatcher and arguments.get("action") == "help"

        # Bulk list-body calls ({objects: [...]} etc.) bypass required-field
        # validation — the host serializer validates each item individually.
        # Without this, a single-create schema (with required fields like
        # "location") rejects a valid bulk payload before it reaches invocation.
        _is_list_body = (
            len(arguments) == 1
            and next(iter(arguments)) in _BULK_LIST_BODY_KEYS
            and isinstance(next(iter(arguments.values())), list)
        )

        if not is_dispatcher_help and not _is_list_body:
            try:
                jsonschema.validate(instance=arguments, schema=entry.input_schema)
            except jsonschema.exceptions.ValidationError as exc:
                raise ToolInputError(exc.message) from exc

        if asyncio.iscoroutinefunction(entry.fn):
            return async_to_sync(entry.fn)(arguments, request)
        return entry.fn(arguments, request)


#: Module-level singleton imported by ``views.py`` and ``@mcp_tool``.
#: Import this directly rather than instantiating :class:`ToolRegistry`.
tool_registry: ToolRegistry = ToolRegistry()


def register(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    handler: Callable[..., Any],
    permission_classes: list[type[BasePermission]] | None = None,
) -> None:
    """
    Register a callable as a named MCP tool with the global registry.

    This is the imperative counterpart to ``@mcp_tool``, intended for host
    apps that register tools from ``AppConfig.ready()`` rather than at import
    time.  The handler signature must be ``(arguments: dict, request: HttpRequest)``.

    Args:
        name: Unique tool name (e.g. ``"orders.cancel"``).
        description: Human-readable description shown in ``tools/list``.
        input_schema: JSON Schema (draft-07) describing expected arguments.
        handler: Callable invoked as ``handler(arguments, request)``.
        permission_classes: DRF ``BasePermission`` subclasses guarding this
            tool.  Pass ``None`` or ``[]`` for unrestricted access.

    """
    tool_registry.register(
        name=name,
        fn=handler,
        description=description,
        input_schema=input_schema,
        permission_classes=permission_classes,
    )
