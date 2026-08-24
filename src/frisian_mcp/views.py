"""
MCP gateway endpoint — JSON-RPC 2.0 over HTTP POST.

Single entry-point for all MCP traffic.  Clients POST a JSON-RPC 2.0 message
and receive a JSON-RPC 2.0 response.  Server-Sent Events (SSE) are out of scope
for v1.

Supported methods
-----------------
* ``initialize``       — protocol handshake
* ``initialized``      — client confirmation notification
* ``tools/list``       — enumerate registered tools
* ``tools/call``       — invoke a registered tool
* ``resources/list``   — stub (returns empty list in v1)
* ``resources/read``   — stub (returns METHOD_NOT_FOUND in v1)
* ``ping``             — liveness check
* ``help``             — server metadata and usage hints for AI agents

Authentication & permissions
-----------------------------
:class:`McpView` (formerly ``McpEndpointView``) extends DRF's
:class:`~rest_framework.views.APIView`, which
means host projects can gate the MCP surface using standard DRF mechanisms:

* ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` — list of dotted-path strings *or*
  class objects; falls back to DRF's ``DEFAULT_AUTHENTICATION_CLASSES``.
* ``FRISIAN_MCP_PERMISSION_CLASSES``    — list of dotted-path strings *or*
  class objects; defaults to ``[]`` (no gateway-level permission check) for
  backwards compatibility.  Tool-level ``permission_classes`` are enforced
  separately by :data:`~frisian_mcp.registry.tool_registry`.
"""

# pylint: disable=too-many-lines

import asyncio
import base64
import difflib
import hashlib
import importlib.metadata
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, Container, Generator
from typing import Any

from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS, cache as django_cache, caches
from django.core.cache.backends.base import InvalidCacheBackendError
from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request as DRFRequest
from rest_framework.views import APIView

from frisian_mcp.backends.base import ToolResult
from frisian_mcp.contrib.permissions.base import build_action_filter, entry_is_visible
from frisian_mcp.middleware import build_middleware_chain, get_middleware_instances
from frisian_mcp.negotiation import (
    DEFAULT_NEGOTIATION_MODE,
    NEGOTIATION_MODES,
    schema_discloses_continuation,
)
from frisian_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    JsonDict,
    JsonRpcId,
)
from frisian_mcp.registry import (
    _TIER_RANK,
    ToolInputError,
    ToolInvocationError,
    ToolNotFoundError,
    _caller_rank,
    normalize_tier_setting,
    tool_registry,
)
from frisian_mcp.resources import ResourceNotFoundError, resource_registry
from frisian_mcp.route_views import RouteView, route_views
from frisian_mcp.usage import maybe_attach_usage

logger = logging.getLogger(__name__)

#: DOC-7 pre-wire: the audit-context seam.  One structured record per resolved
#: ``tools/call``, emitted on a dedicated child logger so a durable sink can
#: attach a handler to ``frisian_mcp.audit`` and consume the records without
#: transformation — and without inheriting this module's operational noise.
audit_logger = logging.getLogger("frisian_mcp.audit")

_TOOLS_LIST_CACHE_KEY = "frisian_mcp:tools_list"
_HEAVY_CACHE_PREFIX = "frisian_mcp:heavy:"

#: Default TTL for a continuation entry, in seconds.  Overridable via
#: ``FRISIAN_MCP_HEAVY_CACHE_TTL`` (H6): a blast-radius control, **not** a
#: substitute for isolation — a caller can mint many short-lived entries.
_DEFAULT_HEAVY_CACHE_TTL: int = 300


def _heavy_cache_ttl() -> int:
    """Return the continuation-entry TTL in seconds (``FRISIAN_MCP_HEAVY_CACHE_TTL``)."""
    return int(getattr(settings, "FRISIAN_MCP_HEAVY_CACHE_TTL", _DEFAULT_HEAVY_CACHE_TTL))


#: Guard so a misconfigured alias logs once per process rather than once per
#: request.  The condition is static — it cannot resolve itself between calls —
#: so repeating it per request would bury the one line that matters.
_missing_heavy_alias_logged: set[str] = set()


def _log_missing_heavy_cache_alias(alias: str) -> None:
    """Report an unusable continuation-cache alias, once per process."""
    if alias in _missing_heavy_alias_logged:
        return
    _missing_heavy_alias_logged.add(alias)
    logger.error(
        "heavy_cache_alias_unavailable",
        extra={
            "alias": alias,
            "detail": (
                f"FRISIAN_MCP_HEAVY_CACHE_ALIAS={alias!r} names no configured cache. "
                "Heavy-response negotiation is DISABLED: over-threshold responses are "
                "returned whole rather than minting continuation state into the "
                "'default' cache, which holds OAuth codes and the brute-force counter."
            ),
        },
    )


def _heavy_cache() -> Any | None:
    """
    Return the cache holding continuation entries, or ``None`` if unavailable.

    H6: continuation state is **attacker-amplifiable** — an unauthenticated
    caller can mint entries — while OAuth authorization codes, the consumed-code
    gate and the token-endpoint rate counter are authentication and
    brute-force-control state.  Sharing one eviction domain lets the first
    displace the second.

    ``FRISIAN_MCP_HEAVY_CACHE_ALIAS`` selects the alias.  It defaults to
    ``default`` because the package cannot conjure a second cache for a host
    that has not configured one; the startup check reports that unseparated
    state rather than letting it pass silently.

    A **separate alias is not by itself a boundary.**  Two aliases addressing
    the same Redis instance — including two logical DBs on one instance — share
    that instance's memory and therefore its failure.  The boundary required is
    an *independent eviction domain*: a separate instance, or a per-instance
    memory budget.  The startup check catches the collisions it can see; the
    rest is an operator obligation and is documented as one.

    An alias naming a cache that does not exist returns ``None``: negotiation is
    **unavailable**, not relocated.  Callers skip the mint and return the whole
    response instead, which is the same answer H2 gives a tool whose schema
    cannot safely carry the continuation branch — if it cannot be done safely,
    the response is not eligible for negotiation.

    This used to fall back to ``default`` on the grounds that a misconfiguration
    should not deny service and the startup check would surface it.  Both halves
    were wrong.  The check did not catch a missing alias at all, and — measured,
    not assumed — ``get_wsgi_application()`` calls only ``django.setup()``, so
    **system checks do not run on a gunicorn/uWSGI boot**.  A check cannot be the
    safety net for a production process that never executes it.  What the
    fallback preserved was writing attacker-amplifiable state into the cache
    holding OAuth codes and the brute-force counter — service continuity bought
    with the exact exposure the setting exists to remove.
    """
    alias = getattr(settings, "FRISIAN_MCP_HEAVY_CACHE_ALIAS", DEFAULT_CACHE_ALIAS)
    if alias == DEFAULT_CACHE_ALIAS:
        # Resolve through the module-level binding rather than ``caches[...]``.
        # They are the same object in production, but this is the seam the test
        # suite injects on, and routing the default case around it would make
        # every heavy-path test assert against a cache the code no longer used.
        return django_cache
    try:
        return caches[alias]
    except (InvalidCacheBackendError, ImproperlyConfigured, ImportError):
        # Django wraps an *unimportable* BACKEND path in InvalidCacheBackendError,
        # but backend construction happens outside that wrapper: a backend whose
        # ``__init__`` raises ImproperlyConfigured (bad LOCATION, missing option)
        # or ImportError (missing client library) propagates out of ``caches[...]``
        # untouched.  Every one of these means the same thing operationally —
        # this alias cannot hold continuation state — so they get the same
        # answer.  Letting them escape turned a misconfigured cache into an
        # HTTP 500 on any over-threshold read, which is a worse failure than
        # declining to negotiate.
        _log_missing_heavy_cache_alias(alias)
        return None


#: Default byte threshold for the auto-negotiate backstop
#: (``FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD``).  A response from a tool **whose
#: published schema discloses the continuation call** is returned as a probe
#: envelope once its serialized JSON exceeds this many bytes, so the caller can
#: negotiate how much to retrieve instead of taking a context-blowing full
#: payload.
#:
#: Size alone is not sufficient: the gate is ``schema_discloses_continuation()``
#: on the outer entry, so the backstop covers ``@mcp_heavy`` and both
#: dispatchers.  An over-threshold response from a tool that does not disclose
#: is returned WHOLE and mints nothing (CR-2) — the threshold is a ceiling on
#: what may probe, never on what may be returned.
#:
#: This ships non-``None`` (the historical default was ``None`` = dormant) so
#: high-cardinality list actions on those shapes probe-first on every host
#: without the operator having to discover the knob.  ~25 KB is on the order of
#: ~6k cl100k_base tokens: above a normal small filtered read, well below a
#: large list page (a 114-row device list serialized to ~145 KB on the Nautobot
#: test box and probes cleanly at this value).  Operators raise it to probe less
#: often, or set
#: ``FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = None`` to disable the backstop.
_DEFAULT_AUTO_NEGOTIATE_THRESHOLD: int = 25_000

#: Sentinel for the one-time WSGI-SSE-worker-pinning warning emitted on the
#: first SSE keepalive request served from a sync worker.  Sync workers cannot
#: host SSE without starving the worker pool — see the warning message + the
#: Deployment Notes in the Installation & Configuration Reference.
_SSE_WSGI_WARNED: bool = False

_REFRESH_HINT = (
    " Call tools/list to refresh your available tools — the server manifest may have changed."
)

#: Substrings used to detect instructional scaffolding text that the
#: ``lite: true`` per-call flag should strip from dispatcher help responses.
#: Matched case-insensitively against the *value* of any string field on the
#: dispatcher help payload (e.g. a ``hints`` map keyed by tool name).  A field
#: whose value contains any of these substrings is removed from the lite
#: response so the agent receives the action list without re-teaching text.
_LITE_SCAFFOLDING_SUBSTRINGS: tuple[str, ...] = (
    "use action=",
    "use action ",
    "call tools/list",
    "tools/list to refresh",
)


def _strip_lite_scaffolding(payload: Any) -> Any:
    """
    Return a copy of *payload* with instructional scaffolding removed.

    Applied on every successful ``tools/call`` response when the caller passes
    ``lite: true``.  The lite contract is:

    * Drop the ``hints`` map (operator-supplied navigation hints).
    * Remove any top-level string field whose value contains
      instructional scaffolding (``"use action='help'"`` and similar) —
      detected by :data:`_LITE_SCAFFOLDING_SUBSTRINGS`.
    * On dispatcher help responses (``payload["help"] is True``): also
      reduce each entry in the ``actions`` list to its ``name`` string
      (drop ``description``, ``input_schema``, ``params``).
    * Leave every other field untouched — data is never stripped.

    Args:
        payload: The dispatch result returned by the tool's ``invoke``.

    Returns:
        A new dict (or the original *payload* unchanged when no stripping
        applies).  Never mutates *payload* in place.

    """
    if not isinstance(payload, dict):
        return payload

    is_help = payload.get("help") is True
    stripped: dict[str, Any] = {}
    for key, value in payload.items():
        if is_help and key == "actions" and isinstance(value, list):
            # Reduce action entries to plain name strings (help responses only).
            # Each entry may be a dict (single dispatcher) or a string (group
            # dispatcher: action names only) — handle both shapes uniformly.
            names: list[str] = []
            for entry in value:
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str):
                        names.append(name)
                elif isinstance(entry, str):
                    names.append(entry)
            stripped[key] = names
            continue
        if is_help and key == "resources" and isinstance(value, dict):
            # Full-group help returns {resource: [actions, ...], ...}.  Agent
            # already knows the action catalogue from orientation; lite reduces
            # this to a sorted list of resource names only.
            stripped[key] = sorted(value.keys())
            continue
        if key == "hints":
            # Operator-supplied navigation hints are scaffolding by definition.
            continue
        if isinstance(value, str) and any(
            marker in value.lower() for marker in _LITE_SCAFFOLDING_SUBSTRINGS
        ):
            continue
        stripped[key] = value
    return stripped


def _get_token_permission(request: Any) -> str:
    """
    Return the effective permission tier for this request.

    Delegates to :func:`frisian_mcp.registry._resolve_request_tier` so that the
    full resolution chain (``FRISIAN_MCP_RESOLVE_TIER`` callable hook,
    ``request.auth.permission``, ``FRISIAN_MCP_TOKEN_TIER_MAP`` role map, and
    fallback) is applied in one canonical place.  Retained as a thin shim for
    backwards compatibility with code (and tests) that imports
    ``views._get_token_permission`` directly.

    If ``request._mcp_max_tier`` is set (stamped by :meth:`McpView.post` from
    the view's :meth:`~McpView._effective_max_tier`), the resolved tier is
    clamped to that maximum.  This is the ``FRISIAN_MCP_MAX_TIER`` mechanism:
    even a superuser hitting an open endpoint receives at most the declared cap.
    """
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        _resolve_request_tier,
    )

    return _resolve_request_tier(request)


def invalidate_tools_list_cache() -> None:
    """
    Delete the cached tools/list manifest so the next request rebuilds it.

    Call this after registering tools at runtime when
    ``FRISIAN_MCP_TOOLS_LIST_CACHE_TTL`` is set, rather than waiting for the
    TTL to expire naturally.
    """
    # Delete per-tier keys + the legacy :all key (written by any custom code using
    # max_tier=None → cache_key={key}:all in older deployments).
    keys = [f"{_TOOLS_LIST_CACHE_KEY}:all"] + [
        f"{_TOOLS_LIST_CACHE_KEY}:{tier}" for tier in _TIER_RANK
    ]
    # Per-route keys (FRISIAN_MCP_ROUTES): each mounted route caches its own
    # manifest per tier, because two routes at the same tier expose different
    # deny-carved surfaces.
    for route_name in route_views.names():
        keys.append(f"{_TOOLS_LIST_CACHE_KEY}:{route_name}:all")
        keys.extend(f"{_TOOLS_LIST_CACHE_KEY}:{route_name}:{tier}" for tier in _TIER_RANK)
    django_cache.delete_many(keys)


# ---------------------------------------------------------------------------
# Heavy response-negotiation helpers
# ---------------------------------------------------------------------------


def _heavy_owner_key(request: Any, tool_name: str) -> str:
    """
    Return a stable identifier for the caller of a heavy tool invocation.

    SEC-3: heavy-response continuation tokens cache the result under a
    server-issued opaque token.  Without binding, anyone who learns the
    token (a leaked log, a compromised middlebox, a different agent on
    the same gateway) could replay it and read another caller's data.
    The owner key composes:

    * the originating tool name — refuses replay against a different tool
    * the auth backend type + primary key (or, for pk-less static API keys,
      the matched key's HMAC digest, T5) — refuses cross-credential replay
    * the effective permission tier — refuses replay after a downgrade
    * the user PK if any — refuses cross-user replay
    * the agent connection PK if the request is per-agent scoped (PKG-6)

    The MCP session id (``Mcp-Session-Id`` header) is intentionally NOT part of
    the key (TUR-16).  It is a client-supplied transport header, not a server
    secret, and real MCP clients (e.g. the Claude.ai connector) mint a fresh
    session id per tool-call POST — so binding on it broke legitimate
    probe→redeem resume on authenticated routes (the probe issued a
    continuation_token the caller could never redeem) while adding no real
    protection: cross-caller/cross-tool/post-downgrade replay is already
    refused by the tool + auth-credential + tier + user components above, and
    an attacker who holds the victim's credential can re-fetch the data
    outright.  Session drift must never gate a caller's own resume.

    The shape is intentionally a single string so the comparison is a
    simple equality check; the exact field set need not be stable across
    releases because the owner key never leaves the server.
    """
    auth_obj = getattr(request, "auth", None)
    if auth_obj is None:
        auth_id = "anon"
    else:
        pk = getattr(auth_obj, "pk", None)
        if pk is not None:
            auth_id = f"{type(auth_obj).__name__}:{pk}"
        else:
            # Static API keys (_ApiKeyAuth) have no PK. Use the matched
            # key's HMAC digest (key_id) as a stable per-key identity so
            # two distinct same-tier keys don't collide (T5) — the digest
            # is already a one-way hash of the raw secret, safe to reuse
            # here. Fall back to tier alone for any other pk-less auth
            # object that doesn't set key_id.
            key_id = getattr(auth_obj, "key_id", None)
            if key_id is not None:
                auth_id = f"{type(auth_obj).__name__}:key={key_id}"
            else:
                auth_id = (
                    f"{type(auth_obj).__name__}:tier={getattr(auth_obj, 'permission', 'unknown')}"
                )

    user = getattr(request, "user", None)
    user_pk = getattr(user, "pk", None) if user is not None else None
    user_part = f":user={user_pk}" if user_pk is not None else ""

    # Tier resolution flows through registry._resolve_request_tier (PKG-15)
    # so SEC-3 inherits the same hook/role-map chain — no duplicated logic.
    tier = _get_token_permission(request)

    conn = getattr(request, "_mcp_agent_connection", None)
    conn_pk = getattr(conn, "pk", None) if conn is not None else None
    conn_part = f":conn={conn_pk}" if conn_pk is not None else ""

    # NOTE (TUR-16): the Mcp-Session-Id header is deliberately excluded — it
    # drifts across a real client's per-call requests and adds no protection
    # beyond auth/tier/user/tool.  See the docstring for the full rationale.
    return f"tool={tool_name}:auth={auth_id}:tier={tier}{user_part}{conn_part}"


def _build_heavy_cache_entry(
    result: Any,
    request: Any,
    tool_name: str,
    resolved_target: str | None = None,
    resolved_action: str | None = None,
    *,
    single_object: bool = False,
) -> dict[str, Any]:
    """
    Wrap *result* with the SEC-3 owner-binding metadata for the cache.

    ADR-011 §5: entries also record ``resolved_target`` — the child tool the
    *server* resolved for this call, not the outer name the caller sent.  For a
    flat call the two are the same; for a grouped call the child is what
    `_dispatcher_target_entry` routed to.

    Recording only the outer name would leave the §4 re-authorization with
    nothing to evaluate: the dispatcher is mounted, so a membership check
    against *it* always passes, while the child — the thing whose route
    containment actually matters — would be unrepresented.  The re-check would
    look correct and be vacuous.

    ``resolved_action`` is the same argument one level down, for **class**
    dispatchers.  They route by ``action`` rather than by member tool, so
    ``_dispatcher_target_entry`` — which resolves group membership — returns
    ``None`` for them and ``resolved_target`` falls back to the dispatcher
    itself.  A class dispatcher registers as ``read`` precisely so it stays
    visible as a navigation entry-point, and its per-action authorization lives
    in the action lens, so re-authorizing the *outer* entry passes trivially:
    the vacuous re-check §5 exists to prevent, one shape over.

    Recorded at mint because that is the action the server actually dispatched.
    The redemption call's own ``action`` argument is caller-supplied and is
    deliberately **not** consulted.

    ``single_object`` records that *result* is one written object rather than a
    list-shaped response, so redemption does not go looking for a list payload
    inside it (see :func:`_serve_heavy_mode`).  Set by the two **write** mint
    sites and by neither read site.  It is carried rather than inferred because
    a created object and a paginated envelope are both dicts — exactly the
    ambiguity :func:`_envelope_payload_key` declines to guess at.  The mint site
    is the only place that knows which one this is.

    Stored only when true, so an entry's shape is unchanged for every existing
    caller.
    """
    entry: dict[str, Any] = {
        "result": result,
        "owner_key": _heavy_owner_key(request, tool_name),
        "tool_name": tool_name,
        "resolved_target": resolved_target or tool_name,
    }
    if resolved_action is not None:
        entry["resolved_action"] = resolved_action
    if single_object:
        entry["single_object"] = True
    return entry


def _dispatched_action(entry: Any, arguments: dict[str, Any]) -> str | None:
    """
    Return the action a **class** dispatcher just dispatched, else ``None``.

    Keyed on ``dispatcher_meta``, which is what distinguishes a class dispatcher
    from a group one: groups carry ``group_tool_names`` and resolve to a member
    tool, so they are covered by ``resolved_target`` and record no action here.
    """
    if entry is None or getattr(entry, "dispatcher_meta", None) is None:
        return None
    action = arguments.get("action")
    return str(action) if isinstance(action, str) and action else None


def _redemption_action_authorized(request: Any, tool_name: str, action_name: str) -> bool:
    """
    ADR-011 §4, class-dispatcher case: may this caller still invoke *action_name*?

    Re-runs the **same action lens** ``tools/list`` and ``action="help"`` apply,
    against the caller's *current* capabilities — plus the action's own tier,
    which ordinary dispatch enforces inside the invoke callable but redemption
    never reaches, because it serves from cache instead of re-dispatching.

    Resolves the dispatcher through the route view for the same reason
    :func:`_redemption_target_authorized` does: the route's entry is the
    authoritative one, and a rebuilt per-route dispatcher may expose fewer
    actions than the global registry's.

    Returns ``False`` on anything it cannot affirmatively authorize, including
    an action that has since been removed from the dispatcher.
    """
    rv: RouteView | None = getattr(request, "_mcp_route_view", None)
    outer = rv.entries.get(tool_name) if rv is not None else tool_registry.get_entry(tool_name)
    if outer is None:
        return False

    meta = getattr(outer, "dispatcher_meta", None)
    action_entry = getattr(meta, "actions", {}).get(action_name) if meta is not None else None
    if action_entry is None:
        return False  # action no longer declared on this dispatcher

    if _caller_rank(_get_token_permission(request)) < _TIER_RANK.get(
        getattr(action_entry, "permission_tier", "read"), 0
    ):
        return False

    caps: Container[str] | None = getattr(request, "_mcp_capabilities", None)
    if caps is None:
        # Permission-aware discovery off, or an unrestricted caller: the tier
        # check above is the whole gate, exactly as it is for discovery.
        return True

    action_filter = build_action_filter(outer, caps)
    if action_filter is None:
        return True  # explicit universal_discovery — publishes every action
    return bool(action_filter(action_name, action_entry))


def _redemption_target_authorized(request: Any, tool_name: str, target_name: str) -> bool:
    """
    ADR-011 §4: may the *current* route serve a continuation for *target_name*?

    Re-evaluates the cached, server-resolved target against the **current**
    :class:`~frisian_mcp.route_views.RouteView` of the route the redemption
    arrived on — not the route recorded at mint time, and deliberately not the
    tier ceiling alone.

    **A ceiling comparison would be insufficient.**  Two routes may declare the
    same ``FRISIAN_MCP_MAX_TIER`` and expose entirely different resources,
    because ADR-010's allow/deny grammar carves the surface independently of
    tier.  A control that compared ceilings would look correct, pass a
    plausible test, and permit exactly the cross-route service it exists to
    refuse.  So all four dimensions of the surface are evaluated:

    * **mounted membership + deny carve-outs** — the outer tool must be present
      in this route's ``entries``, and for a grouped call the child must be in
      that route's *pruned* ``group_tool_names``.  Pruning is per route, so a
      group whose members were partly denied here yields a narrower set than
      the one the token was minted against.
    * **effective tier ceiling** — the caller's tier as already clamped to
      ``request._mcp_max_tier``, compared against the *target's* required tier
      rather than the dispatcher's (dispatchers register as ``read`` to stay
      visible as navigation entry-points, so checking the outer entry's tier
      would pass everything).
    * **capability / permission visibility** — the same per-user entry filter
      ``tools/list`` applies under ``PERMISSION_AWARE_DISCOVERY``.

    Returns ``False`` on anything it cannot affirmatively authorize; the caller
    maps that to §6's existing refusal outcome.
    """
    rv: RouteView | None = getattr(request, "_mcp_route_view", None)
    # No per-route mount means the global registry *is* this route's surface.
    outer = rv.entries.get(tool_name) if rv is not None else tool_registry.get_entry(tool_name)
    if outer is None:
        return False  # outer tool unmounted or denied on this route

    target = outer
    if target_name != tool_name:
        members = getattr(outer, "group_tool_names", None)
        if members is None or target_name not in members:
            return False  # child not a member of this route's pruned group
        # Membership is the route-scoped fact; the tier/permission metadata
        # still lives on the member's own entry.  Resolve it against the
        # registry the view was materialised from — in production the global
        # singleton, but threading it keeps the check honest about its backing
        # store rather than reaching past the view to a global.
        #
        # Deliberate protected access: the view's own backing store is the
        # honest source here, and RouteView exposes no public accessor for it.
        # pylint: disable-next=protected-access
        source = rv._registry if rv is not None else tool_registry
        # Narrowed in its own name so `target` stays a non-Optional entry; the
        # earlier `target = outer` already fixed its type.
        resolved = source.get_entry(target_name)
        if resolved is None:
            return False
        target = resolved

    if _caller_rank(_get_token_permission(request)) < _TIER_RANK.get(target.permission_tier, 0):
        return False

    perm_filter = getattr(request, "_mcp_perm_entry_filter", None)
    if perm_filter is not None and not perm_filter(target):
        return False

    return True


#: ADR-011 §6: every non-ownership redemption refusal returns *this* string.
#: Expiry, a pre-SEC-3 legacy entry, an entry minted before §5, and a
#: route-containment failure are operationally distinct and are distinguished in
#: the audit reason — never on the wire.  Redemption already exposes two
#: client-visible outcomes (this one and the owner mismatch), which is a
#: token-validity oracle; tolerable only because tokens are 128-bit.  A third
#: outcome meaning "valid token, wrong route" would additionally disclose server
#: deploy state — that this host serves some other mount where the token would
#: work — to any token holder, anonymous callers on open mounts included.
_CONTINUATION_REFUSED_ERROR: str = (
    "Continuation token expired or not found."
    " Re-invoke without continuation_token"
    " to start a new negotiation."
)


def _continuation_refused(request_id: Any) -> JsonResponse:
    """Return the single client-visible refusal envelope shared by §6's cases."""
    return _jsonrpc_success(
        request_id,
        {
            "content": [
                {"type": "text", "text": json.dumps({"error": _CONTINUATION_REFUSED_ERROR})}
            ],
            "isError": True,
        },
    )


def _dispatcher_target_entry(entry: Any, arguments: dict[str, Any]) -> Any | None:
    """
    Resolve the entry *entry* routes to, **restricted to its own members**.

    A group dispatcher carries ``resource`` + ``action`` at the top level and
    routes to the flat tool ``f"{resource}{sep}{action}"``.  Flags declared on
    that underlying tool — ``is_write``, ``is_heavy`` — therefore have to be
    read from *its* entry: the dispatcher entry itself never carries them,
    because the decorators mark the flat tool.

    The membership check is a security boundary, not a tidy-up.  ``resource``
    is caller-supplied, and a class dispatcher never reads it (see
    ``backends/dispatcher.py``) — so a call shaped
    ``{"action": ..., "params": {...}, "resource": <anything>}`` dispatches
    normally through the class dispatcher and then, without this check, would
    resolve ``<anything>_<action>`` against the **global** registry and read
    its flags.  Nothing between that resolve and the heavy mint consults tier,
    Django permissions, or the per-route ``_mcp_max_tier`` ceiling, so an
    unrestricted lookup lets a caller force a probe envelope for a tool they
    never invoked: it mints a 300s entry in the shared default cache for a
    response of *any* size, bypassing FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD
    entirely, and the probe-versus-normal-result difference answers "does this
    name exist and is it @mcp_heavy?" for any name guessed.

    Gate on *presence* of ``group_tool_names``, not merely on membership:
    class dispatchers never set it at all, so a bare member check would
    silently no-op on the one path that is actually reachable.  Resolution is
    the **last** step, after membership, so a non-member name is never looked
    up rather than looked up and discarded.

    This is a no-op for every legitimate call.  Group dispatchers already
    resolve only their own members — ``make_group_invoke`` enforces that during
    dispatch, and the heavy branch runs only on a dispatch that already
    succeeded — and class dispatchers already resolve nothing for a
    well-behaved caller.  Only the malformed call changes behaviour.

    Returns ``None`` when *entry* bundles no members (any non-group
    dispatcher), when the arguments do not name a routable target, when the
    target is not a member of *entry*, or when no such tool is registered.
    """
    members: frozenset[str] | None = getattr(entry, "group_tool_names", None)
    if not members:
        return None
    resource = arguments.get("resource", "")
    action = arguments.get("action", "")
    sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
    target = f"{resource}{sep}{action}" if resource and action else ""
    if not target or target not in members:
        return None
    return tool_registry.get_entry(target)


def _build_probe_envelope(result: Any, token: str) -> dict[str, Any]:
    """Build the call-1 probe envelope for the two-call response-negotiation protocol."""
    serialized = json.dumps(result)
    if isinstance(result, dict):
        preview = json.dumps({k: str(v)[:80] for k, v in list(result.items())[:5]})
    elif isinstance(result, list):
        preview = json.dumps(result[:3])
    else:
        preview = serialized[:200]
    return {
        "preview": preview[:200],
        "total_size": len(serialized.encode()),
        "available_modes": list(NEGOTIATION_MODES),
        "continuation_token": token,
        # T6: an agent mid-negotiation is not re-reading tools/list, so the
        # envelope that advertises the modes must also say where the fields go
        # and what omitting `mode` costs.  Advertising reachable modes without
        # disclosing their placement is what made all four look unreachable.
        #
        # The placement wording is deliberately shape-neutral.  This is the
        # single builder for every consumer — flat `@mcp_heavy` (the tool's own
        # fields, no `action` and no `params`), class dispatcher (`action` +
        # `params`) and group dispatcher (`resource` + `action` + `params`) —
        # and neither call site passes shape.  Naming sibling keys here was
        # therefore wrong for the flat shape, which has none of them.  `params`
        # is the only key the shapes that have one share, and the only place
        # the fields must never go, so naming just it is true everywhere.
        #
        # CR-15: the companion fields are described here rather than in the
        # schema.  They are meaningful ONLY on a continuation call, which
        # requires a token, which only ever arrives in this envelope — so the
        # agent always reads this before it could use them.  Describing them in
        # the schema instead cost every caller on every call, forever, to say
        # something only a token-holder can act on.  `available_modes` above
        # advertises `filtered`; without the `filter_keys` clause that mode is
        # visible and unusable, which is the T6 failure one level down.
        #
        # The two `mode` sentences below are pinned byte-for-byte by
        # `test_bare_token_clause_is_unchanged` (B2).  Append beside them;
        # rewording them is a ruling, not an edit.
        "usage": (
            "Re-invoke this same tool with 'continuation_token' at the TOP LEVEL"
            " of arguments, not inside 'params'. 'mode' is optional and goes"
            " beside it."
            f" Omitting 'mode' returns ONE PAGE of the {len(serialized.encode())}-byte"
            " result if it is a list, or the whole object if it is not;"
            " pass mode='full' explicitly for the complete dataset."
            " For 'paginated', 'page' is 1-based and 'page_size' defaults to"
            " FRISIAN_MCP_HEAVY_PAGE_SIZE. For 'filtered', supply 'filter_keys':"
            " the top-level keys of the result to keep. These sit beside 'mode'."
        ),
    }


def _envelope_payload_key(result: Any) -> str | None:
    """
    Return the key holding a paginated envelope's list payload, or ``None``.

    Host-agnostic **by construction**: an envelope is recognised as a dict with
    exactly one list-valued key, and that key is the payload.  No field name is
    hard-coded, so a host using ``results``, ``items``, ``data`` or anything
    else is handled identically and no vendor vocabulary enters ``src/``.

    ``None`` is returned when the shape is not unambiguous — no list value, or
    more than one.  Two lists could each plausibly be the payload and guessing
    would silently paginate the wrong one; the caller keeps the whole result,
    which is the pre-H23 behaviour for that shape.
    """
    if not isinstance(result, dict):
        return None
    list_keys = [k for k, v in result.items() if isinstance(v, list)]
    return list_keys[0] if len(list_keys) == 1 else None


def _serve_heavy_mode(
    result: Any, mode: str, arguments: dict[str, Any], *, single_object: bool = False
) -> Any:
    """
    Serve a cached heavy result in the requested response mode.

    Modes:
    * ``summary``   — first 10 dict keys / 5 list items; values truncated to 100 chars
    * ``paginated`` — one page of a **list** result; honours ``page`` and
      ``page_size`` (default page=1, page_size=FRISIAN_MCP_HEAVY_PAGE_SIZE|20).
      A non-list result is already bounded and is returned whole (T18)
    * ``filtered``  — result filtered to the keys listed in ``filter_keys`` argument
    * ``full``      — complete cached result (must be requested explicitly)

    An unrecognised *mode* raises :exc:`ToolInputError` naming the supported
    enum.  It does **not** fall back to ``full`` (ADR-005 item (b), ruled B2):
    a typo must not select the most expensive possible response, and silently
    serving something other than what was asked for hides the mistake from the
    caller entirely.  The caller never reaches here without a
    ``continuation_token``, so the absent-mode default is applied by the
    redemption path rather than here.

    ``single_object`` is the mint site's statement that *result* is one written
    object rather than a list-shaped response.  It suppresses only the
    envelope-payload-key lookup in ``paginated``; every other mode is
    unaffected, and a ``list`` result still paginates normally so a bulk write
    pages as it should.  It defaults to ``False`` — see the ``paginated``
    branch for why that direction is the safe one.
    """
    if mode == "full":
        return result

    if mode == "summary":
        if isinstance(result, dict):
            return {k: str(v)[:100] for k, v in list(result.items())[:10]}
        if isinstance(result, list):
            return result[:5]
        return {"summary": str(result)[:500]}

    if mode == "paginated":
        # T18: pagination is only coherent for a sequence.  A non-list result —
        # a created/updated object on the ADR-004 write path, or any single-object
        # `retrieve` — is returned whole instead.
        #
        # This branch previously chunked `json.dumps(result)` into fixed-width
        # slices, which cuts the serialisation at an arbitrary offset, usually
        # mid-token.  The caller received neither the object nor anything
        # parseable as one.  That was tolerable while `paginated` had to be asked
        # for explicitly; B2 made it the default for a bare continuation_token,
        # and read and write share one cache prefix and one redemption path
        # (there is no token type to discriminate on), so a bare write-token
        # redemption started returning a truncated string for the object that
        # had just been created.
        #
        # Returning it whole is safe because a single object is *already*
        # bounded — that is why the negotiation fired on size in the first
        # place.  A caller who genuinely needs a large object bounded still has
        # `summary` and `filtered`, both of which stay meaningful on a dict;
        # what is removed is only an option that produced unusable output.
        # H23: a *paginated list envelope* is a dict, and the T18 guard above
        # sent it down the "already bounded" path — so the single most common
        # heavy case (a large list endpoint) returned in full, ignoring
        # `page_size` entirely.  T18 enumerated two non-list shapes, the write
        # result and the single-object retrieve, and missed the third, which is
        # the dominant one in production.
        #
        # `_envelope_payload_key` finds the list to paginate WITHOUT naming a
        # host field: an envelope is a dict with exactly one list-valued key.
        # Ambiguity is not guessed at — zero or several lists means we cannot
        # tell which is the payload, and the result is returned whole as before.
        #
        # CL-6: H23 was right about list endpoints and wrong about one shape it
        # could not see.  A *created object* is also a dict, and its own
        # representation often has exactly one list-valued field — which then
        # satisfies the envelope test and is served as the payload, usually
        # empty on a fresh create.  Redemption reported `total: 0` with the
        # created object displaced into `envelope`, so a client reading the
        # payload concluded nothing had been created.  Two list fields returned
        # `None` and behaved correctly, which is why it stayed hidden.
        #
        # The shapes are indistinguishable here by construction, so the fact is
        # carried from the mint site rather than inferred. This does NOT change
        # how the payload key is found within a genuine list envelope.
        #
        # Only the *lookup* is suppressed, not pagination: a bulk write caches a
        # list at the top level and still pages normally, because a list has no
        # envelope key to find in the first place.
        #
        # The default is False on purpose, and the direction matters: entries
        # minted before this shipped carry no such key and are read with
        # `.get()`.  Defaulting to True would stop in-flight READ entries
        # paginating for the remainder of their TTL — a worse fault than this
        # one, on the dominant path.  Old write entries keep this bug until
        # they age out; that is the acceptable side of the trade.
        payload_key = (
            None if single_object or isinstance(result, list) else _envelope_payload_key(result)
        )
        if not isinstance(result, list) and payload_key is None:
            return result

        items: list[Any] = result if isinstance(result, list) else result[payload_key]
        _default_page_size: int = getattr(settings, "FRISIAN_MCP_HEAVY_PAGE_SIZE", 20)
        _max_page_size: int = getattr(
            settings, "FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE", _default_page_size
        )
        # Redemption deliberately short-circuits schema validation (call-2 needs
        # only the token and a mode), so `page` / `page_size` arrive UNVALIDATED
        # and the redemption path catches only ToolInputError.  A non-numeric
        # value therefore escaped `int()` as TypeError/ValueError and surfaced as
        # a 500 rather than a caller error.  Converting here keeps the one
        # exception type the redemption path already handles.
        try:
            page: int = max(1, int(arguments.get("page", 1)))
            page_size: int = max(
                1,
                min(int(arguments.get("page_size", _default_page_size)), _max_page_size),
            )
        except (TypeError, ValueError) as exc:
            raise ToolInputError(
                f"'page' and 'page_size' must be integers; got page="
                f"{arguments.get('page')!r}, page_size={arguments.get('page_size')!r}."
            ) from exc
        start = (page - 1) * page_size
        end = start + page_size
        served: dict[str, Any] = {
            "items": items[start:end],
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "has_more": end < len(items),
        }
        if payload_key is not None:
            # The envelope's own keys describe the HOST's pagination over the
            # host's full result set; ours describe this slice of the cached
            # page.  `count: 114` beside `total: 50` beside two returned records
            # is three numbers meaning three different things, so the host's are
            # nested rather than merged — nothing is dropped, and nothing sits
            # side by side pretending to be the same measurement.
            served["envelope"] = {k: v for k, v in result.items() if k != payload_key}
            served["envelope_payload_key"] = payload_key
        return served

    if mode == "filtered":
        filter_keys: list[str] = list(arguments.get("filter_keys") or [])
        if isinstance(result, dict) and filter_keys:
            return {k: v for k, v in result.items() if k in filter_keys}
        if isinstance(result, list) and filter_keys:
            return [
                {k: item[k] for k in filter_keys if k in item} if isinstance(item, dict) else item
                for item in result
            ]
        return result

    # The message names only the public enum — identical for every caller, and
    # carrying nothing caller-specific, tool-specific or deploy-state-specific.
    # The redemption path deliberately exposes no caller-varying outcomes.
    raise ToolInputError(
        f"Unknown continuation mode {mode!r}."
        f" Supported modes: {', '.join(NEGOTIATION_MODES)}."
        f" Omit 'mode' for {DEFAULT_NEGOTIATION_MODE!r} (one bounded page)."
    )


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _jsonrpc_success(request_id: JsonRpcId, result: JsonDict) -> JsonResponse:
    """Return a JSON-RPC 2.0 success response."""
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _caller_visible_schema(request: Any, tool_name: str) -> Any:
    """Return the ``inputSchema`` for *tool_name* as surfaced to *this* caller.

    Mirrors :meth:`ToolRegistry.list_tools` exactly so that ``schema_tokens``
    counts what the caller could actually see -- the tier-filtered (and, under
    ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY``, permission-filtered) dispatcher
    schema, not the full one.  Counting the full schema on a tier-capped route
    would disclose, via the token count, the size of actions the caller is not
    authorised to see (security TUR-6).  Returns ``None`` when the tool is not
    visible to the request, in which case the count is treated as empty.

    Only called when usage reporting has already resolved ON, so the possibly
    non-trivial schema rebuild never runs on the default (disabled) path.
    """
    entry = _request_visible_entry(request, tool_name)
    if entry is None:
        return None
    # Plain (non-dispatcher) tool, or a dispatcher without meta: the entry's own
    # permission_tier already gated the whole tool, so the schema is unfiltered.
    if not entry.is_dispatcher or entry.dispatcher_meta is None:
        return entry.input_schema
    # Dispatcher: rebuild the action enum filtered to the caller's tier and, when
    # permission-aware discovery is on, their Django capabilities -- reusing the
    # exact helpers tools/list and action="help" use.
    from frisian_mcp.backends.dispatcher import (  # pylint: disable=import-outside-toplevel
        _build_dispatcher_input_schema,
        _build_perm_action_filter_from_request,
    )

    action_filter = _build_perm_action_filter_from_request(request, tool_name)
    return _build_dispatcher_input_schema(
        entry.dispatcher_meta,
        max_tier=_get_token_permission(request),
        action_filter=action_filter,
    )


def _usage_success(
    request: Any,
    request_id: JsonRpcId,
    payload: Any,
    *,
    tool_name: str,
    usage_args: Any,
) -> JsonResponse:
    """Build a ``tools/call`` success response, attaching ``_usage`` when enabled.

    Serializes *payload* once into ``content[0].text`` and reuses that exact
    string for ``result_tokens``.  When token-usage reporting resolves OFF (the
    default, and unconditionally under a system ``deny``) the result is
    byte-identical to the pre-feature output -- no ``_usage`` key, no counting,
    and the schema rebuild is skipped via the lazy ``schema_json`` callable.
    """
    emitted_text = json.dumps(payload)
    result_obj: JsonDict = {
        "content": [{"type": "text", "text": emitted_text}],
        "isError": False,
    }
    maybe_attach_usage(
        result_obj,
        request=request,
        schema_json=lambda: _caller_visible_schema(request, tool_name),
        arguments=usage_args,
        emitted_text=emitted_text,
    )
    return _jsonrpc_success(request_id, result_obj)


def _jsonrpc_error(
    request_id: JsonRpcId,
    code: int,
    message: str,
    data: Any = None,
) -> JsonResponse:
    """
    Return a JSON-RPC 2.0 error response.

    *data* may be any JSON-serialisable value.  Strings remain the common
    case (e.g. close-match suggestions for an unknown tool); dicts are used
    by the lite-mode escape hatch (see :func:`_lite_enrich_error`) to attach
    the failing tool's ``inputSchema`` so the agent can self-teach without
    a separate ``tools/list`` round-trip.
    """
    error: JsonDict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


def _request_visible_entry(request: Any, tool_name: str) -> Any:
    """
    Return the tool entry for *tool_name* as visible to **this request**.

    Absence must hold on every observable surface (WI-1).  The lite escape
    hatch resolves the entry it is about to hand back through this helper so
    that, on a per-route mount, it cannot return the schema of a tool the
    route denies — the "No tool registered" error would arrive carrying the
    denied tool's full input contract — nor of a tool the effective tier
    hides, which ``registry.dispatch`` has just reported as nonexistent.

    On a plain mount (no ``request._mcp_route_view``) this is exactly the
    global-registry lookup the escape hatch always performed; the tier half of
    the shipped lite-schema exposure is tracked separately (CR-19) and is
    deliberately not changed here.
    """
    route_view: RouteView | None = getattr(request, "_mcp_route_view", None) if request else None
    if route_view is None:
        return tool_registry.get_entry(tool_name)
    entry = route_view.entries.get(tool_name)
    if entry is None:
        return None
    caller_rank = _caller_rank(_get_token_permission(request))
    if _TIER_RANK.get(entry.permission_tier, 0) > caller_rank:
        return None
    return entry


def _dispatcher_audit_labels(
    request: Any, tool_name: str, arguments: dict[str, Any]
) -> tuple[str | None, str | None]:
    """
    Return validated ``(resource, action)`` config-vocabulary labels for the audit.

    Only labels that resolve to a real member of the addressed dispatcher are
    returned; everything else is ``(None, None)``.  A **group** dispatcher's
    ``resource``/``action`` are free-form caller strings — the schema has no
    enum, so validity is enforced by a ``LookupError`` at dispatch, not by the
    JSON schema.  On an unknown pair (the deny path) they are raw,
    potentially-sensitive caller input and must not reach the audit sink, whose
    contract is routing labels only — no PII, no secrets.

    Validation is against the **unpruned** membership of the route view's backing
    registry, not the route's pruned view: a resource denied on this route is
    still legitimate config vocabulary (it is named in the route's ``deny_list``),
    whereas arbitrary caller text is a member of no group at all.  A
    ``@mcp_dispatcher``'s ``action`` is already enum-constrained by its schema, so
    it is trusted when it names a known action; that dispatcher has no
    ``resource`` addressing.
    """
    route_view = getattr(request, "_mcp_route_view", None)
    # The view's backing registry holds the FULL (unpruned) group membership; the
    # view's own entries are deny-carved, which would wrongly suppress a denied-
    # but-real resource.  Fall back to the global registry on a plain mount.
    registry = getattr(route_view, "_registry", None) or tool_registry
    entry = registry.get_entry(tool_name)
    if entry is None or not entry.is_dispatcher:
        return None, None
    resource = arguments.get("resource")
    action = arguments.get("action")
    if entry.group_tool_names is not None:
        sep = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
        if (
            isinstance(resource, str)
            and isinstance(action, str)
            and f"{resource}{sep}{action}" in entry.group_tool_names
        ):
            return resource, action
        return None, None
    if entry.dispatcher_meta is not None:
        known_actions = getattr(entry.dispatcher_meta, "actions", {})
        if isinstance(action, str) and action in known_actions:
            return None, action
    return None, None


#: Length cap for the one audit field that can carry raw caller input.
_AUDIT_LABEL_CAP = 128


def _sanitize_audit_label(value: Any) -> str | None:
    """
    Return *value* made safe for the audit sink: printable, bounded, ``str``.

    ``tool`` is the one payload field that can carry raw caller input — on the
    unknown-tool deny path the probed name IS the forensic record, so it must
    be kept (a fixed ``'unknown'`` placeholder would erase exactly the probe an
    audit trail exists to show).  Keeping it verbatim, however, hands callers a
    log-injection primitive: CR/LF and other control characters can forge
    record boundaries in line-oriented sinks, and unbounded length is a
    storage/DoS vector.  Sanitize-and-keep: strip non-printables, cap length
    with an explicit truncation marker.  Registered tool names are short
    printable identifiers, so this is a no-op for every legitimate call.
    """
    if value is None:
        return None
    cleaned = "".join(ch for ch in str(value) if ch.isprintable())
    if len(cleaned) > _AUDIT_LABEL_CAP:
        cleaned = cleaned[:_AUDIT_LABEL_CAP] + "…[truncated]"
    return cleaned


def _log_audit_context(
    request: Any,
    tool_name: str,
    arguments: dict[str, Any],
    decision: str,
    reason: str | None,
) -> None:
    """
    Emit one DOC-7 audit-context record for a resolved ``tools/call``.

    Everything in the payload is already computed on the request path — route
    identity, effective ceiling, effective tier, permission decision — so this
    seam records it instead of discarding it; the downstream SOC sink attaches
    a handler to :data:`audit_logger` and consumes the record verbatim.

    The payload carries **routing labels only**: route/tier vocabulary, the
    canonical mount path, the addressed tool and (for dispatchers) its
    ``resource``/``action`` labels.  Caller argument *values*, token material,
    and user identity are deliberately excluded — no PII, no secrets.  The
    addressed tool name passes through :func:`_sanitize_audit_label` because on
    the unknown-tool path it is caller text.
    """
    route_view: RouteView | None = getattr(request, "_mcp_route_view", None)
    # resource/action are logged only when they are validated config-vocabulary
    # labels of the addressed dispatcher (see _dispatcher_audit_labels).  On a
    # flat tool, or on a dispatcher call whose resource/action do not name a real
    # member, they are treated as caller data and excluded — no raw caller input
    # reaches the sink even on the unknown-pair deny path.
    resource_label, action_label = _dispatcher_audit_labels(request, tool_name, arguments)
    audit_logger.info(
        "mcp_audit_context",
        extra={
            "route": route_view.route_name if route_view is not None else None,
            "route_path": (
                route_view.path if route_view is not None else getattr(request, "path", None)
            ),
            "effective_ceiling": getattr(request, "_mcp_max_tier", None),
            "effective_tier": getattr(request, "_mcp_effective_tier", None),
            "tool": _sanitize_audit_label(tool_name),
            "resource": resource_label,
            "tool_action": action_label,
            "decision": decision,
            "reason": reason,
        },
    )


def _lite_enrich_error_content(
    content: dict[str, Any],
    tool_name: str,
    lite: bool,
    request: Any = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Attach the failing tool's ``inputSchema`` to an ``isError`` content dict.

    ``tools/call`` failures that surface as ``isError=true`` content blocks
    (rather than JSON-RPC ``error`` responses) carry their detail in
    ``content[0].text`` as JSON.  This helper mirrors :func:`_lite_enrich_error`
    for that path: when *lite* is ``True`` and the tool exists — as visible to
    *request* (WI-1) — attach the tool's ``inputSchema`` to the content dict so
    the agent can self-correct.

    Args:
        content: The dict that will be JSON-serialised into
            ``content[0].text`` of an ``isError=true`` response.
        tool_name: The tool name the caller invoked.
        lite: The per-call ``lite`` flag extracted from arguments.
        request: The current request; scopes the entry lookup to the route
            view and effective tier when present.
        arguments: The call's arguments.  When supplied **and** *tool_name* is
            a group dispatcher that routes them to one of its members, the
            member's schema is echoed instead of the dispatcher's — see below.
            Omit it to keep the dispatcher's own schema.

    Returns:
        Either *content* unchanged, or a new dict with ``"inputSchema"``
        added.  Never mutates *content* in place.

    """
    if not lite or not tool_name:
        return content
    entry = _request_visible_entry(request, tool_name)
    if entry is None:
        return content
    # The entry is looked up by the name the CALLER invoked, which on a grouped
    # call is the dispatcher.  Echoing its schema hands back
    # ``{resource, action, params}`` — the shape the caller already had — and
    # never names the member's fields, so the hatch could not disclose a
    # missing field on the dominant call shape.
    #
    # ``arguments`` is passed only where the failure is against a MEMBER's
    # schema (input validation).  Everywhere else it is omitted and the
    # dispatcher's schema is still the right answer: an unknown resource or a
    # tier denial is a failure against the dispatcher, and resolving inward
    # there would describe a tool the caller never reached.
    #
    # ``_dispatcher_target_entry`` is reused rather than resolving here: it
    # already carries the membership check that stops a caller-supplied
    # ``resource`` reaching an arbitrary global tool, and it returns ``None``
    # for class dispatchers, which deliberately resolve nothing.
    #
    # F1: membership is NOT sufficient on its own, and the reasoning that said
    # it was covered only half the paths.  "``registry.dispatch`` runs its tier
    # check before argument validation, so the member was already cleared" holds
    # when the MEMBER's schema rejects — but when the DISPATCHER's own schema
    # rejects (``params`` not an object) ``registry.dispatch`` raises before
    # calling ``entry.fn``, so ``make_group_invoke``'s per-action gate never
    # runs.  The only tier check that fired was the dispatcher's, and
    # dispatchers are registered ``read`` deliberately so they stay visible as
    # navigation entry-points — a no-op.  ``resource``/``action`` remain
    # caller-supplied on that path, and ``_dispatcher_target_entry`` resolves
    # against the GLOBAL registry with no route or tier filter of its own.
    #
    # So the resolved member goes back through the same gate the outer name
    # did.  Absence has to hold on every observable surface (WI-1): a route
    # that denies a tool, or an effective tier that hides it, must not have its
    # contract arrive inside an error about something else.  A hidden member
    # falls back to the dispatcher's schema — the same outcome an unroutable
    # ``resource``/``action`` pair already produces — so self-correction is
    # narrowed to members the caller can actually invoke, not removed.
    #
    # On a plain mount ``_request_visible_entry`` is the global lookup the hatch
    # always performed, so this deliberately leaves that posture unchanged; the
    # tier half there is tracked separately (CR-19).
    if arguments:
        target = _dispatcher_target_entry(entry, arguments)
        if target is not None:
            visible = _request_visible_entry(request, target.name)
            if visible is not None:
                entry = visible
    return {**content, "inputSchema": entry.input_schema}


def _lite_enrich_error(
    response: JsonResponse, tool_name: str, lite: bool, request: Any = None
) -> JsonResponse:
    """
    Attach the failing tool's ``inputSchema`` to a JSON-RPC error response.

    Implements the lite-mode escape hatch: when a caller passes
    ``lite: true`` on a ``tools/call`` and the call fails (bad params,
    unknown action, validation error, etc.), the response includes the
    tool's input schema so the agent can self-correct without re-fetching
    ``tools/list``.  Lite mode normally suppresses scaffolding; a failure
    re-includes it.

    When *lite* is ``False`` or the tool is not registered — or not visible to
    *request*'s route view and effective tier (WI-1) — *response* is returned
    unchanged.  Otherwise the response body is rewritten so that
    ``error.data`` is a structured dict containing ``"detail"`` (the original
    string data, when present) and ``"inputSchema"`` (the tool's schema).

    Args:
        response: The JSON-RPC error response built by :func:`_jsonrpc_error`.
        tool_name: The tool name the caller invoked.
        lite: The per-call ``lite`` flag extracted from arguments.
        request: The current request; scopes the entry lookup to the route
            view and effective tier when present.

    Returns:
        Either *response* unchanged, or a new ``JsonResponse`` with the
        ``inputSchema`` attached to ``error.data``.

    """
    if not lite or not tool_name:
        return response
    entry = _request_visible_entry(request, tool_name)
    if entry is None:
        return response
    body = json.loads(response.content)
    err = body.get("error")
    if not isinstance(err, dict):
        return response
    existing = err.get("data")
    enriched: dict[str, Any]
    if isinstance(existing, dict):
        enriched = {**existing}
    elif existing is None:
        enriched = {}
    else:
        enriched = {"detail": existing}
    enriched["inputSchema"] = entry.input_schema
    err["data"] = enriched
    return JsonResponse(body)


# ---------------------------------------------------------------------------
# Error content builders
# ---------------------------------------------------------------------------


def _build_drf_error_content(exc: DRFValidationError) -> dict[str, Any]:
    """
    Convert a DRF ``ValidationError`` into a structured tool-error dict.

    * Field errors (dict detail) →
      ``{"error": "Validation failed", "detail": {field: [msgs]}, "status_code": 422}``
    * Bulk errors (list of dicts) →
      ``{"error": "Validation failed", "detail": ["row 0: …", …], "status_code": 422}``
    * Non-field errors (flat list) → ``{"error": "<joined messages>", "status_code": 422}``
    * Scalar detail → ``{"error": "<string>", "status_code": 422}``

    The result is safe to JSON-serialise and return to the MCP caller inside
    an ``isError=True`` content block.
    """
    from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
        _flatten_error_detail,
    )

    detail = exc.detail
    if isinstance(detail, dict):
        return {
            "error": "Validation failed",
            "detail": {
                field: [str(e) for e in (errors if isinstance(errors, list) else [errors])]
                for field, errors in detail.items()
            },
            "status_code": 422,
        }
    if isinstance(detail, list):
        if detail and isinstance(detail[0], dict | list):
            # Bulk validation: detail is a list of per-row error dicts.
            # Flatten each row using _flatten_error_detail to avoid Python repr()
            # of ErrorDetail objects (e.g. "{'field': [ErrorDetail(...)]}").
            rows: list[str] = []
            for i, row_err in enumerate(detail):
                msg = _flatten_error_detail(row_err)
                if msg:
                    rows.append(f"row {i}: {msg}")
            return {
                "error": "Validation failed",
                "detail": rows if rows else ["unknown error"],
                "status_code": 422,
            }
        return {"error": "; ".join(str(e) for e in detail), "status_code": 422}
    return {"error": str(detail), "status_code": 422}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _resolve_classes(setting_name: str) -> list[Any] | None:
    """
    Resolve a settings list of class paths or class objects.

    Returns ``None`` when the setting is absent (caller should fall back to
    DRF defaults).  Returns an empty list when the setting is explicitly ``[]``.

    Each element may be:

    * A dotted-path string (e.g. ``"rest_framework.authentication.SessionAuthentication"``).
    * A class object already imported by the host project.

    """
    raw = getattr(settings, setting_name, None)
    if raw is None:
        return None
    return [import_string(cls) if isinstance(cls, str) else cls for cls in raw]


def _get_permission_adapter() -> Any:
    """
    Load and instantiate the configured ``PermissionAdapter``.

    Reads ``FRISIAN_MCP_PERMISSION_ADAPTER`` (dotted import path).  Defaults
    to :class:`~frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter`.
    Cached as a module-level singleton so the import happens once per process.
    """
    dotted: str = getattr(
        settings,
        "FRISIAN_MCP_PERMISSION_ADAPTER",
        "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    cls = import_string(dotted)
    return cls()


def _make_perm_entry_filter(capabilities: Container[str]) -> Any:
    """
    Return a ``_ToolEntry`` filter callable for the given capability set.

    A thin adapter over :func:`entry_is_visible`, which is the single place the
    "what can this caller see" question is answered.  The logic deliberately
    does not live here: it used to, and three other consumers kept a sibling
    copy whose indeterminate branch meant the opposite.
    """

    def _filter(entry: Any) -> bool:
        return entry_is_visible(entry, capabilities)

    return _filter


def _make_perm_action_filter_factory(
    capabilities: Container[str],
) -> Any:
    """
    Build an ``action_filter_factory`` for permission-filtered dispatcher action enums.

    A thin adapter over :func:`build_action_filter`.  ``None`` from the factory
    means "publish every action" and is reached only via an explicit
    ``universal_discovery`` declaration; an indeterminate dispatcher gets
    :func:`deny_all_actions` so its enum empties and ``list_tools`` drops it.
    """

    def factory(entry: Any) -> Any:
        return build_action_filter(entry, capabilities)

    return factory


def _ensure_perm_context_on_request(request: Any) -> None:
    """
    Compute and cache permission context on *request* if not already done.

    Sets two attributes:

    * ``_mcp_capabilities`` — a ``Container[str]`` of Django permission strings
      the requesting user holds (lazy: resolved via ``has_perm`` on lookup), or
      ``None`` when permission-aware discovery is disabled or the user is
      unrestricted (superuser).
    * ``_mcp_perm_entry_filter`` — a ``(_ToolEntry) -> bool`` callable built
      from the capabilities, or ``None`` for the same conditions.

    Idempotent: a second call on the same request object is a no-op.
    Both dispatchers and the tools/list handler call this so capabilities are
    computed exactly once per request regardless of which path runs first.
    """
    # pylint: disable=protected-access
    if hasattr(request, "_mcp_capabilities"):
        return
    perm_aware: bool = getattr(settings, "FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY", False)
    if not perm_aware:
        request._mcp_capabilities = None
        request._mcp_perm_entry_filter = None
        return
    user = getattr(request, "user", None)
    if user is None:
        request._mcp_capabilities = None
        request._mcp_perm_entry_filter = None
        return
    # Blanket-tier mode: OAuthServicePrincipal (User field blank on OAuthClient).
    # The tier is the sole gate; per-capability filtering does not apply.
    # Operators who want Django-permission scoping must link a Django User to the
    # OAuthClient — matching the "API token" design the admin UI describes.
    if getattr(user, "_mcp_is_service_principal", False) is True:
        request._mcp_capabilities = None
        request._mcp_perm_entry_filter = None
        return
    adapter = _get_permission_adapter()
    if adapter.is_unrestricted(user):
        request._mcp_capabilities = None
        request._mcp_perm_entry_filter = None
        return
    caps: Container[str] = adapter.get_capabilities(user)
    request._mcp_capabilities = caps
    request._mcp_perm_entry_filter = _make_perm_entry_filter(caps)


def _resolve_agent_connection_state(request: Any) -> tuple[Any | None, bool]:
    """
    Look up the AgentConnection state for ``request.auth``.

    Returns ``(active_connection_or_None, has_inactive_match)``:

    * ``(conn, False)`` — at least one active AgentConnection links the
      credential; ``conn`` is the most-recent.  Apply per-agent filtering.
    * ``(None, True)`` — the credential IS linked to at least one
      AgentConnection but all of them are inactive.  SEC-5: fail closed.
      Operators who deactivate an agent expect access to stop, not for
      filtering to silently disappear.
    * ``(None, False)`` — no AgentConnection links this credential.  Pass
      through with no filtering (existing default-allow contract).

    Resolution order:

    1. ``frisian_mcp.contrib.agents`` not installed → ``(None, False)``.
    2. ``request.auth`` is a
       :class:`~frisian_mcp.contrib.tokens.models.FrisianMcpToken` → look up
       AgentConnections linked via ``token``.
    3. ``request.auth`` is an
       :class:`~frisian_mcp.contrib.oauth.models.OAuthAccessToken` → look up
       AgentConnections linked via the parent ``OAuthClient``.
    4. Otherwise → ``(None, False)``.
    """
    from django.apps import apps as django_apps  # pylint: disable=import-outside-toplevel

    if not django_apps.is_installed("frisian_mcp.contrib.agents"):
        return None, False

    auth = getattr(request, "auth", None)
    if auth is None:
        return None, False

    queryset = None

    try:
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        if isinstance(auth, FrisianMcpToken):
            queryset = auth.agent_connections.select_related("token", "oauth_client")
    except ImportError:
        pass

    if queryset is None:
        try:
            from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
                OAuthAccessToken,
            )

            if isinstance(auth, OAuthAccessToken):
                queryset = auth.client.agent_connections.select_related("token", "oauth_client")
        except ImportError:
            pass

    if queryset is None:
        return None, False

    active = queryset.filter(is_active=True).order_by("-created_at").first()
    if active is not None:
        return active, False

    # No active match — but maybe the credential is linked to an inactive one.
    # SEC-5: an admin who deactivated the agent expects the credential to stop
    # working, not for filtering to silently disappear.
    has_inactive = queryset.exists()
    return None, has_inactive


def _get_agent_connection(request: Any) -> Any | None:
    """
    Return the active AgentConnection for ``request.auth``, or ``None``.

    Backwards-compatible thin wrapper around
    :func:`_resolve_agent_connection_state` that drops the
    ``has_inactive_match`` signal.  Callers that need the SEC-5 fail-closed
    behaviour should use the resolver directly.
    """
    conn, _ = _resolve_agent_connection_state(request)
    return conn


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------


def _maybe_sse(response: HttpResponse, request: Any) -> HttpResponse | StreamingHttpResponse:
    """
    Wrap *response* as a single-message SSE stream when the caller accepts it.

    Returns *response* unchanged when:

    * The request ``Accept`` header does not include ``text/event-stream``, or
    * *response* is not a :class:`~django.http.JsonResponse` (e.g. HTTP 202
      notifications have no body to stream).

    When SSE is accepted, returns a :class:`~django.http.StreamingHttpResponse`
    with ``Content-Type: text/event-stream`` and ``Cache-Control: no-cache``
    containing a single ``data:`` event followed by the double-newline delimiter.

    """
    if not isinstance(response, JsonResponse):
        return response
    accept: str = request.META.get("HTTP_ACCEPT", "")
    if "text/event-stream" not in accept:
        return response
    if "application/json" in accept:
        return response

    body: str = response.content.decode("utf-8")

    def _stream() -> Generator[str, None, None]:
        yield f"data: {body}\n\n"

    sse: StreamingHttpResponse = StreamingHttpResponse(_stream(), content_type="text/event-stream")
    sse["Cache-Control"] = "no-cache"
    for header, value in response.items():
        if header.lower() not in ("content-type", "content-length"):
            sse[header] = value
    return sse


# ---------------------------------------------------------------------------
# Middleware dispatch helper
# ---------------------------------------------------------------------------


def _tool_registry_dispatch(request: HttpRequest, tool_name: str, arguments: dict[str, Any]) -> Any:
    """
    Inner dispatch callable passed to the middleware chain.

    On a per-route mount (``request._mcp_route_view`` stamped by
    :meth:`McpView.post`) invocation goes through the route's deny-carved
    :class:`~frisian_mcp.route_views.RouteView`, so a denied tool raises the
    same :class:`ToolNotFoundError` a never-registered tool raises.  Plain
    mounts keep today's global-registry path unchanged.
    """
    route_view: RouteView | None = getattr(request, "_mcp_route_view", None)
    if route_view is not None:
        return route_view.dispatch(request, tool_name, arguments)
    return tool_registry.dispatch(request, tool_name, arguments)


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


def _server_version() -> str:
    """Return the installed frisian-mcp package version, or ``'unknown'`` as fallback."""
    try:
        return importlib.metadata.version("frisian-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _handle_initialize(request_id: JsonRpcId, params: JsonDict) -> JsonResponse:
    """Handle ``initialize`` — MCP protocol handshake."""
    client_info: Any = params.get("clientInfo", {})
    protocol_version: Any = params.get("protocolVersion", MCP_PROTOCOL_VERSION)

    logger.info(
        "mcp_initialize",
        extra={
            "client_name": client_info.get("name") if isinstance(client_info, dict) else None,
            "client_version": (
                client_info.get("version") if isinstance(client_info, dict) else None
            ),
            "protocol_version": protocol_version,
        },
    )

    server_name: str = getattr(settings, "FRISIAN_MCP_SERVER_NAME", "frisian-mcp")
    tool_names = sorted(t["name"] for t in tool_registry.list_tools())
    tools_version = hashlib.sha256(",".join(tool_names).encode()).hexdigest()[:8]
    response = _jsonrpc_success(
        request_id,
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": server_name,
                "version": _server_version(),
            },
            "capabilities": {"tools": {}, "resources": {}},
            "toolsVersion": tools_version,
        },
    )
    if getattr(settings, "FRISIAN_MCP_SESSION_ID_HEADER", True):
        response["Mcp-Session-Id"] = str(uuid.uuid4())
    return response


def _handle_initialized(request_id: JsonRpcId) -> JsonResponse:
    """Handle ``initialized`` — client confirms handshake; acknowledgement only."""
    logger.info("mcp_initialized")
    return _jsonrpc_success(request_id, {})


def _decode_cursor(cursor: str) -> int:
    """
    Decode a base64url cursor string to an integer offset.

    Raises :exc:`ValueError` when *cursor* is not a valid base64url-encoded
    integer so the caller can surface INVALID_PARAMS to the client.
    """
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {cursor!r}") from exc


def _encode_cursor(offset: int) -> str:
    """Encode an integer offset as a base64url cursor string."""
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _handle_tools_list(  # pylint: disable=too-many-locals
    request_id: JsonRpcId, request: Any, params: JsonDict
) -> JsonResponse:
    """
    Handle ``tools/list`` — return the tool manifest from the registry.

    When ``frisian_mcp.contrib.agents`` is installed and ``request.auth`` maps to
    an active :class:`~frisian_mcp.contrib.agents.models.AgentConnection` with a
    non-null ``allowed_tools`` list, only those tools are included in the
    response.  All other callers receive the full manifest.

    When ``FRISIAN_MCP_TOOLS_PAGE_SIZE`` is set, results are paginated using an
    opaque base64url cursor that encodes a simple integer offset.  Clients pass
    the returned ``nextCursor`` in subsequent requests to advance through pages.
    When the setting is absent, all tools are returned in a single response with
    no ``nextCursor`` key (default, zero-behavior-change).

    **Auth note:** Beyond per-agent filtering, this handler does not perform
    additional authentication or permission checks.  The host application is
    responsible for gateway-level auth-gating via
    ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` / ``FRISIAN_MCP_PERMISSION_CLASSES`` or
    upstream infrastructure.
    """
    conn, has_inactive_match = _resolve_agent_connection_state(request)
    # SEC-5: when the credential is bound to AgentConnection(s) but every
    # one is inactive, fail closed — the operator deactivated the agent
    # and expects access to stop, not for filtering to silently disappear.
    if conn is None and has_inactive_match:
        return _jsonrpc_success(request_id, {"tools": []})
    max_tier = _get_token_permission(request)
    # PR-7: on a per-route mount the manifest comes from the route's deny-carved
    # RouteView, and max_tier is the effective tier stamped in McpView.post()
    # (min of token tier, route ceiling, and FRISIAN_MCP_MAX_TIER) — discovery
    # reads the capped tier so a write-capable token on a read-ceiling route is
    # never shown a write action that would then fail at invoke (WI-2).
    route_view: RouteView | None = getattr(request, "_mcp_route_view", None)
    cache_ttl: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_LIST_CACHE_TTL", None)
    # Use a per-tier cache key so authenticated requests benefit from caching
    # too.  Per-route mounts get a per-route key: same tier, different route ⇒
    # different deny-carved surface, so sharing the legacy key would leak one
    # route's manifest to another.
    if route_view is not None:
        cache_key = f"{_TOOLS_LIST_CACHE_KEY}:{route_view.route_name}:{max_tier or 'all'}"
    else:
        cache_key = f"{_TOOLS_LIST_CACHE_KEY}:{max_tier or 'all'}"
    # Permission-aware discovery builds a per-user entry filter; bypassing the
    # shared tier-based cache ensures different users see only their own tools.
    perm_aware: bool = getattr(settings, "FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY", False)
    use_cache = (
        cache_ttl is not None and (conn is None or conn.allowed_tools is None) and not perm_aware
    )

    # Resolve per-user filters when permission-aware discovery is on.
    # _ensure_perm_context_on_request caches the result on the request so tools/call
    # invocations that follow in the same request cycle share the same computation.
    _ensure_perm_context_on_request(request)
    entry_filter = None
    action_filter_factory = None
    caps: Container[str] | None = getattr(request, "_mcp_capabilities", None)
    if caps is not None:
        entry_filter = _make_perm_entry_filter(caps)
        action_filter_factory = _make_perm_action_filter_factory(caps)

    _lister = route_view.list_tools if route_view is not None else tool_registry.list_tools
    if use_cache:
        tools: list[dict[str, Any]] | None = django_cache.get(cache_key)
        if tools is None:
            tools = _lister(max_tier=max_tier)
            django_cache.set(cache_key, tools, cache_ttl)
    else:
        tools = _lister(
            max_tier=max_tier,
            entry_filter=entry_filter,
            action_filter_factory=action_filter_factory,
        )

    if conn is not None and conn.allowed_tools is not None:
        allowed: frozenset[str] = frozenset(conn.allowed_tools)
        tools = [t for t in tools if t["name"] in allowed]

    page_size: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_PAGE_SIZE", None)
    if page_size is None:
        return _jsonrpc_success(request_id, {"tools": tools})

    cursor_str: Any = params.get("cursor")
    offset = 0
    if cursor_str is not None:
        try:
            offset = _decode_cursor(str(cursor_str))
        except ValueError:
            return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid cursor")

    page = tools[offset : offset + page_size]
    result: dict[str, Any] = {"tools": page}
    next_offset = offset + page_size
    if next_offset < len(tools):
        result["nextCursor"] = _encode_cursor(next_offset)
    return _jsonrpc_success(request_id, result)


def _handle_tools_call(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    request: HttpRequest,
    request_id: JsonRpcId,
    params: JsonDict,
) -> JsonResponse:
    """Handle ``tools/call`` — validate and dispatch to the tool registry."""
    tool_name: Any = params.get("name")
    arguments: Any = params.get("arguments") or {}

    if not tool_name or not isinstance(tool_name, str):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid params", "'name' is required")
    if not isinstance(arguments, dict):
        return _jsonrpc_error(
            request_id, INVALID_PARAMS, "Invalid params", "'arguments' must be an object"
        )

    # Per-call ``lite`` opt-in (Issue 4 / Repeated-Path Token Reduction).  When
    # ``lite: true`` is present we (a) suppress dispatcher help scaffolding on
    # success and (b) re-include the tool's ``inputSchema`` on failure as a
    # self-teaching escape hatch.  Strip it from ``arguments`` here so the
    # underlying tool implementation never sees the protocol flag.  Pattern
    # matches how ``verify`` is stripped on the write-path below.
    # Snapshot the ORIGINAL inbound arguments for the usage ``request_tokens``
    # count (TUR-1 sec 2): count what the agent actually sent, before lite/verify
    # stripping or key normalization.  A copy so nothing downstream perturbs it.
    _usage_args: dict[str, Any] = dict(arguments)

    arguments = dict(arguments)
    _lite: bool = bool(arguments.pop("lite", False))
    # Fallback: agents that cached an older schema (before ``lite`` was added as
    # a top-level dispatcher property) pass ``lite`` inside the ``params`` bag.
    # Extract it there too so lite works regardless of which schema the agent saw.
    if not _lite and isinstance(arguments.get("params"), dict):
        arguments["params"] = dict(arguments["params"])
        _lite = bool(arguments["params"].pop("lite", False))

    # Per-agent tool allowlist: when an active AgentConnection with a non-null
    # allowed_tools list is linked to the caller's credential, reject any tool
    # name not in that list before reaching the registry.
    conn, has_inactive_match = _resolve_agent_connection_state(request)
    # SEC-5: fail closed when the credential is bound to AgentConnection(s)
    # but every one is inactive.  Returning isError=true (not a JSON-RPC
    # protocol error) so MCP clients render it as a normal tool denial and
    # the JSON-RPC session stays alive for the caller to inspect.
    if conn is None and has_inactive_match:
        # DOC-7: this is a fail-closed permission decision — audit it before
        # returning, since these pre-dispatch returns never reach the try/finally.
        _log_audit_context(request, tool_name, arguments, decision="deny", reason="agent_inactive")
        return _jsonrpc_success(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "error": (
                                    "Agent connection is inactive; this credential "
                                    "is not currently authorised to call MCP tools."
                                )
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )
    if conn is not None and conn.allowed_tools is not None:
        if tool_name not in frozenset(conn.allowed_tools):
            # DOC-7: an agent-allowlist rejection is a permission denial — the
            # most audit-relevant decision on this path; record it before return.
            _log_audit_context(
                request, tool_name, arguments, decision="deny", reason="agent_not_allowed"
            )
            return _jsonrpc_success(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": (
                                        f"Tool {tool_name!r} is not permitted "
                                        "for this agent connection"
                                    )
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )

    # ADR-011 §4 requires redemption to re-evaluate capability visibility, and
    # that lens lives on the request.  Resolve it BEFORE the continuation branch:
    # the branch returns without reaching the dispatch-time call further down, so
    # leaving it there made `_redemption_target_authorized` read an attribute
    # that did not exist yet and skip the capability check silently — the gate
    # was present, tested, and inert on every real redemption.
    #
    # This is not the G4 short-circuit being weakened.  G4 forbids re-*dispatch*
    # — re-running the tool's query — and resolving permission context is not
    # dispatch.  The call is idempotent, so the existing one below becomes a
    # no-op rather than a second resolution.
    _ensure_perm_context_on_request(request)

    # Heavy response negotiation: if continuation_token is present, serve the cached
    # result without dispatching to the tool again.  This short-circuits schema
    # validation, which is intentional — call-2 arguments only need the token + mode.
    cont_token: str | None = arguments.get("continuation_token")
    if cont_token is not None:
        # A missing alias means no continuation store at all, so every token is
        # a miss and falls to the ordinary expired-or-not-found outcome below.
        # No new client-visible result (ADR-011 §6), and nothing is served from
        # the cache the isolation setting was pointed away from.
        _hc = _heavy_cache()
        cached = _hc.get(f"{_HEAVY_CACHE_PREFIX}{cont_token}") if _hc is not None else None
        # SEC-3: legacy raw-result entries (pre-fix deploys) lack the owner
        # binding and are treated as expired — better a brief disruption
        # during cutover than serving cross-caller data.
        is_bound = isinstance(cached, dict) and "owner_key" in cached and "result" in cached
        if cached is None or not is_bound:
            # The two denials are operationally different — a genuine miss
            # (expired or never issued) versus a pre-SEC-3 entry rejected on
            # cutover — and during an upgrade window the second produces a wave
            # of denials that would otherwise be indistinguishable from normal
            # expiry.  The distinction goes in the audit reason, NOT in the
            # response: the client string stays one message for both.  Redemption
            # already exposes two outcomes to the caller ("expired or not found"
            # versus the owner mismatch below), which is a token-validity oracle
            # — harmless while tokens are 128-bit, but a third outcome would
            # additionally disclose server deploy state ("this host still holds
            # pre-SEC-3 entries") to any token holder, anonymous callers on open
            # mounts included.  Operators get the discrimination from the log;
            # callers do not get it from the wire.
            _log_audit_context(
                request,
                tool_name,
                arguments,
                decision="deny",
                reason="continuation_expired" if cached is None else "continuation_unbound_legacy",
            )
            return _continuation_refused(request_id)
        # SEC-3: refuse to serve when the current caller does not match the
        # caller that issued the continuation.  Owner key composes auth
        # identity, tier, user, agent connection, and tool name; any drift
        # (different token, different tool, downgraded tier, different
        # agent connection) terminates the negotiation safely.
        expected_owner: str = cached.get("owner_key", "")
        actual_owner = _heavy_owner_key(request, tool_name)
        if expected_owner != actual_owner:
            logger.warning(
                "heavy_continuation_owner_mismatch",
                extra={"tool": tool_name},
            )
            # DOC-7: SEC-3 owner mismatch is a security decision — audit it.
            _log_audit_context(
                request,
                tool_name,
                arguments,
                decision="deny",
                reason="continuation_owner_mismatch",
            )
            return _jsonrpc_success(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": (
                                        "Continuation token does not belong to this"
                                        " caller / tier / tool.  Re-invoke without"
                                        " continuation_token to start a new negotiation."
                                    )
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        # ADR-011 §4: SEC-3 answers *who minted this token*; it does not answer
        # *may it be served here, now*.  Route containment is an authorization
        # check at use time, deliberately NOT an owner-key dimension — folding
        # it into the owner key would invalidate every outstanding token
        # whenever a route's configuration changed, and callers would
        # experience that mass invalidation as ordinary expiry.
        #
        # Evaluated against the CURRENT route surface, so a mount change takes
        # effect on tokens already outstanding without invalidating them.
        _target: str = cached.get("resolved_target", "")
        # Class dispatchers resolve no child, so containment alone would
        # re-authorize the navigation entry-point.  When an action was recorded
        # at mint, the action lens has to agree as well — and the action comes
        # from the cache entry, never from this call's arguments, which are
        # caller-supplied.
        _action: str | None = cached.get("resolved_action")
        _action_ok = _action is None or _redemption_action_authorized(request, tool_name, _action)
        if (
            not _target
            or not _action_ok
            or not _redemption_target_authorized(request, tool_name, _target)
        ):
            logger.warning(
                "heavy_continuation_route_not_authorized",
                extra={"tool": tool_name, "target": _target or "<unrecorded>"},
            )
            _log_audit_context(
                request,
                tool_name,
                arguments,
                decision="deny",
                # §5: an entry minted before the shape change has no resolved
                # target, so it cannot be authorized and is refused rather than
                # served on trust — same posture as the pre-SEC-3 legacy entries
                # above, and visible as a refusal wave across the deploy.
                reason=(
                    "continuation_route_not_authorized"
                    if _target
                    else "continuation_target_unrecorded"
                ),
            )
            return _continuation_refused(request_id)
        # ADR-005 item (b), ruled B2: a bare continuation_token is bounded, not
        # complete.  `full` is still available and still returns everything —
        # it just has to be asked for, so an omitted or mistyped `mode` cannot
        # select the most expensive response by accident.
        _mode: str = arguments.get("mode", DEFAULT_NEGOTIATION_MODE)
        try:
            served = _serve_heavy_mode(
                cached["result"],
                _mode,
                arguments,
                # Absent on entries minted before this shipped, and on every
                # read entry; `.get` keeps both on the existing behaviour.
                single_object=bool(cached.get("single_object", False)),
            )
        except ToolInputError as exc:
            _log_audit_context(
                request, tool_name, arguments, decision="deny", reason="continuation_bad_mode"
            )
            return _jsonrpc_success(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True,
                },
            )
        # DOC-7: serving a cached heavy result is an allowed, audit-worthy call.
        _log_audit_context(
            request, tool_name, arguments, decision="allow", reason="continuation_served"
        )
        return _usage_success(
            request, request_id, served, tool_name=tool_name, usage_args=_usage_args
        )

    # Record last_seen_at for this agent connection (fire-and-forget UPDATE).
    if conn is not None:
        from frisian_mcp.contrib.agents.models import (  # pylint: disable=import-outside-toplevel
            AgentConnection,
        )

        AgentConnection.objects.filter(pk=conn.pk).update(last_seen_at=timezone.now())

    # Ensure permission context is cached on the request before dispatch so that
    # dispatcher action="help" calls can apply Django permission filtering without
    # needing to re-resolve capabilities (avoids a second DB/cache round-trip).
    _ensure_perm_context_on_request(request)

    # Write-path filtering: strip the `verify` negotiation flag before dispatching
    # to the invocation backend.  The ViewSet serializer must never see it — it
    # is a frisian-mcp protocol param, not a model field.
    _write_entry = tool_registry.get_entry(tool_name)
    _verify = False
    _dispatch_arguments = arguments
    if _write_entry is not None and _write_entry.is_write:
        _verify = bool(arguments.get("verify", False))
        if "verify" in arguments:
            _dispatch_arguments = {k: v for k, v in arguments.items() if k != "verify"}

    # DOC-7 audit-context seam for the DISPATCH path: the permission decision
    # defaults to allow and is downgraded by the deny branches below; the
    # ``finally`` emits exactly one record per call that reaches this try.  The
    # pre-dispatch decisions above (agent inactive / not-allowed, continuation
    # expired / owner-mismatch / served) return before this try and each emit
    # their own record, so every resolved call is audited exactly once.
    _audit: dict[str, Any] = {"decision": "allow", "reason": None}
    try:
        result = build_middleware_chain(_tool_registry_dispatch, get_middleware_instances())(
            request, tool_name, _dispatch_arguments
        )
    except ToolNotFoundError as exc:
        _audit.update(decision="deny", reason="absent")
        # JSON-RPC 2.0: -32601 METHOD_NOT_FOUND is the correct code for an unknown
        # tool name.  -32602 INVALID_PARAMS is reserved for structural argument
        # errors; using it for a missing tool misleads clients into thinking their
        # call format is wrong rather than the tool name.
        #
        # Append close-match suggestions so agents can self-correct without an
        # extra tools/list round-trip.  The full tool list is intentionally
        # omitted — listing all names in the error leaks the discovery surface
        # to callers who have not made an explicit tools/list call.
        # On a per-route mount, enumerate the route's own deny-carved view so a
        # denied tool is never named back in a suggestion — the absence error
        # this branch wraps would otherwise undo itself one line later (WI-1).
        _rv: RouteView | None = getattr(request, "_mcp_route_view", None)
        _known_lister = _rv.list_tools if _rv is not None else tool_registry.list_tools
        # Apply the SAME capability filter tools/list uses (WI-1): under
        # FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY a near-miss suggestion must not
        # name a tool the caller lacks Django permission to see — that would
        # undo the "do not appear at any tier" guarantee one line after the
        # absence error that wraps it.  The route/tier lens is already applied
        # via the route view + max_tier; this adds the per-user capability lens.
        _perm_filter = getattr(request, "_mcp_perm_entry_filter", None)
        # The ACTION lens has to come too, not just the entry lens.  A dispatcher
        # whose every action the caller lacks permission for still passes
        # ``entry_filter`` — the entry itself is visible, it is the actions that
        # are not — so it stayed in ``known_names`` and a near-match handed its
        # name back inside the very error that denies it exists.  ``tools/list``
        # drops that dispatcher because the empty action enum makes it an empty
        # shell; passing the same factory here makes the two agree.  Same
        # disagreement class as H3's four consumers: one lens applied in one
        # consumer is not the lens applied.
        _caps = getattr(request, "_mcp_capabilities", None)
        _action_factory = _make_perm_action_filter_factory(_caps) if _caps is not None else None
        known_names = [
            t["name"]
            for t in _known_lister(
                max_tier=_get_token_permission(request),
                entry_filter=_perm_filter,
                action_filter_factory=_action_factory,
            )
        ]
        suggestions = difflib.get_close_matches(tool_name, known_names, n=3, cutoff=0.6)
        data = str(exc)
        if suggestions:
            data += f". Did you mean: {', '.join(suggestions)}?"
        data += _REFRESH_HINT
        # Lite-mode escape hatch is a no-op here BY CONSTRUCTION (WI-1): the
        # tool is absent for this request — never registered, route-denied, or
        # hidden by the effective tier — and _request_visible_entry resolves
        # the entry through the same route/tier lens, so there is no schema to
        # re-include.  A global-registry lookup here would hand a route-denied
        # tool's full input contract back inside its own absence error.  The
        # close-match suggestions in ``data`` are the agent's recovery path.
        return _lite_enrich_error(
            _jsonrpc_error(request_id, METHOD_NOT_FOUND, "Unknown tool", data),
            tool_name,
            _lite,
            request,
        )
    except LookupError as exc:
        # LookupError from inside a registered tool (e.g. group dispatcher raises
        # LookupError for an unknown resource/action pair).  Distinct from the
        # tool-not-found case above: the MCP tool exists, but the sub-action does
        # not.  Surface as isError:true so the agent can self-correct.
        _audit.update(decision="deny", reason="absent")
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 404}, tool_name, _lite, request
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except ToolInputError as exc:
        # MCP 2025-11-25 (`server/tools.mdx`) splits error reporting in two:
        # protocol errors for unknown tools, malformed requests and server
        # errors; tool results with `isError: true` for input validation.  A
        # failure against the tool's own `inputSchema` is the second kind.
        # "Malformed request" is about the CallToolRequest ENVELOPE — a missing
        # `name`, or `arguments` that is not an object, both still rejected as
        # protocol errors above — not a missing required field inside
        # `arguments`.
        #
        # This path used to answer with `_jsonrpc_error(INVALID_PARAMS,
        # "Invalid arguments", str(exc))`, where `message` is a constant and the
        # field name is the `data` argument.  Clients deliver `data` as null, so
        # the agent was told only "Invalid arguments" and could not self-correct;
        # a bad *action* enum on the same client and route displayed in full
        # because its text travels in the payload.  Using the wrong mechanism is
        # the defect and the unreadable `data` is its consequence, so the fix is
        # to move the mechanism rather than to fold the detail into `message`.
        #
        # This also makes the path consistent with every sibling dispatcher
        # error — unknown action, missing resource, omitted action — which
        # already report as tool results.
        #
        # `_lite_enrich_error_content` is the `isError` counterpart of
        # `_lite_enrich_error`; routing through it keeps one helper per shape
        # rather than adding a third.
        #
        # ``arguments`` is passed here and at no other enrich site: this is the
        # one failure that is against the MEMBER tool's schema, so it is the one
        # where echoing the dispatcher's schema would answer the wrong question.
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 400}, tool_name, _lite, request, arguments
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except ToolInvocationError as exc:
        # Backend returned ToolResult.is_error=True (non-DRF exception in the ViewSet).
        # Forward the actual error content so the agent receives actionable feedback
        # instead of the generic "Internal tool error" fallback.
        content = exc.content if isinstance(exc.content, dict) else {"error": str(exc.content)}
        if "status_code" not in content:
            content = {**content, "status_code": 500}
        content = _lite_enrich_error_content(content, tool_name, _lite, request)
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except PermissionError as exc:
        # Return as isError=True tool-level content, not a JSON-RPC protocol error.
        # INVALID_PARAMS (-32602) is reserved for argument structure failures; using
        # it for auth denial misleads agents into thinking their call format is wrong.
        _audit.update(decision="deny", reason="permission")
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 403}, tool_name, _lite, request
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except DRFValidationError as exc:
        # IT-8: Surface DRF field-level validation errors with structured detail so
        # the caller can display per-field messages without parsing a flat string.
        content = _build_drf_error_content(exc)
        content = _lite_enrich_error_content(content, tool_name, _lite, request)
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except DjangoValidationError as exc:
        # Surface Django model/form validation errors as structured isError=True content
        # so agents receive actionable feedback in the same format as DRFValidationError.
        msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        content = _lite_enrich_error_content(
            {"error": msg, "status_code": 400}, tool_name, _lite, request
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except ValueError as exc:
        # IT-3: Surface ValueError raised by @mcp_tool handlers — the convention is to
        # raise ValueError for user-correctable input problems (e.g. invalid UUID, bad
        # enum value).  Return as a tool-level isError response so the caller gets
        # actionable feedback without a full JSON-RPC error.
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 400}, tool_name, _lite, request
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("tool_execution_error", extra={"tool": tool_name, "error": str(exc)})
        content = _lite_enrich_error_content(
            {
                "error": (
                    str(exc)
                    if getattr(settings, "FRISIAN_MCP_EXPOSE_ERRORS", settings.DEBUG)
                    else "Internal tool error"
                ),
                "status_code": 500,
            },
            tool_name,
            _lite,
            request,
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    finally:
        # Runs on every exit from the try/except construct — the deny branches
        # above return from inside their handlers, and the success path falls
        # through to the result shaping below.  Exactly one record per call.
        _log_audit_context(request, tool_name, arguments, **_audit)

    # Unwrap ToolResult from DRF-backed tools so the write-path lean envelope
    # can access the actual HTTP status (201 for creates, 204 for deletes, etc.).
    # Custom @mcp_tool callables return plain Python objects, not ToolResult, so
    # the isinstance check is a no-op for those paths.
    _http_status: int = 200
    if isinstance(result, ToolResult):
        _http_status = result.http_status
        result = result.content

    # Lite-mode success post-processing: strip instructional scaffolding
    # (hints map, navigation strings, dispatcher action descriptions) from
    # any successful response when the caller passed ``lite: true``.
    # _strip_lite_scaffolding is safe to call on all result shapes — it only
    # removes known scaffolding patterns, never operation data.  The
    # is_dispatcher guard was removed because it caused silent no-ops when
    # host apps omit that registration flag; the function itself is the guard.
    if _lite:
        result = _strip_lite_scaffolding(result)

    # Write-path lean default: cache the full result and return a compact
    # confirmation envelope unless the caller passed verify=True.  @mcp_heavy
    # takes precedence when a tool is decorated with both.
    if (
        _write_entry is not None
        and _write_entry.is_write
        and not (_write_entry is not None and _write_entry.is_heavy)
    ):
        if _verify:
            return _usage_success(
                request, request_id, result, tool_name=tool_name, usage_args=_usage_args
            )
        from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
            _extract_lean_envelope,
        )

        _w_token = secrets.token_urlsafe(16)
        _lean = _extract_lean_envelope(result, _w_token, _http_status, tool_name=tool_name)
        if "continuation_token" in _lean:
            _hc = _heavy_cache()
            # Two ways the token cannot be redeemed, one outcome: return the
            # write result whole rather than a lean envelope advertising a
            # token nothing can redeem.
            #
            #   _hc is None                 no continuation store to redeem from
            #   schema does not disclose    the caller cannot legally send it back
            #
            # CR-9: the second is the same condition as the first and was
            # missing.  This path minted on ``is_write`` alone, and ``is_write``
            # is set only by auto-discovery, so the affected population is
            # auto-discovered ``*_create``/``*_update``/``*_partial_update`` —
            # the production-dominant write path.
            #
            # It is worse here than the read-side defect the mint gate was
            # written for.  There an undisclosed token was merely redundant: the
            # payload came back anyway.  Here ``_extract_lean_envelope`` returns
            # id + status_code + data_size + token and NOTHING else, so the
            # written object is reachable ONLY by redeeming.  A schema-validating
            # client cannot send an undeclared field, so it does not lose a
            # negotiation nicety — it loses the write result, and pays for a
            # pinned entry in the shared default cache to do it.
            #
            # The outer entry's schema is authoritative, matching the size
            # backstop: it is the schema this caller validated against.
            #
            # CL-9: the token is the only unusable part of the envelope, so it
            # is the only part dropped.  CR-9 returned the FULL result here,
            # which cost a 60-item bulk create 25 -> 7,143 tokens; the envelope
            # itself is ordinary data and the object stays reachable by
            # ``verify=True`` (injected into every auto-discovered write schema)
            # or a ``retrieve`` on the id it carries.
            #
            # CR-9's gate is unchanged and still governs whether we MINT — this
            # decides only what is returned instead.  Nothing is cached on this
            # arm, so no entry is pinned for a token nobody can send back.
            #
            # ``_lean_envelope_without_token`` returns None when the envelope
            # would not let the caller reach what was written (a bulk result
            # with no ids, or a single object with no id).  Then, and only then,
            # the full result is still the right answer — a larger response
            # beats a confirmation the caller cannot act on.
            if _hc is None or not schema_discloses_continuation(
                getattr(_write_entry, "input_schema", None)
            ):
                from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
                    _lean_envelope_without_token,
                )

                _tokenless = _lean_envelope_without_token(result, _http_status, tool_name=tool_name)
                return _usage_success(
                    request,
                    request_id,
                    result if _tokenless is None else _tokenless,
                    tool_name=tool_name,
                    usage_args=_usage_args,
                )
            _hc.set(
                f"{_HEAVY_CACHE_PREFIX}{_w_token}",
                # A write result is the object that was just written, not a
                # list envelope — say so, so redemption does not mistake one of
                # its fields for a paginated payload.
                _build_heavy_cache_entry(result, request, tool_name, single_object=True),
                _heavy_cache_ttl(),
            )
        elif _lean.get("deleted") is True:
            # Delete: enrich with the pk from original arguments.
            pk_val = arguments.get("pk") or arguments.get("id")
            if pk_val is not None:
                _lean["id"] = pk_val
        return _usage_success(
            request, request_id, _lean, tool_name=tool_name, usage_args=_usage_args
        )

    # Dispatcher-routed write: a group dispatcher routed to a write-tier
    # underlying tool.  Apply the same lean/verify logic as the flat write
    # path.  `verify` was stripped from params by make_group_invoke before
    # the underlying tool ran, so we read it from the original arguments here.
    if _write_entry is not None and _write_entry.is_dispatcher:
        _d_params: dict[str, Any] = arguments.get("params") or {}
        _d_entry = _dispatcher_target_entry(_write_entry, arguments)
        if _d_entry is not None and _d_entry.is_write and not _d_entry.is_heavy:
            _d_verify = bool(_d_params.get("verify", False))
            if _d_verify:
                return _usage_success(
                    request, request_id, result, tool_name=tool_name, usage_args=_usage_args
                )
            from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
                _extract_lean_envelope,
            )

            _w_token = secrets.token_urlsafe(16)
            # Pass the outer dispatcher tool name, matching the prior
            # frame-introspection behaviour.  (Resolving the underlying
            # ``_d_target`` serializer's light-key here would be a behaviour
            # change, not a refactor — left for a follow-up.)
            _d_lean = _extract_lean_envelope(result, _w_token, _http_status, tool_name=tool_name)
            if "continuation_token" in _d_lean:
                _hc = _heavy_cache()
                if _hc is None:
                    # Same as the flat write path: no store, so no envelope
                    # promising a redemption that cannot happen.
                    return _usage_success(
                        request, request_id, result, tool_name=tool_name, usage_args=_usage_args
                    )
                _hc.set(
                    f"{_HEAVY_CACHE_PREFIX}{_w_token}",
                    # ADR-011 §5: bind the server-resolved child, not the outer
                    # dispatcher name, so redemption has something to re-authorize.
                    # single_object: as on the flat write path above.
                    _build_heavy_cache_entry(
                        result,
                        request,
                        tool_name,
                        getattr(_d_entry, "name", None),
                        single_object=True,
                    ),
                    _heavy_cache_ttl(),
                )
            elif _d_lean.get("deleted") is True:
                pk_val = _d_params.get("pk") or _d_params.get("id")
                if pk_val is not None:
                    _d_lean["id"] = pk_val
            return _usage_success(
                request, request_id, _d_lean, tool_name=tool_name, usage_args=_usage_args
            )

    # @mcp_heavy tools: cache the result and return a probe envelope so the agent
    # can choose how much of the response to retrieve on the follow-up call.
    #
    # R1: a dispatcher entry is never itself `is_heavy` — `@mcp_heavy` marks the
    # underlying flat tool — so resolving this flag on the outer name meant
    # `@mcp_heavy` never fired for a grouped call.  Only the size backstop below
    # negotiated, and hosts running AUTO_NEGOTIATE_THRESHOLD=None got no
    # negotiation at all: the same tool probed when called flat and returned its
    # full payload when called through a group.  Resolve through to the routed
    # entry, mirroring the write path above.
    _entry = tool_registry.get_entry(tool_name)
    _heavy_entry = (
        _dispatcher_target_entry(_entry, arguments)
        if _entry is not None and _entry.is_dispatcher
        else _entry
    )
    # A missing continuation store disables negotiation rather than
    # relocating it: the full response is returned instead of a probe
    # advertising a token that was never stored.
    _hc = _heavy_cache()
    if _heavy_entry is not None and _heavy_entry.is_heavy and _hc is not None:
        _token = secrets.token_urlsafe(16)
        # SEC-3: bind the cache entry to the current caller so a leaked
        # continuation_token cannot be replayed by a different agent.
        #
        # G1: `tool_name` here is the OUTER tool — deliberately, and it must
        # stay that way.  The line above changes which entry *decides* whether
        # to negotiate; it must not change which *name* is bound.  Redemption
        # (`_heavy_owner_key(request, tool_name)` on the continuation path)
        # knows only the outer name — it never sees `resource`/`action` — so
        # passing the resolved inner name here "for consistency" would bind
        # mint to the inner name while redeem stays outer, and every grouped
        # redemption would fail owner-mismatch.  That is the unredeemable-token
        # bug this change exists to fix, reintroduced from the other side.
        _hc.set(
            f"{_HEAVY_CACHE_PREFIX}{_token}",
            # ADR-011 §5: bind the server-resolved child, not the outer
            # dispatcher name, so redemption has something to re-authorize.
            _build_heavy_cache_entry(
                result, request, tool_name, getattr(_heavy_entry, "name", None)
            ),
            _heavy_cache_ttl(),
        )
        probe = _build_probe_envelope(result, _token)
        return _usage_success(
            request, request_id, probe, tool_name=tool_name, usage_args=_usage_args
        )

    # Threshold backstop (secondary, v2): auto-negotiate an over-threshold response from
    # any tool whose published schema DISCLOSES the continuation call — @mcp_heavy and the
    # dispatchers.  A non-disclosing tool is returned whole no matter its size (CR-2); see
    # the disclosure gate below.  Prefer @mcp_heavy for explicit control.
    # Defaults to _DEFAULT_AUTO_NEGOTIATE_THRESHOLD (on) so high-cardinality lists on those
    # shapes probe first without per-host config; an explicit None in settings disables the
    # backstop entirely.
    _threshold: int | None = getattr(
        settings, "FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD", _DEFAULT_AUTO_NEGOTIATE_THRESHOLD
    )
    #
    # H2: size alone is not sufficient grounds to mint.  The token is only
    # worth issuing if the caller can legally send it back, and that is decided
    # by the schema they were handed in `tools/list` — so the gate reads that
    # same schema rather than a flag recorded beside it.  Disclosure and mint
    # eligibility are therefore one fact read twice and cannot drift apart,
    # which is how tokens came to be minted for shapes no schema-validating
    # caller could return.
    #
    # The OUTER entry's schema is authoritative, deliberately.  `_heavy_entry`
    # above resolves inward to decide *whether* a routed tool negotiates; this
    # decides whether the *caller* can reply, and the caller validated against
    # the outer tool — the same reason the owner key binds the outer name (G1).
    if (
        _threshold is not None
        and _hc is not None
        and schema_discloses_continuation(getattr(_entry, "input_schema", None))
    ):
        _serialized = json.dumps(result)
        if len(_serialized.encode()) > _threshold:
            _token = secrets.token_urlsafe(16)
            _hc.set(
                f"{_HEAVY_CACHE_PREFIX}{_token}",
                # ADR-011 §5: the outer name governs schema disclosure and the
                # owner key (both above); route containment is a third fact and
                # takes the server-resolved child.  Binding the outer name here
                # would make §4's membership re-check trivially pass for every
                # grouped token minted through this backstop.
                #
                # A class dispatcher resolves no child at all, so it records the
                # dispatched *action* instead — otherwise the re-check
                # authorizes the navigation entry-point rather than the thing
                # that produced the payload.  This backstop is the only mint
                # path a class dispatcher reaches.
                _build_heavy_cache_entry(
                    result,
                    request,
                    tool_name,
                    getattr(_heavy_entry, "name", None),
                    _dispatched_action(_entry, arguments),
                ),
                _heavy_cache_ttl(),
            )
            probe = _build_probe_envelope(result, _token)
            return _usage_success(
                request, request_id, probe, tool_name=tool_name, usage_args=_usage_args
            )

    return _usage_success(request, request_id, result, tool_name=tool_name, usage_args=_usage_args)


def _handle_resources_list(request_id: JsonRpcId, request: Any) -> JsonResponse:
    """Handle ``resources/list`` — return all registered resources."""
    return _jsonrpc_success(request_id, {"resources": resource_registry.list_resources(request)})


def _handle_resources_read(request_id: JsonRpcId, params: JsonDict, request: Any) -> JsonResponse:
    """Handle ``resources/read`` — dispatch to a registered resource handler."""
    uri: Any = params.get("uri")
    if not uri or not isinstance(uri, str):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid params", "'uri' is required")

    try:
        text = resource_registry.read_resource(uri, request)
    except ResourceNotFoundError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Resource not found: {uri}", str(exc))

    defn = resource_registry.get_definition(uri)
    mime_type = defn.mime_type if defn is not None else "text/plain"

    return _jsonrpc_success(
        request_id,
        {"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]},
    )


def _handle_help(request_id: JsonRpcId) -> JsonResponse:
    """
    Handle ``help`` — return server metadata and usage hints for AI agents.

    Returns a structured summary of available methods, error formats, and
    navigation tips so that agents can self-orient without out-of-band
    documentation.
    """
    server_name: str = getattr(settings, "FRISIAN_MCP_SERVER_NAME", "frisian-mcp")
    return _jsonrpc_success(
        request_id,
        {
            "server": server_name,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "methods": [
                "initialize",
                "initialized",
                "tools/list",
                "tools/call",
                "resources/list",
                "ping",
                "help",
            ],
            "hints": {
                "discovery": (
                    "Call tools/list to enumerate available tools and their inputSchema."
                ),
                "invocation": ("Call tools/call with {name, arguments} to invoke a tool."),
                "errors": (
                    "Tool errors return isError=true with content[0].text as JSON. "
                    "Check the 'error' key for the message and 'detail' for field-level hints."
                ),
                "unknown_tool": (
                    "If tools/call returns -32601, the tool name is unrecognised. "
                    "Re-run tools/list for the correct name — suggestions are included in "
                    "the error data field."
                ),
            },
        },
    )


def _parse_and_dispatch(  # pylint: disable=too-many-branches
    request: HttpRequest | DRFRequest,
) -> JsonResponse | HttpResponse:
    """Parse the POST body and dispatch to the appropriate method handler."""
    # DRF wraps the Django HttpRequest in a rest_framework.request.Request.
    # _parse_and_dispatch only needs request.body and request.user — both are
    # proxied transparently by the DRF wrapper, so no unwrapping is needed.
    try:
        body: Any = json.loads(request.body)
    except json.JSONDecodeError as exc:
        return _jsonrpc_error(None, -32700, "Parse error", str(exc))

    if not isinstance(body, dict):
        return _jsonrpc_error(None, INVALID_REQUEST, "Invalid Request", "expected a JSON object")
    if body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(None, INVALID_REQUEST, "jsonrpc must be '2.0'")

    request_id: JsonRpcId = body.get("id")
    method: Any = body.get("method", "")
    params: Any = body.get("params") or {}

    if not isinstance(method, str):
        return _jsonrpc_error(request_id, INVALID_REQUEST, "'method' must be a string")
    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "'params' must be an object")

    # MCP Streamable HTTP (2025-03-26) §transport: when a POST body contains only
    # JSON-RPC *notifications* (messages without an ``id`` field), the server MUST
    # return HTTP 202 Accepted with no body.  Notifications have no ``id`` key at all
    # (distinct from an explicit ``"id": null`` on a request).
    is_notification = "id" not in body
    if is_notification:
        if method == "initialized":
            logger.info("mcp_initialized")
        else:
            logger.debug("mcp_notification", extra={"method": method})
        return HttpResponse(status=202)

    logger.debug("mcp_request", extra={"method": method, "request_id": request_id})

    if method == "ping":
        return _jsonrpc_success(request_id, {})
    if method == "initialize":
        return _handle_initialize(request_id, params)
    if method == "initialized":
        return _handle_initialized(request_id)
    if method == "tools/list":
        return _handle_tools_list(request_id, request, params)
    if method == "tools/call":
        return _handle_tools_call(request, request_id, params)
    if method == "resources/list":
        return _handle_resources_list(request_id, request)
    if method == "resources/read":
        return _handle_resources_read(request_id, params, request)
    if method == "help":
        return _handle_help(request_id)
    return _jsonrpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# SSE renderer — lets DRF content negotiation accept text/event-stream
# ---------------------------------------------------------------------------


class _EventStreamRenderer(BaseRenderer):
    """Passthrough renderer that satisfies DRF content negotiation for SSE."""

    media_type = "text/event-stream"
    format = "event-stream"

    def render(
        self, data: Any, accepted_media_type: str | None = None, renderer_context: Any = None
    ) -> Any:
        return data


# ---------------------------------------------------------------------------
# Main endpoint — DRF APIView
# ---------------------------------------------------------------------------


class McpView(APIView):
    """
    MCP gateway — single HTTP POST endpoint for all JSON-RPC 2.0 traffic.

    ``renderer_classes`` includes :class:`_EventStreamRenderer` so that DRF
    content negotiation accepts ``Accept: text/event-stream`` requests without
    raising HTTP 406.  The actual SSE wrapping is handled by :func:`_maybe_sse`;
    the renderer's ``render`` method is never invoked because ``post`` returns
    a raw :class:`~django.http.StreamingHttpResponse` that bypasses DRF rendering.

    Extends DRF :class:`~rest_framework.views.APIView` so that host projects
    can apply standard DRF authentication and permission classes to the MCP
    surface without requiring custom middleware.

    Configuration (all optional)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``FRISIAN_MCP_AUTHENTICATION_CLASSES``
        List of dotted-path strings or class objects.  When absent, DRF's
        ``DEFAULT_AUTHENTICATION_CLASSES`` are used.

    ``FRISIAN_MCP_PERMISSION_CLASSES``
        List of dotted-path strings or class objects.  Defaults to ``[]``
        (no gateway-level permission check) to preserve backwards
        compatibility.  Individual tools still enforce their own
        ``permission_classes`` via :data:`~frisian_mcp.registry.tool_registry`.

    Example (JWT-gated MCP surface)::

        # settings.py
        FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
        ]
        FRISIAN_MCP_PERMISSION_CLASSES = [
            "rest_framework.permissions.IsAuthenticated",
        ]

    """

    renderer_classes = [JSONRenderer, _EventStreamRenderer]

    #: Set on the per-route subclasses that ``apps._install_route_urls`` mounts
    #: when ``FRISIAN_MCP_ROUTES`` is configured.  ``None`` on plain mounts —
    #: the legacy gateway, extra paths, and the protected path — which keep the
    #: global-registry behaviour byte-identical to today.
    _route_name: str | None = None
    #: The parsed :class:`~frisian_mcp.route_config.RouteConfig` backing a
    #: per-route mount; carried on the subclass so the tier ceiling and
    #: permission classes stay enforceable even before the route's view
    #: snapshot has been materialised.
    _route_config: Any = None

    def _resolve_route_view(self) -> Any:
        """
        Return this mount's :class:`~frisian_mcp.route_views.RouteView`, or ``None``.

        Plain mounts return ``None`` (global-registry path).  A per-route mount
        resolves its snapshot from the process-scoped registry; if the snapshot
        is missing (e.g. ``FRISIAN_MCP_AUTODISCOVER = False``, where deferred
        discovery — the normal rebuild trigger — never runs) it is built now and
        swapped in atomically.  Falling back to the *global* registry instead
        would silently drop the route's allow/deny carve-out — fail-open — so
        the fallback here builds the carved view rather than bypassing it.
        """
        if self._route_name is None:
            return None
        view = route_views.get(self._route_name)
        if view is None:
            if self._route_config is None:
                # A per-route mount with a name but no config can neither resolve
                # nor rebuild its carved view.  Returning None here would fail
                # OPEN: every caller treats None as "plain mount → use the
                # unfiltered global registry", silently serving the full tool
                # surface on what is meant to be a restricted route.  Fail loud
                # instead — apps.py always sets _route_name and _route_config
                # together, so reaching this is a construction bug, not config.
                raise ImproperlyConfigured(
                    f"Route {self._route_name!r} is mounted without a route config; "
                    "cannot resolve its allow/deny view, and refusing to fall back "
                    "to the unfiltered global registry."
                )
            view = route_views.rebuild(self._route_config)
        return view

    def _effective_max_tier(self) -> str | None:
        """
        Return the tier cap for this endpoint, or ``None`` for no cap.

        Reads ``FRISIAN_MCP_MAX_TIER`` from settings.  Override in a subclass
        to pin a different cap (or ``None`` to disable it) without touching
        global settings — the auto-registered protected endpoint does exactly
        this so that authenticated callers receive their full tier there.

        Normalised through the same helper the ``E010`` startup check uses, so
        the check cannot bless a value the runtime then rejects.  An
        unrecognised value stays unrecognised — it is **not** coerced to a tier
        — so it still fails closed; what changes is that ``"  READ_WRITE  "``
        now means what the operator plainly intended instead of denying every
        privileged caller while the check reported the config clean.
        """
        return normalize_tier_setting(getattr(settings, "FRISIAN_MCP_MAX_TIER", None)) or getattr(
            settings, "FRISIAN_MCP_MAX_TIER", None
        )

    def get_authenticators(self) -> list[Any]:
        """
        Return authenticator instances for this view.

        Reads ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` from settings.  When the
        setting is absent, delegates to the DRF default.
        """
        classes = _resolve_classes("FRISIAN_MCP_AUTHENTICATION_CLASSES")
        if classes is None:
            return super().get_authenticators()
        return [cls() for cls in classes]

    def get_permissions(self) -> list[Any]:
        """
        Return permission instances for this view.

        Reads ``FRISIAN_MCP_PERMISSION_CLASSES`` from settings.  Defaults to
        ``[]`` when the setting is absent (backward compatible — no gateway
        permission check; tool-level permissions still apply).
        """
        classes = _resolve_classes("FRISIAN_MCP_PERMISSION_CLASSES")
        if classes is None:
            return []
        return [cls() for cls in classes]

    def get(
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> StreamingHttpResponse | HttpResponse:
        """
        Handle GET — open an SSE keepalive channel per MCP Streamable HTTP spec.

        When ``FRISIAN_MCP_SSE_CHANNEL`` is ``False``, the server does not
        support server-initiated messages and returns HTTP 405 so MCP clients
        fall back to receiving responses in the POST response body.  Use this
        for stateless deployments (e.g. multi-pod Kubernetes) that cannot route
        POST responses through a long-lived per-client SSE stream.

        When ``FRISIAN_MCP_SSE_CHANNEL`` is ``True`` (default), a keepalive
        comment is sent every 15 seconds (WSGI) or 3 seconds (ASGI) to prevent
        proxy and client timeouts.

        ``FRISIAN_MCP_SSE_MAX_STREAM_SECONDS`` (default ``300``) caps how long
        a single SSE connection is held open.  After the deadline the stream
        closes and the MCP client reconnects.  This bounds the number of WSGI
        worker threads pinned by long-lived keepalive connections.  Set to ``0``
        to close immediately after the first keepalive (useful in tests).
        """
        if not getattr(settings, "FRISIAN_MCP_SSE_CHANNEL", True):
            return HttpResponse(status=405)

        max_stream_seconds: int = getattr(settings, "FRISIAN_MCP_SSE_MAX_STREAM_SECONDS", 300)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # WSGI context — async generators cannot be iterated synchronously,
            # but a sync generator works fine: the worker thread blocks on
            # time.sleep() between keepalives while the HTTP chunked stream stays
            # open. This is preferable to returning 405 because some clients
            # (e.g. Cursor) do not fall back to POST-only on 405 and instead
            # treat it as a hard connection failure even though POST calls work.
            #
            # FRISIAN_MCP_SSE_MAX_STREAM_SECONDS caps the worker hold time (default
            # 300 s).  After the deadline the stream closes cleanly; the client
            # reconnects.  Set to 0 to yield one keepalive and close immediately.
            #
            # ONE-TIME WARNING: sync WSGI workers cannot scale this pattern — each
            # MCP client pins one worker for up to max_stream_seconds, so the
            # (N+1)th concurrent connection starves on a pool of N workers.  The
            # warning fires once per process lifetime so operators see it without
            # the log getting flooded.
            global _SSE_WSGI_WARNED  # pylint: disable=global-statement
            if not _SSE_WSGI_WARNED:
                _SSE_WSGI_WARNED = True
                _msg = (
                    "frisian_mcp: SSE keepalive served from a sync WSGI worker. "
                    "Each MCP client connection pins one worker for up to "
                    "FRISIAN_MCP_SSE_MAX_STREAM_SECONDS=%s seconds; concurrent "
                    "connection count >= worker count will starve the pool and "
                    "manifest as WORKER TIMEOUT loops.  Switch to an ASGI worker "
                    "class — e.g. 'gunicorn config.asgi:application -k "
                    "uvicorn.workers.UvicornWorker' or 'uvicorn config.asgi:"
                    "application'.  Bumping --timeout only delays the symptom."
                )
                logger.warning(_msg, max_stream_seconds)
                print(  # noqa: T201 — one-time loud signal for misconfigured deployment
                    "[frisian-mcp] WARNING: SSE keepalive running on a sync WSGI "
                    "worker; switch to an ASGI worker class (uvicorn workers) to "
                    "avoid worker-pool starvation. See log for details.",
                    flush=True,
                )

            def _wsgi_keepalive() -> Generator[str, None, None]:
                deadline = time.monotonic() + max_stream_seconds
                while True:
                    yield ": keepalive\n\n"
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(15.0, remaining))

            resp = StreamingHttpResponse(_wsgi_keepalive(), content_type="text/event-stream")
            resp["Cache-Control"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            return resp

        async def _keepalive_stream() -> AsyncGenerator[str, None]:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + max_stream_seconds
            while True:
                yield ": keepalive\n\n"
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(3.0, remaining))

        resp = StreamingHttpResponse(_keepalive_stream(), content_type="text/event-stream")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

    def post(
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse | HttpResponse | StreamingHttpResponse:
        """Handle POST — dispatch JSON-RPC 2.0 requests."""
        # Stamp the endpoint-level tier cap so _get_token_permission can apply
        # it throughout the request without re-reading settings on each call.
        # On a per-route mount this is min(route ceiling, FRISIAN_MCP_MAX_TIER)
        # via the subclass's _effective_max_tier override.
        request._mcp_max_tier = self._effective_max_tier()  # type: ignore[attr-defined]  # pylint: disable=protected-access
        # Stamp the route view (None on plain mounts) so tools/list, tools/call,
        # and the not-found suggester all read the same deny-carved surface.
        request._mcp_route_view = self._resolve_route_view()  # type: ignore[attr-defined]  # pylint: disable=protected-access
        # ADR-010 §8: the effective tier — min(token_tier, route_ceiling,
        # FRISIAN_MCP_MAX_TIER) — is computed exactly once per request, here,
        # and stamped.  _resolve_request_tier short-circuits on the stamp, so
        # every later read (discovery, dispatch-time enforcement, suggesters,
        # error paths) returns this one value; nothing recomputes it.  The
        # stamp must land AFTER _mcp_max_tier so the cap participates in the
        # resolution it is meant to bound.
        request._mcp_effective_tier = _get_token_permission(request)  # type: ignore[attr-defined]  # pylint: disable=protected-access
        if not getattr(settings, "FRISIAN_MCP_ENABLED", True):
            return _maybe_sse(
                JsonResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": INTERNAL_ERROR, "message": "MCP gateway is disabled"},
                    },
                    status=503,
                ),
                request,
            )
        # Enforce a request-body size limit.  Django's DATA_UPLOAD_MAX_MEMORY_SIZE
        # only applies to multipart/form-encoded bodies; raw JSON POST bodies are
        # unbounded by default.  FRISIAN_MCP_REQUEST_BODY_MAX_SIZE (bytes, default
        # 1 MiB) protects against oversized payloads being loaded into memory by
        # json.loads() in _parse_and_dispatch.
        max_body: int = getattr(settings, "FRISIAN_MCP_REQUEST_BODY_MAX_SIZE", 1 * 1024 * 1024)
        if len(request.body) > max_body:
            return _maybe_sse(
                _jsonrpc_error(
                    None,
                    INVALID_REQUEST,
                    "Request body too large",
                    f"Maximum allowed size is {max_body} bytes.",
                ),
                request,
            )
        return _maybe_sse(_parse_and_dispatch(request), request)

    def delete(self, request: DRFRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Handle DELETE — stateless no-op for agent session-cleanup calls."""
        return JsonResponse({}, status=200)

    def http_method_not_allowed(  # type: ignore[override]
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        """Return a JSON-RPC 2.0 error for non-POST methods."""
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": METHOD_NOT_FOUND, "message": "Method Not Allowed — POST only"},
            },
            status=405,
        )
