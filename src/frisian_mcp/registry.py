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

from frisian_mcp.negotiation import _NEGOTIATION_PROPERTIES

logger = logging.getLogger(__name__)

_TIER_RANK: dict[str, int] = {"read": 0, "read_write": 1, "admin": 2}
_VALID_PERMISSION_TIERS: frozenset[str] = frozenset(_TIER_RANK)

#: The caller tier meaning "no access at all" (H7).
#:
#: Deliberately NOT a member of ``_TIER_RANK``, and therefore not of
#: ``_VALID_PERMISSION_TIERS`` or ``route_config.PERMISSION_TIERS``.  Those
#: vocabularies govern what a *tool* may be registered at and what a *route
#: ceiling* may be set to, and neither may be ``"none"``: a tool nobody can
#: ever reach is a configuration error, not a tier.  ``"none"`` describes only
#: what a **caller** was granted.
DENY_TIER = "none"

#: Caller-tier ranks.  Identical to :data:`_TIER_RANK` plus :data:`DENY_TIER`
#: below every real tier, so ``caller_rank < entry_rank`` denies *every* tool
#: including ``read`` ones without any comparison site needing a special case.
_CALLER_TIER_RANK: dict[str, int] = {DENY_TIER: -1, **_TIER_RANK}


def _caller_rank(tier: str | None) -> int:
    """
    Return the access rank of a **caller** tier.

    Use this wherever the value being ranked is what the *caller* was granted;
    keep :data:`_TIER_RANK` for what a *tool or route* requires.  The two
    vocabularies differ by exactly :data:`DENY_TIER`, and collapsing them would
    make ``"none"`` a registerable tool tier.

    Unrecognised values rank as ``DENY_TIER``, not as ``read``.  That inversion
    is the H7 fix: ``_TIER_RANK.get(tier, 0)`` previously gave an unknown
    string the same rank as ``read``, so a typo'd or explicitly-``None``
    ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` silently granted the full read
    surface.  The setting was present, spelled plausibly, and did nothing.
    """
    if tier is None:
        return _CALLER_TIER_RANK[DENY_TIER]
    return _CALLER_TIER_RANK.get(str(tier), _CALLER_TIER_RANK[DENY_TIER])


#: The four states ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` can be in (H7).
#: ``ABSENT`` and ``EXPLICIT_NONE`` are deliberately distinct: absence is the
#: documented ``read`` default and must keep working, while an explicit ``None``
#: is a lockdown.  Collapsing them is the bug this vocabulary exists to prevent.
UNAUTH_TIER_ABSENT = "absent"
UNAUTH_TIER_EXPLICIT_NONE = "explicit_none"
UNAUTH_TIER_VALID = "valid"
UNAUTH_TIER_INVALID = "invalid"

#: Sentinel distinguishing "attribute missing" from "attribute set to None".
#: ``getattr(settings, NAME, None)`` cannot tell those apart, and every H7
#: consumer needs to.
_UNSET = object()


def classify_unauthenticated_tier() -> tuple[str, str]:
    """
    Return ``(case, effective_tier)`` for ``FRISIAN_MCP_UNAUTHENTICATED_TIER``.

    **The single source of truth for this setting (H13).**  Three consumers ask
    about it — the runtime resolver, the ``E007`` startup check, and
    ``mcp_doctor`` — and each previously derived the answer itself.  The doctor's
    copy drifted: it reported *"defaulting to 'read' at runtime"* for an
    unrecognised value after H7 had made that deny, and it read the setting with
    ``getattr(..., None)``, so a deliberate lockdown was reported as *"not
    set"* with a green tick while the server denied every anonymous caller.

    That is the standing failure of this project in miniature — a gate fixed in
    the consumer the task named while the other consumers keep the old
    semantics.  The classification is therefore computed **once**, here, and the
    *case* is returned alongside the tier so a caller that needs to explain
    itself does not have to re-derive why.

    Cases:

    * ``UNAUTH_TIER_ABSENT`` → ``"read"``.  The documented default; every host
      that never set it keeps working.
    * ``UNAUTH_TIER_EXPLICIT_NONE`` → :data:`DENY_TIER`.  ``None`` or the
      canonical string ``"none"`` — a deliberate lockdown.
    * ``UNAUTH_TIER_VALID`` → the tier itself.
    * ``UNAUTH_TIER_INVALID`` → :data:`DENY_TIER`, and ``E007`` fires.  Denying
      is correct; denying *silently* would strand an operator who typed
      ``readwrite`` for ``read_write``, which is why the case is distinguished
      rather than folded into the one above.
    """
    raw = getattr(settings, "FRISIAN_MCP_UNAUTHENTICATED_TIER", _UNSET)
    if raw is _UNSET:
        return UNAUTH_TIER_ABSENT, "read"
    if raw is None:
        return UNAUTH_TIER_EXPLICIT_NONE, DENY_TIER
    value = str(raw).strip().lower()
    if value == DENY_TIER:
        return UNAUTH_TIER_EXPLICIT_NONE, DENY_TIER
    if value in _VALID_PERMISSION_TIERS:
        return UNAUTH_TIER_VALID, value
    return UNAUTH_TIER_INVALID, DENY_TIER


def _resolve_unauthenticated_tier() -> str:
    """
    Return the caller tier for an unauthenticated request (H7).

    Thin wrapper over :func:`classify_unauthenticated_tier` so the runtime and
    every diagnostic answer from one derivation.  Fails closed on purpose: a
    host relying on the previous fail-open behaviour loses anonymous access
    when this lands, which is the intended outcome and carries a release note.
    """
    return classify_unauthenticated_tier()[1]


#: Single-key argument dicts whose value is a list are treated as bulk-create
#: (or bulk-update/destroy) bodies.  When detected in :meth:`ToolRegistry.dispatch`
#: the required-field schema validation is skipped — the host serializer validates
#: each item individually.  Mirrors ``_LIST_BODY_KEYS`` in ``backends.invocation``.
_BULK_LIST_BODY_KEYS: frozenset[str] = frozenset({"objects", "data", "items", "_items", "body"})

#: Recognised role-keys for ``FRISIAN_MCP_TOKEN_TIER_MAP`` lookup.  Probed in
#: this order against ``request.user`` attributes — the first match wins.
_TOKEN_TIER_MAP_ROLE_PROBES: tuple[tuple[str, str], ...] = (
    ("superuser", "is_superuser"),
    ("staff", "is_staff"),
)


def _validate_permission_tier(tier: str, *, field_name: str = "permission_tier") -> str:
    """Return *tier* when valid, otherwise raise a configuration error."""
    if tier not in _VALID_PERMISSION_TIERS:
        valid = ", ".join(sorted(_VALID_PERMISSION_TIERS))
        raise ValueError(f"Invalid {field_name} {tier!r}; expected one of: {valid}")
    return tier


def _resolve_tier_hook() -> Callable[[Any], str | None] | None:
    """
    Resolve ``settings.FRISIAN_MCP_RESOLVE_TIER`` to a callable.

    Accepts either an already-callable object or a dotted import path.  Returns
    ``None`` when the setting is absent or the path cannot be imported (the
    failure is logged at ERROR level so misconfigured deployments are visible
    without raising at request time).
    """
    raw = getattr(settings, "FRISIAN_MCP_RESOLVE_TIER", None)
    if raw is None:
        return None
    if callable(raw):
        return raw  # type: ignore[no-any-return]
    if isinstance(raw, str):
        try:
            return import_string(raw)  # type: ignore[no-any-return]
        except (ImportError, AttributeError):
            logger.exception("FRISIAN_MCP_RESOLVE_TIER %r could not be imported; ignoring", raw)
            return None
    logger.error(
        "FRISIAN_MCP_RESOLVE_TIER must be a callable or dotted-path string, got %r", type(raw)
    )
    return None


def _resolve_tier_from_role_map(request: Any) -> str | None:
    """
    Map a request's user role to a tier via ``FRISIAN_MCP_TOKEN_TIER_MAP``.

    The static map keys ``superuser``, ``staff``, and ``default`` are matched
    against ``request.user`` attributes (``is_superuser``, ``is_staff``).  The
    ``default`` entry applies to any authenticated user that did not match a
    higher-privilege role.  Unauthenticated callers do NOT receive ``default``
    — they continue to ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` so the existing
    anonymous-rejection contract is preserved.

    Returns ``None`` when the setting is absent or no entry matches.
    """
    role_map: dict[str, str] | None = getattr(settings, "FRISIAN_MCP_TOKEN_TIER_MAP", None)
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
    # Caller-side rank on the left: a denied caller ranks below every ceiling,
    # so a route cap can only ever lower a tier, never raise a denial to read.
    if cap is not None and _caller_rank(tier) > _TIER_RANK.get(cap, 0):
        return cap
    return tier


def _resolve_request_tier(request: Any) -> str:
    """
    Return the effective MCP permission tier for *request*.

    Resolution order — first non-``None`` result wins:

    1. ``settings.FRISIAN_MCP_RESOLVE_TIER`` (callable or dotted path).  Called
       with *request*.  Returning ``None`` falls through.  Exceptions are
       logged and treated as a fall-through so a broken hook cannot break the
       gateway.
    2. ``request.auth.permission`` (the historical convention; populated by
       :class:`~frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication`
       and OAuth tokens).
    3. ``settings.FRISIAN_MCP_TOKEN_TIER_MAP`` static role map keyed by
       ``superuser`` / ``staff`` / ``default`` — see
       :func:`_resolve_tier_from_role_map`.
    4. ``settings.FRISIAN_MCP_UNAUTHENTICATED_TIER`` (default ``"read"``) when
       ``request.auth is None``; otherwise ``"read"`` (most conservative — an
       authenticated request with an unknown auth backend never silently
       receives a higher tier).

    After resolution, the tier is clamped to ``request._mcp_max_tier`` when
    that attribute is set (stamped by :meth:`~frisian_mcp.views.McpView.post`
    from :meth:`~frisian_mcp.views.McpView._effective_max_tier`).  This applies
    the ``FRISIAN_MCP_MAX_TIER`` endpoint-level cap regardless of which
    resolution path was taken — including hook, token permission, and role map.

    Defined at module level so :class:`ToolRegistry` can enforce tier at
    dispatch time without importing :mod:`frisian_mcp.views` (avoiding a
    circular import).

    ``request._mcp_effective_tier`` short-circuits everything: it is the
    ``min(token_tier, route_ceiling, FRISIAN_MCP_MAX_TIER)`` result computed
    once in :meth:`~frisian_mcp.views.McpView.post` (ADR-010 §8).  Every later
    read in the same request — discovery, dispatch-time enforcement, error
    messages — returns that one value; nothing recomputes it.
    """
    stamped: str | None = getattr(request, "_mcp_effective_tier", None)
    if stamped is not None:
        # ``_mcp_effective_tier`` already incorporates the endpoint cap by
        # construction (it is ``min(token_tier, route_ceiling, MAX_TIER)``), so
        # re-applying the cap is normally a no-op.  Do it anyway as a
        # defense-in-depth invariant: if the stamp is ever stale or a bug stamps
        # it above ``_mcp_max_tier``, the endpoint cap still holds and dispatch
        # cannot be tricked into serving write/admin tools past the ceiling.
        return _apply_max_tier_cap(str(stamped), request)

    hook = _resolve_tier_hook()
    if hook is not None:
        try:
            tier = hook(request)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("FRISIAN_MCP_RESOLVE_TIER hook raised; falling through")
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
        tier = _resolve_unauthenticated_tier()
    else:
        tier = "read"
    return _apply_max_tier_cap(tier, request)


def _without_negotiation_constraints(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Return *schema* with the response-negotiation properties removed (T7-F5).

    The negotiation fields are DISCLOSED in a dispatcher's published schema
    (ADR-005 "Decision": redemption re-invokes *the same tool* with a
    ``continuation_token`` and a ``mode``) but must not be ENFORCED when
    validating the caller's
    top-level arguments.  They carry an ``enum`` (``mode``) and types
    (``page``, ``page_size``, ``filter_keys``), so leaving them in re-imposes
    the very reservation the dispatcher deliberately declines: ``mode`` is a
    genuine model field on at least one real host application and
    ``page``/``page_size`` are DRF pagination parameters, all of which reach
    the flat argument form legitimately.

    A continuation call never reaches dispatch — ``views`` short-circuits it
    first — so at this point these keys can only be action data.

    The names are derived from ``_NEGOTIATION_PROPERTIES`` rather than restated,
    because a hardcoded copy silently drifts out of sync with the schema.
    """
    props = schema.get("properties", {})
    stripped = {k: v for k, v in props.items() if k not in _NEGOTIATION_PROPERTIES}
    if len(stripped) == len(props):
        return schema
    return {**schema, "properties": stripped}


def _camel_to_snake(name: str) -> str:
    """Convert a camelCase or PascalCase identifier to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _normalize_argument_keys(arguments: Any) -> Any:
    """
    Recursively convert all dict keys from camelCase to snake_case.

    Controlled by the ``FRISIAN_MCP_NORMALIZE_INPUT_CASE`` Django setting
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
        """Store the raw tool error content."""
        self.content: Any = content
        super().__init__(str(content))


class _ToolEntry:  # pylint: disable=too-many-instance-attributes
    __slots__ = (
        "capability",
        "description",
        "dispatcher_meta",
        "fn",
        "group_tool_names",
        "hidden",
        "input_schema",
        "is_dispatcher",
        "is_heavy",
        "is_write",
        "name",
        "perm_app_label",
        "perm_drf_action",
        "perm_model",
        "permission_classes",
        "permission_tier",
        "universal_discovery",
        "view_class",
    )

    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]],
        is_dispatcher: bool = False,
        is_heavy: bool = False,
        is_write: bool = False,
        permission_tier: str = "read",
        dispatcher_meta: Any = None,
        hidden: bool = False,
        perm_app_label: str | None = None,
        perm_model: str | None = None,
        perm_drf_action: str | None = None,
        group_tool_names: frozenset[str] | None = None,
        capability: str | None = None,
        universal_discovery: bool = False,
    ) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.input_schema = input_schema
        self.permission_classes = permission_classes
        self.is_dispatcher = is_dispatcher
        self.is_heavy = is_heavy
        self.is_write = is_write
        self.permission_tier = permission_tier
        # ``dispatcher_meta`` is a ``backends.dispatcher.DispatcherMeta`` for
        # tools registered via ``@mcp_dispatcher``; ``None`` for plain
        # ``@mcp_tool`` / ``@mcp_heavy`` entries.  Typed as ``Any`` to avoid
        # a circular import between ``registry`` and ``backends.dispatcher``.
        self.dispatcher_meta = dispatcher_meta
        # ``hidden`` tools remain dispatchable by name but are excluded from
        # ``list_tools()`` output — used by FRISIAN_MCP_DISPATCH_GROUPS to bury
        # bundled flat tools behind their group dispatcher.
        self.hidden = hidden
        # Permission metadata extracted from DRF discovery.  Used by
        # FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY to filter tools/list.
        # ``None`` for decorator tools, which must instead declare ``capability``
        # or ``universal_discovery`` — see below.
        self.perm_app_label = perm_app_label
        self.perm_model = perm_model
        self.perm_drf_action = perm_drf_action
        # H3: the two explicit halves of the capability descriptor, for
        # registrations DRF discovery cannot derive one for.
        #
        # ``capability`` is the Django permission string the caller must hold
        # (``"app_label.verb_model"``) for a flat tool, or the
        # ``"app_label.model"`` base a dispatcher's per-action verbs resolve
        # against.  ``universal_discovery`` marks a tool as intentionally
        # visible to every caller.
        #
        # Under FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY an entry with neither a
        # derived nor a declared capability is INDETERMINATE and is HIDDEN.  It
        # used to be universally visible, which made a discovery control fail
        # open — the same defect class as the unauthenticated-tier fallback, and
        # fixed the same way.  Universal visibility is now reachable only by
        # stating it, never by omitting metadata.
        self.capability = capability
        self.universal_discovery = universal_discovery
        # For group dispatchers: frozenset of the flat tool names bundled inside.
        # Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to hide the group
        # dispatcher when the requesting user has no capabilities for any of its
        # child tools — so agents never see a group they cannot use at all.
        self.group_tool_names = group_tool_names
        # ``view_class`` is the DRF ViewSet that produced this tool, resolved
        # once at registration time by walking ``fn.__closure__`` for a
        # ToolDefinition-like cell.  ``None`` for decorator-only tools
        # (``@mcp_tool`` / ``@mcp_heavy`` / ``@mcp_dispatcher``) whose
        # invocation closure does not capture a ViewSet.  Consumed by
        # ``backends.invocation._extract_lean_envelope`` so it can read
        # ``view_class.serializer_class.Meta.mcp_light_key`` without
        # re-walking the closure on every write.
        self.view_class = None
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                candidate = cell.cell_contents
            except ValueError:  # empty cell during early closure construction
                continue
            resolved = getattr(candidate, "view_class", None)
            if resolved is not None:
                self.view_class = resolved
                break


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

    def register(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]] | None = None,
        is_dispatcher: bool = False,
        is_heavy: bool = False,
        is_write: bool = False,
        permission_tier: str = "read",
        dispatcher_meta: Any = None,
        hidden: bool = False,
        perm_app_label: str | None = None,
        perm_model: str | None = None,
        perm_drf_action: str | None = None,
        group_tool_names: frozenset[str] | None = None,
        capability: str | None = None,
        universal_discovery: bool = False,
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
            is_write: ``True`` when the tool mutates state (create/update/delete).
                Enables the lean-envelope write-path filtering in ``views.py``.
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
                Used by ``FRISIAN_MCP_DISPATCH_GROUPS`` to bury bundled flat
                tools behind their group dispatcher.
            perm_app_label: Django app label extracted from the ViewSet queryset
                model.  Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to
                build the required permission string.
            perm_model: Django model name (``model._meta.model_name``) extracted
                from the ViewSet queryset.  Paired with *perm_app_label*.
            perm_drf_action: DRF action name (e.g. ``"list"``, ``"retrieve"``)
                used to derive the Django permission verb via the
                ``_DRF_ACTION_TO_PERM_VERB`` mapping in ``views.py``.
            group_tool_names: For group dispatchers registered via
                ``FRISIAN_MCP_DISPATCH_GROUPS``, the frozenset of flat tool
                names bundled inside this dispatcher.  Used by
                ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to hide the group
                dispatcher from ``tools/list`` when none of its child tools are
                accessible to the requesting user.
            capability: H3 — the capability descriptor for registrations DRF
                discovery cannot derive one for.  For a flat tool, the full
                Django permission string the caller must hold
                (``"app_label.verb_model"``).  For a dispatcher, the
                ``"app_label.model"`` base its per-action verbs resolve
                against.  Ignored when *perm_app_label* / *perm_model* are
                present, since those already describe the same fact.
            universal_discovery: H3 — declare this tool intentionally visible
                to every caller under
                ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY``.  Required to be
                explicit: an entry with neither a derived nor a declared
                capability is treated as indeterminate and **hidden**, so
                universal visibility can no longer be the accidental result of
                missing metadata.

        """
        permission_tier = _validate_permission_tier(permission_tier)
        with self._lock:
            self._tools[name] = _ToolEntry(
                name=name,
                fn=fn,
                description=description,
                input_schema=input_schema,
                permission_classes=list(permission_classes or []),
                is_dispatcher=is_dispatcher,
                is_heavy=is_heavy,
                is_write=is_write,
                permission_tier=permission_tier,
                dispatcher_meta=dispatcher_meta,
                hidden=hidden,
                perm_app_label=perm_app_label,
                perm_model=perm_model,
                perm_drf_action=perm_drf_action,
                group_tool_names=group_tool_names,
                capability=capability,
                universal_discovery=universal_discovery,
            )

    def get_entry(self, name: str) -> _ToolEntry | None:
        """Return the raw ``_ToolEntry`` for *name*, or ``None`` if absent."""
        with self._lock:
            return self._tools.get(name)

    def list_dispatcher_names(self) -> frozenset[str]:
        """Return the names of all tools registered via ``@mcp_dispatcher``."""
        with self._lock:
            return frozenset(entry.name for entry in self._tools.values() if entry.is_dispatcher)

    def list_names(self) -> list[str]:
        """Return a snapshot of all currently-registered tool names."""
        with self._lock:
            return list(self._tools.keys())

    def entries_snapshot(self) -> dict[str, _ToolEntry]:
        """
        Return a name→entry snapshot of the registry taken under the lock.

        The mapping is a fresh dict, but the ``_ToolEntry`` values are the live
        registry objects — :class:`~frisian_mcp.route_views.RouteView` shares
        surviving flat entries by reference rather than copying them.  Consumers
        must treat the entries as read-only.
        """
        with self._lock:
            return dict(self._tools)

    def set_hidden(self, name: str, hidden: bool = True) -> bool:
        """
        Toggle the *hidden* flag on a registered tool.

        Hidden tools remain dispatchable by name but are excluded from
        :meth:`list_tools` so they do not appear in MCP ``tools/list`` output.
        Used by ``FRISIAN_MCP_DISPATCH_GROUPS`` post-processing to bury
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

    def list_tools(
        self,
        max_tier: str | None = None,
        entry_filter: Callable[[Any], bool] | None = None,
        action_filter_factory: Callable[[Any], Callable[[str, Any], bool] | None] | None = None,
    ) -> list[dict[str, Any]]:
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
            entry_filter: Optional callable applied to each ``_ToolEntry`` before
                it is included in the result.  Return ``False`` to exclude the
                entry.  Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to
                apply per-user capability filtering without holding the registry
                lock across external calls.
            action_filter_factory: Optional callable that receives a dispatcher
                ``_ToolEntry`` and returns either ``None`` (no extra filtering)
                or a ``(action_name, ActionEntry) -> bool`` predicate applied on
                top of tier filtering when building the dispatcher's action enum.
                Used by ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to hide
                write/delete actions the requesting user lacks permission for.

        """
        # ``max_tier is None`` means "no cap" (rank 2, show all).  A non-None but
        # UNRECOGNISED tier string fails CLOSED — via ``_caller_rank`` it now
        # ranks BELOW read (H7), so a garbled cap hides everything rather than
        # exposing the read surface.  Kept identical to
        # frisian_mcp.route_views._list_entries — the two must not diverge.
        max_rank = 2 if max_tier is None else _caller_rank(max_tier)

        # Lazy-import to avoid a circular dependency with backends.dispatcher,
        # which itself imports from this module.
        # pylint: disable=import-outside-toplevel
        from frisian_mcp.backends.dispatcher import _build_dispatcher_input_schema

        with self._lock:
            tools: list[dict[str, Any]] = []
            for entry in self._tools.values():
                if entry.hidden:
                    continue
                if _TIER_RANK.get(entry.permission_tier, 0) > max_rank:
                    continue
                # H3: a group dispatcher is judged by its children, not by its
                # own (necessarily absent) capability metadata — see below.
                # Running the generic filter on it first would hide every group
                # unconditionally now that the filter fails closed.
                if (
                    entry_filter is not None
                    and not entry.group_tool_names
                    and not entry_filter(entry)
                ):
                    continue

                # Group dispatcher: hide when the user has no capabilities for
                # any of its child tools.  Without this check every group tool
                # (dcim, bgp, ipam, …) would always appear in tools/list even
                # when the user's ObjectPermissions cover only one group (e.g.
                # dns), leaking the existence of every other group dispatcher.
                #
                # H3: every child is now considered, not just perm-aware ones.
                #
                # This branch used to pre-filter to children with perm_app_label
                # AND perm_model, because perm-less children "always pass
                # _make_perm_entry_filter regardless of the user's
                # capabilities" — counting them would have made the group
                # visible to everyone.  That premise is gone: the filter fails
                # closed, so a perm-less child passes only when it explicitly
                # declares a capability or universal discovery, and then it is
                # a legitimate reason to show the group.
                #
                # Keeping the pre-filter would leave the gap this task names: a
                # group assembled ENTIRELY from perm-less tools has no
                # perm-aware children, so the guard never fired and the group
                # stayed universally visible no matter what its children
                # required.  Behaviour for perm-aware children is unchanged —
                # one that fails the filter is excluded either way.
                #
                # This IS the group's capability determination, which is why it
                # replaces the generic entry filter above rather than stacking
                # on top of it: a group dispatcher never carries perm metadata
                # of its own, so under fail-closed the generic filter would
                # reject every group before its children were ever consulted.
                if entry.group_tool_names and entry_filter is not None:
                    children = [self._tools[t] for t in entry.group_tool_names if t in self._tools]
                    if not any(entry_filter(c) for c in children):
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
                            "tier": entry.permission_tier,
                        }
                    )
                    continue

                # Dispatcher: rebuild the inputSchema with the action enum
                # filtered to the caller's tier.  Hide the dispatcher entirely
                # when no actions remain visible (avoids exposing an empty
                # navigation tool that can only return help with zero actions).
                action_filter = (
                    action_filter_factory(entry) if action_filter_factory is not None else None
                )
                filtered_schema = _build_dispatcher_input_schema(
                    entry.dispatcher_meta, max_tier=max_tier, action_filter=action_filter
                )
                visible_actions = filtered_schema["properties"]["action"]["enum"]
                # H3: also drop when *capability* filtering emptied the enum,
                # not only tier filtering.  The old condition required
                # ``max_tier is not None``, so a perm-aware caller with no
                # capabilities for any action got the dispatcher published with
                # an empty enum — the empty navigation shell this check already
                # refuses to advertise, reached by the other filter.  Still
                # gated on filtering having been applied, so an unfiltered
                # listing keeps its legacy shape.
                if not visible_actions and (max_tier is not None or action_filter is not None):
                    continue
                tools.append(
                    {
                        "name": entry.name,
                        "description": entry.description,
                        "inputSchema": filtered_schema,
                        "tier": entry.permission_tier,
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
        # FRISIAN_MCP_NORMALIZE_INPUT_CASE = False in Django settings.
        if getattr(settings, "FRISIAN_MCP_NORMALIZE_INPUT_CASE", True):
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
            if _caller_rank(caller_tier) < _TIER_RANK.get(entry.permission_tier, 0):
                if getattr(request, "_mcp_max_tier", None) is not None:
                    # On a max-tier-capped endpoint the tool must appear
                    # nonexistent — returning a tier error leaks that the tool
                    # exists and reveals the elevation path to callers.
                    raise ToolNotFoundError(f"No tool registered with name {name!r}")
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

        # Reject nested-dict wrapper on write tools: agents often send
        # {data: {field: value}} following REST conventions, which silently
        # produces records with all fields empty because the flat field keys
        # the serializer expects are never present in the top-level arguments.
        # Only fires when the wrapper key is not a declared property in the
        # tool's inputSchema (a model with a real "data" JSON field is fine).
        if entry.is_write and not _is_list_body and len(arguments) == 1:
            _wrap_key = next(iter(arguments))
            if (
                _wrap_key in _BULK_LIST_BODY_KEYS
                and isinstance(arguments[_wrap_key], dict)
                and _wrap_key not in (entry.input_schema.get("properties") or {})
            ):
                _expected = sorted((entry.input_schema.get("properties") or {}).keys())
                raise ToolInputError(
                    f"Payload must be flat: fields should be top-level arguments, "
                    f'not wrapped in "{_wrap_key}": {{...}}. '
                    f"Send the fields directly as: {_expected}."
                )

        _validation_schema = entry.input_schema
        if entry.is_dispatcher:
            _validation_schema = _without_negotiation_constraints(_validation_schema)
        if entry.is_dispatcher and getattr(request, "_mcp_max_tier", None) is not None:
            # V11-20 (F3): the registration-time action enum is the FULL action
            # set, and jsonschema's enum-violation message enumerates every
            # allowed value — handing a tier-capped caller the write/admin
            # action names that tools/list and help deliberately hide.  Drop
            # the enum from validation on capped routes; the dispatcher invoke
            # rejects unknown actions itself with a hint drawn from the
            # caller-visible set, so a never-existed action and an
            # above-ceiling action produce the same absence error.  Uncapped
            # legacy mounts keep the enum (and its self-correction value).
            _props = _validation_schema.get("properties", {})
            if "enum" in _props.get("action", {}):
                _validation_schema = {
                    **_validation_schema,
                    "properties": {
                        **_props,
                        "action": {k: v for k, v in _props["action"].items() if k != "enum"},
                    },
                }

        if not is_dispatcher_help and not _is_list_body:
            try:
                jsonschema.validate(instance=arguments, schema=_validation_schema)
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
