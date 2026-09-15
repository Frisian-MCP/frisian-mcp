"""
Startup configuration audit for the per-route permission surface.

The audit runs once, at boot, over ``settings.FRISIAN_MCP_ROUTES``.  It has
three severities, and the difference between them is *what happens to the
process*, not how loud the message is:

``FATAL``
    A :class:`~django.core.checks.Error` **and** an
    :class:`~django.core.exceptions.ImproperlyConfigured` raised from
    :meth:`~frisian_mcp.apps.FrisianMcpConfig.ready`.  Boot fails, the exit
    code is non-zero, and ``manage.py check`` surfaces it.  A banner printed
    by a container that keeps serving traffic is not a refusal — the process
    must actually stop.

``LOUD``
    A :class:`~django.core.checks.Warning` at boot.  The configuration is
    coherent but almost certainly not what the operator meant.

``SOFT``
    A :class:`~django.core.checks.Warning` at boot for a setting that is off
    its secure default, or a working carve-out worth a second look.

Why the audit is split across two phases
----------------------------------------

Auto-discovery is deferred to the first request (``apps.py`` connects
``_run_deferred_discovery`` to the ``request_started`` signal so that host
apps appended to ``INSTALLED_APPS`` after ours have run their own
``ready()``).  Django system checks run under ``manage.py check`` with no
request, so **the tool registry is empty while these checks execute**.

Every trigger evaluated at *check* time is therefore config-only: it reads
settings and never consults :data:`~frisian_mcp.registry.tool_registry`.  The
triggers that require a populated tool surface — net-empty exposure, a
carve-out that leaves survivors, an allow/deny entry that matches no tool, and
the cross-carve discoverability pair W017/W019 — cannot be evaluated here.
Evaluated against an empty registry they do not merely miss; they *invert*,
reporting an empty surface for every route.  Those land in the discovery-time
pass instead (see :func:`audit_route_surface`).

Note that the surface pass is no longer purely name-level.  W017/W019 read
per-field relation targets, resolved during discovery and looked up here by
tool name through :mod:`frisian_mcp._relations`.  This module still resolves
*nothing* from a serializer itself and touches no queryset — the enumeration
above said "three triggers" before GH #67 added the pair, and the sentence is
updated rather than left to imply the surface pass is name-only.

Legacy hosts
------------

When ``FRISIAN_MCP_ROUTES`` is absent the package mounts a single implicit
route with today's behaviour.  The audit is **silent** in that case: it only
speaks when an operator has opted into the per-route surface.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from django.core.checks import (  # pylint: disable=redefined-builtin
    CheckMessage,
    Error,
    Tags,
    Warning,
    register,
)
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:  # pragma: no cover
    from frisian_mcp.apps import FrisianMcpConfig
    from frisian_mcp.route_config import RouteConfig

logger = logging.getLogger(__name__)

__all__ = [
    "E004_OPEN_WORLD_DEFAULT_ABOVE_READ",
    "E005_ROUTE_SCHEMA",
    "E006_ANONYMOUS_GRANT_ON_PRIVILEGED",
    "W004_EMPTY_ALLOW_LIST",
    "W005_AUTO_REGISTER_ANONYMOUS",
    "W006_AUTO_DISCOVER_ENABLED",
    "W007_MAX_TIER_CAPS_ROUTE",
    "W008_NET_EXPOSURE_EMPTY",
    "W009_WORKING_CARVE_OUT",
    "W010_ANONYMOUS_SSE_REACHABLE",
    "W011_UNPROVABLE_PERMISSION_CLASS",
    "W017_REQUIRED_FK_UNREADABLE",
    "W019_CROSS_CARVE_UNEVALUATED",
    "CrossCarveReport",
    "audit_cross_carve_surface",
    "audit_route_configs",
    "audit_route_surface",
    "force_tool_discovery",
    "raise_on_fatal_route_config",
]

#: FATAL — an anonymously-reachable ``default`` route that declares a ceiling
#: above ``read``.  This is the trigger that actually guards the flagship
#: "open demo beside a hard-auth admin route" deployment.
E004_OPEN_WORLD_DEFAULT_ABOVE_READ = "frisian_mcp.E004"

#: FATAL — ``FRISIAN_MCP_ROUTES`` failed schema validation.  Wraps the
#: ``ImproperlyConfigured`` raised by :func:`~frisian_mcp.route_config.parse_route_configs`
#: so ``manage.py check`` reports it as a check rather than a traceback.
E005_ROUTE_SCHEMA = "frisian_mcp.E005"

#: FATAL — an ``elevated`` / ``admin`` route whose effective permission classes
#: include a literal ``AllowAny`` grant (or an unmodified subclass).  A route
#: named to be privileged that anonymous callers can POST to is incoherent; the
#: process refuses to boot rather than silently override the operator's grant.
#: (An *empty* class list on such a route resolves to ``[IsAuthenticated]`` — see
#: :func:`~frisian_mcp.route_views.route_effective_permission_classes` rule 3 —
#: so the empty case is structurally unreachable here; this fires only on an
#: explicit anonymous grant.)  WI-3.
E006_ANONYMOUS_GRANT_ON_PRIVILEGED = "frisian_mcp.E006"

#: LOUD — a configured route with an empty ``allow_list`` exposes nothing.
W004_EMPTY_ALLOW_LIST = "frisian_mcp.W004"

#: LOUD — client self-enrollment on a route anonymous callers can POST to.
W005_AUTO_REGISTER_ANONYMOUS = "frisian_mcp.W005"

#: SOFT — tool auto-discovery is on; newly-discovered tools join this route.
W006_AUTO_DISCOVER_ENABLED = "frisian_mcp.W006"

#: SOFT — a global ``FRISIAN_MCP_MAX_TIER`` caps a route below the ceiling it
#: declares, leaving the route quietly inert.
W007_MAX_TIER_CAPS_ROUTE = "frisian_mcp.W007"

#: LOUD — an anonymous caller can open this route's GET / SSE keepalive channel
#: (empty classes, ``AllowAny``, or ``IsAuthenticatedOrReadOnly``).  The POST
#: tool surface stays gated; the risk is a worker-pinning unauthenticated stream
#: held up to ``FRISIAN_MCP_SSE_MAX_STREAM_SECONDS``, i.e. resource exhaustion,
#: not disclosure.  Deliberately **not** conditioned on ``highest_tier`` —
#: lowering the ceiling does not close the SSE door.  Suppressed under
#: ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED`` (the acknowledged open demo), exactly as
#: W001 is.  WI-3 (R2).
W010_ANONYMOUS_SSE_REACHABLE = "frisian_mcp.W010"

#: LOUD — an ``elevated`` / ``admin`` route carries a permission class that
#: cannot be statically proven to require authentication (an opaque custom
#: class).  It is *not* fatal — it may enforce auth by means the audit cannot
#: read; "FATAL unless IsAuthenticated present" is forbidden because it
#: false-positives on legitimate custom auth.  WI-3 middle tier.
W011_UNPROVABLE_PERMISSION_CLASS = "frisian_mcp.W011"

#: LOUD — a non-empty ``allow_list`` is fully zeroed by ``deny_list``: the
#: route selected tools and then denied every one of them, so it exposes
#: nothing.  Surface-time only (needs the live registry), so it is emitted as a
#: log from :func:`audit_route_surface`, not as a Django check — see the module
#: docstring for why the registry is empty at check time.
W008_NET_EXPOSURE_EMPTY = "frisian_mcp.W008"

#: SOFT — a working carve-out: ``deny_list`` removed one or more tools and the
#: route still exposes others.  Almost always intentional; surfaced once so an
#: operator can confirm the removed set was the one they meant.  Surface-time
#: only, same as :data:`W008_NET_EXPOSURE_EMPTY`.
W009_WORKING_CARVE_OUT = "frisian_mcp.W009"

#: Maximum tools enumerated in a W008/W009 message before truncation.  A
#: truncated list that looks complete is its own failure mode, so the cap is
#: always made explicit with a ``(+N more)`` suffix.
_SURFACE_LIST_CAP = 5


def _routes_setting() -> Any:
    """Return the raw ``FRISIAN_MCP_ROUTES`` value, or ``None`` when unset."""
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    return getattr(settings, "FRISIAN_MCP_ROUTES", None)


def _route_is_anonymous_reachable(route: RouteConfig) -> bool:
    """
    Return ``True`` when an anonymous caller can POST to *route*'s tool surface.

    Thin adapter over the single authoritative definition in
    :mod:`frisian_mcp.route_views` — imported, never re-derived, so the audit
    and the runtime cannot disagree about which routes are anonymous.  (PR-9
    shipped a temporary fallback here for the window before ``route_views``
    existed; PR-9b removed it once PR-6 landed.)
    """
    from frisian_mcp.route_views import (  # pylint: disable=import-outside-toplevel
        route_is_anonymous_reachable,
    )

    return bool(route_is_anonymous_reachable(route))


def _tier_rank(tier: str | None) -> int | None:
    """Return the strict rank of *tier*, or ``None`` when *tier* is ``None``."""
    from frisian_mcp.route_config import (  # pylint: disable=import-outside-toplevel
        PERMISSION_TIER_RANK,
    )

    if tier is None:
        return None
    return PERMISSION_TIER_RANK.get(tier)


def _check_open_world_default(route: RouteConfig) -> list[CheckMessage]:
    """FATAL when an anonymously-reachable ``default`` route declares a ceiling above ``read``."""
    if route.name != "default":
        return []

    rank = _tier_rank(route.highest_tier)
    # ``None`` means the key was omitted.  PR-7 resolves an omitted ceiling to
    # the tier key's secure default (``default`` -> ``read``), which is never
    # above ``read``, so an omitted key cannot trigger this.
    if rank is None or rank <= 0:
        return []

    if not _route_is_anonymous_reachable(route):
        return []

    return [
        Error(
            f"FRISIAN_MCP_ROUTES['default'] declares highest_tier="
            f"{route.highest_tier!r} while the route does not require "
            "authentication, so unauthenticated callers reach this route's tool "
            "surface at a tier above 'read'.  An open route must not grant write "
            "or admin capability.",
            hint=(
                "Either lower FRISIAN_MCP_ROUTES['default']['highest_tier'] to "
                "'read', or set FRISIAN_MCP_PERMISSION_CLASSES to require "
                "authentication (e.g. "
                "['rest_framework.permissions.IsAuthenticated']).  A route "
                "intended for anonymous read-only access should declare "
                "highest_tier='read' explicitly."
            ),
            id=E004_OPEN_WORLD_DEFAULT_ABOVE_READ,
        )
    ]


def _check_allow_deny_grammar(route: RouteConfig) -> list[CheckMessage]:
    """FATAL on any grammar error; the code is reused verbatim as the check id."""
    from frisian_mcp.route_grammar import (  # pylint: disable=import-outside-toplevel
        GrammarError,
        parse_lists,
    )

    try:
        parse_lists(route.allow_list, route.deny_list, route_name=route.name)
    except GrammarError as exc:
        return [
            Error(
                str(exc),
                hint=(
                    "Allow/deny entries are 'group' or 'group:resource'.  The "
                    "only wildcard is a bare '*' in allow_list; 'group:*' "
                    "normalises to 'group'.  '*' is rejected in deny_list, and "
                    "'__' is rejected anywhere."
                ),
                id=f"frisian_mcp.{exc.code}",
            )
        ]
    return []


def _check_empty_allow_list(route: RouteConfig) -> list[CheckMessage]:
    """LOUD when a configured route exposes nothing because ``allow_list`` is empty."""
    if route.allow_list:
        return []
    return [
        Warning(
            f"FRISIAN_MCP_ROUTES[{route.name!r}] has an empty allow_list, so the "
            "route exposes no tools at all.  Route construction is deny-all by "
            "default; an empty allow_list is not a shorthand for 'everything'.",
            hint=(
                "Add the groups or tools this route should expose, or use "
                "allow_list=['*'] to expose the whole discovered surface.  "
                "Remove the route entirely if it is not meant to be mounted — "
                "a tier absent from FRISIAN_MCP_ROUTES is simply not mounted."
            ),
            id=W004_EMPTY_ALLOW_LIST,
        )
    ]


def _check_auto_register(route: RouteConfig) -> list[CheckMessage]:
    """LOUD when client self-enrollment is enabled on an anonymously-reachable route."""
    if not route.auto_register:
        return []
    if not _route_is_anonymous_reachable(route):
        return []
    return [
        Warning(
            f"FRISIAN_MCP_ROUTES[{route.name!r}] sets auto_register=True on a "
            "route that unauthenticated callers can reach.  Any unknown client "
            "can enroll itself on first contact.",
            hint=(
                "auto_register controls client self-enrollment, not tool "
                "discovery — set auto_discover for that.  Require "
                "authentication on this route, or set auto_register=False and "
                "enroll clients out of band."
            ),
            id=W005_AUTO_REGISTER_ANONYMOUS,
        )
    ]


def _check_auto_discover(route: RouteConfig) -> list[CheckMessage]:
    """SOFT when tool auto-discovery is enabled — a security-relevant non-default."""
    if not route.auto_discover:
        return []
    return [
        Warning(
            f"FRISIAN_MCP_ROUTES[{route.name!r}] sets auto_discover=True, so "
            "tools discovered in future will join this route's exposed surface "
            "without an explicit config change.",
            hint=(
                "The secure default is auto_discover=False.  Leave it enabled "
                "only if this route is meant to track the discovered surface; "
                "pair it with an explicit allow_list to bound what can appear."
            ),
            id=W006_AUTO_DISCOVER_ENABLED,
        )
    ]


def _check_privileged_permission_buckets(route: RouteConfig) -> list[CheckMessage]:
    """
    Audit an ``elevated`` / ``admin`` route's effective permission classes (WI-3).

    Buckets each class via the single definition in
    :mod:`frisian_mcp.route_views` — never re-derived here:

    * a literal ``AllowAny`` grant (or unmodified subclass) → **FATAL** (E006);
    * an opaque class that cannot be statically proven auth-requiring → **LOUD**
      (W011);
    * ``IsAuthenticated`` / ``IsAdminUser`` → pass.

    ``IsAuthenticatedOrReadOnly`` (the partial-anonymous bucket) is *not* handled
    here — it gates POST correctly and only opens the GET/SSE door, which
    :func:`_check_anonymous_sse` owns.  The ``default`` tier is exempt: it is
    allowed to be open, and its anonymous-POST hazard is E004's job.
    """
    if route.name not in ("elevated", "admin"):
        return []

    from frisian_mcp.route_views import (  # pylint: disable=import-outside-toplevel
        BUCKET_ANONYMOUS_GRANTING,
        BUCKET_OPAQUE,
        _bucket,
        route_effective_permission_classes,
    )

    classes = route_effective_permission_classes(route)
    buckets = [_bucket(c) for c in classes]
    messages: list[CheckMessage] = []

    if BUCKET_ANONYMOUS_GRANTING in buckets:
        messages.append(
            Error(
                f"FRISIAN_MCP_ROUTES[{route.name!r}] is a privileged route whose "
                "effective permission classes include a literal AllowAny grant, "
                "so unauthenticated callers reach its tool surface.  A privileged "
                "route must require authentication.",
                hint=(
                    "Remove AllowAny from FRISIAN_MCP_PERMISSION_CLASSES, or move "
                    "the open surface to the 'default' route.  The package will "
                    "not silently substitute IsAuthenticated over an explicit "
                    "AllowAny — that would make the config mean the opposite of "
                    "what it says."
                ),
                id=E006_ANONYMOUS_GRANT_ON_PRIVILEGED,
            )
        )
    elif BUCKET_OPAQUE in buckets:
        opaque = [
            f"{c.__module__}.{c.__qualname__}"
            for c, b in zip(classes, buckets, strict=True)
            if b == BUCKET_OPAQUE
        ]
        messages.append(
            Warning(
                f"FRISIAN_MCP_ROUTES[{route.name!r}] is a privileged route gated "
                f"by a permission class the audit cannot prove requires "
                f"authentication: {', '.join(opaque)}.  It may enforce auth by "
                "means the static audit cannot read; verify it does.",
                hint=(
                    "If the class does require authentication, no change is "
                    "needed — this is a heads-up, not an error.  The audit will "
                    "not refuse to boot on an unrecognised class, because that "
                    "would false-positive on legitimate custom auth."
                ),
                id=W011_UNPROVABLE_PERMISSION_CLASS,
            )
        )
    return messages


def _check_anonymous_sse(route: RouteConfig) -> list[CheckMessage]:
    """
    LOUD when an anonymous caller can open this route's GET / SSE channel (WI-3, R2).

    Distinct from :func:`_check_privileged_permission_buckets`: POST may be fully
    gated while GET is not (``IsAuthenticatedOrReadOnly``), and the hazard is a
    worker-pinning unauthenticated keepalive, not tool disclosure.  Severity is
    never conditioned on ``highest_tier`` — lowering the ceiling does not close
    the SSE door.  Suppressed under ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED``, the
    same acknowledgement that silences W001, so the open demo is not double-warned.
    """
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    if getattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED", False):
        return []

    from frisian_mcp.route_views import (  # pylint: disable=import-outside-toplevel
        route_is_anonymous_sse_reachable,
    )

    if not route_is_anonymous_sse_reachable(route):
        return []

    return [
        Warning(
            f"FRISIAN_MCP_ROUTES[{route.name!r}] serves its GET / SSE keepalive "
            "channel to unauthenticated callers.  The POST tool surface remains "
            "gated, but an anonymous client can hold an SSE stream open (up to "
            "FRISIAN_MCP_SSE_MAX_STREAM_SECONDS), pinning a worker thread — a "
            "resource-exhaustion surface, not a disclosure one.",
            hint=(
                "Require authentication on this route to close the SSE door, or "
                "set FRISIAN_MCP_ALLOW_UNAUTHENTICATED=True to acknowledge an "
                "intentionally open gateway (this also silences W001).  Lowering "
                "highest_tier does NOT mitigate this — the SSE channel is "
                "independent of the tool tier."
            ),
            id=W010_ANONYMOUS_SSE_REACHABLE,
        )
    ]


def _check_max_tier_caps_route(route: RouteConfig) -> list[CheckMessage]:
    """SOFT when a global ``FRISIAN_MCP_MAX_TIER`` caps the route below its declared ceiling."""
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    declared_rank = _tier_rank(route.highest_tier)
    if declared_rank is None:
        # Ceiling omitted; PR-7 resolves it to the tier key's secure default.
        # Only a *declared* ceiling can be contradicted by the global cap.
        return []

    max_tier = getattr(settings, "FRISIAN_MCP_MAX_TIER", None)
    cap_rank = _tier_rank(max_tier)
    if cap_rank is None or cap_rank >= declared_rank:
        return []

    return [
        Warning(
            f"FRISIAN_MCP_ROUTES[{route.name!r}] declares highest_tier="
            f"{route.highest_tier!r} but the global FRISIAN_MCP_MAX_TIER="
            f"{max_tier!r} caps every route below it.  The effective ceiling "
            f"for this route is {max_tier!r}; the declared value has no effect.",
            hint=(
                "The effective tier is min(token_tier, route ceiling, "
                "FRISIAN_MCP_MAX_TIER).  Raise or remove FRISIAN_MCP_MAX_TIER, "
                f"or lower this route's highest_tier to {max_tier!r} so the "
                "config states what actually happens."
            ),
            id=W007_MAX_TIER_CAPS_ROUTE,
        )
    ]


#: Per-route checks, run for every parsed route in deterministic name order.
_ROUTE_CHECKS = (
    _check_open_world_default,
    _check_allow_deny_grammar,
    _check_empty_allow_list,
    _check_auto_register,
    _check_auto_discover,
    _check_privileged_permission_buckets,
    _check_anonymous_sse,
    _check_max_tier_caps_route,
)


def audit_route_configs() -> list[CheckMessage]:
    """
    Run the config-only route audit and return its check messages.

    Returns an empty list when ``FRISIAN_MCP_ROUTES`` is unset — the implicit
    legacy route is not audited.

    Path validation short-circuits on the first offending route (that is
    :func:`~frisian_mcp.route_paths.validate_route_paths`'s contract, which
    visits routes in sorted name order so the reported error is stable across
    runs).  Every other trigger is evaluated for every route.
    """
    from frisian_mcp.route_config import (  # pylint: disable=import-outside-toplevel
        parse_route_configs,
    )
    from frisian_mcp.route_paths import (  # pylint: disable=import-outside-toplevel
        RoutePathError,
        validate_route_paths,
    )

    raw = _routes_setting()
    if raw is None:
        return []

    try:
        configs = parse_route_configs(raw)
    except ImproperlyConfigured as exc:
        return [
            Error(
                str(exc),
                hint=(
                    "FRISIAN_MCP_ROUTES maps a tier key ('default', 'elevated', "
                    "'admin') to a block with keys: path, highest_tier, "
                    "auto_discover, auto_register, allow_list, deny_list."
                ),
                id=E005_ROUTE_SCHEMA,
            )
        ]

    messages: list[CheckMessage] = []

    try:
        validate_route_paths(configs)
    except RoutePathError as exc:
        messages.append(
            Error(
                str(exc),
                hint=(
                    "Route paths are normalised (slashes stripped, runs "
                    "collapsed) before comparison.  Shared-prefix nesting is "
                    "allowed ('mcp' and 'mcp/elevated' may coexist); only exact "
                    "matches collide.  Paths reserved by the package cannot be "
                    "claimed or shadowed."
                ),
                id=f"frisian_mcp.{exc.code}",
            )
        )

    for name in sorted(configs):
        route = configs[name]
        for check in _ROUTE_CHECKS:
            messages.extend(check(route))

    return messages


def raise_on_fatal_route_config() -> None:
    """
    Raise :exc:`~django.core.exceptions.ImproperlyConfigured` on any FATAL finding.

    Called from :meth:`~frisian_mcp.apps.FrisianMcpConfig.ready`.  Watch-item 5:
    a :class:`~django.core.checks.Error` alone does not stop a WSGI server from
    booting and serving traffic — only an exception raised out of ``ready()``
    does.  The registered system check reports the same findings to
    ``manage.py check``; this function is what makes the refusal real.

    Warnings are left to the check framework and are not raised here.
    """
    errors = [m for m in audit_route_configs() if isinstance(m, Error)]
    if not errors:
        return

    detail = "\n".join(f"  [{m.id}] {m.msg}" for m in errors)
    raise ImproperlyConfigured(
        f"frisian-mcp refused to start: {len(errors)} fatal route-configuration "
        f"error(s) in FRISIAN_MCP_ROUTES.\n{detail}"
    )


@register(Tags.security)
def check_route_config(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001 — Django check signature
    **kwargs: Any,  # noqa: ARG001 — Django check signature
) -> list[CheckMessage]:
    """
    Report the per-route configuration audit to ``manage.py check``.

    Config-only.  The registry is empty while system checks run (auto-discovery
    is deferred to the first request), so surface-dependent triggers are not
    evaluated here — see the module docstring.
    """
    return audit_route_configs()


# ---------------------------------------------------------------------------
# Surface-dependent audit (phase two — runs once, at first request)
# ---------------------------------------------------------------------------
#
# These three triggers need a populated tool registry, which does not exist at
# Django-check time.  Evaluated against an empty surface they *invert* (every
# route reports an empty net), so they are emitted here, once, from the tail of
# ``_run_deferred_discovery`` when the surface is real.  LOUD and SOFT are
# defined by the plan as logs, so log emission satisfies them fully; there is
# no CI gate on these (that gap is PR-18's ``mcp_doctor`` surface).

#: Maps a :class:`~frisian_mcp.route_grammar.Finding` severity onto a logging
#: level.  LOUD is a prominent warning; SOFT is noticeable but non-blocking.
_SEVERITY_LOG_LEVEL = {
    "FATAL": logging.ERROR,
    "LOUD": logging.WARNING,
    "SOFT": logging.INFO,
}


def _cap_names(names: frozenset[str] | set[str]) -> str:
    """Render a sorted, explicitly-truncated tool-name list for a message."""
    ordered = sorted(names)
    shown = ordered[:_SURFACE_LIST_CAP]
    rendered = ", ".join(repr(n) for n in shown)
    extra = len(ordered) - len(shown)
    if extra > 0:
        rendered += f" (+{extra} more)"
    return rendered


def _tool_registry() -> Any:
    """Return the live tool registry.  Imported lazily, as everything here is."""
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        tool_registry,
    )

    return tool_registry


def _build_tool_surface() -> Any:
    """
    Snapshot the live tool registry into a :class:`~frisian_mcp.route_grammar.ToolSurface`.

    ``tool_names`` is every registered name — flat tools, group dispatchers, and
    the *hidden* bundled member tools alike — because deny/allow resolution
    matches against the full surface, not just what ``tools/list`` shows.
    ``group_members`` maps each dispatcher to its resource-leading member names
    (never the group label itself; the group name is a config label that does
    not appear in the tool names it bundles — see ``apps.py`` group install).
    """
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        tool_registry,
    )
    from frisian_mcp.route_grammar import (  # pylint: disable=import-outside-toplevel
        ToolSurface,
    )

    separator = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
    names = tool_registry.list_names()
    group_members: dict[str, frozenset[str]] = {}
    for name in names:
        entry = tool_registry.get_entry(name)
        if entry is not None and entry.group_tool_names:
            group_members[name] = entry.group_tool_names

    return ToolSurface.build(
        tool_names=names,
        group_members=group_members,
        tool_separator=separator,
    )


def _surface_findings_for_route(route: RouteConfig, surface: Any) -> list[Any]:
    """
    Compute the surface-dependent findings for one route.

    Returns :class:`~frisian_mcp.route_grammar.Finding` instances so the report
    reads uniformly alongside the grammar's own SOFT/LOUD findings.  Three
    triggers:

    * **W008 (LOUD)** — a non-empty ``allow_list`` selected tools, and
      ``deny_list`` removed every one of them.  Net exposure is empty.  (An
      *empty* ``allow_list`` is a different, config-time finding: W004.)
    * **W009 (SOFT)** — ``deny_list`` removed something and survivors remain: a
      working carve-out, surfaced once in case it was unintended.
    * **W110–W113** — the grammar's own per-entry findings, wrapped verbatim.
      Severity is consumed as-is (W113 is LOUD, the rest SOFT); it is never
      re-derived here — the grade lives in one place, ``RouteMatcher.audit``.
    """
    from frisian_mcp.route_grammar import (  # pylint: disable=import-outside-toplevel
        Finding,
        parse_lists,
    )

    findings: list[Any] = []
    matcher = parse_lists(route.allow_list, route.deny_list, route_name=route.name)

    # allow_union is what the allow_list alone selects, with deny suppressed.
    # Computed through the same matcher machinery so it cannot drift from
    # ``select``; the difference against the net set is exactly what deny removed.
    allow_union = parse_lists(route.allow_list, (), route_name=route.name).select(surface)
    net = matcher.select(surface)
    removed = allow_union - net

    if allow_union and not net:
        findings.append(
            Finding(
                severity="LOUD",
                code="W008",
                message=(
                    f"route {route.name!r}: allow_list selected {len(allow_union)} "
                    f"tool(s) but deny_list removed all of them "
                    f"({_cap_names(allow_union)}); the route exposes nothing"
                ),
                entry=None,
                list_name="deny_list",
                route_name=route.name,
            )
        )
    elif removed and net:
        findings.append(
            Finding(
                severity="SOFT",
                code="W009",
                message=(
                    f"route {route.name!r}: deny_list removed {len(removed)} "
                    f"tool(s) ({_cap_names(removed)}); {len(net)} remain exposed. "
                    "Flagged in case the carve-out was unintended."
                ),
                entry=None,
                list_name="deny_list",
                route_name=route.name,
            )
        )

    # S2 — per-entry grammar findings, wrapped verbatim (severity not re-derived).
    findings.extend(matcher.audit(surface, route_name=route.name))

    # GH #67 — cross-carve discoverability, guarded separately and on purpose.
    # The whole-audit try in ``audit_route_surface`` is not enough: a raise in
    # here would discard W008, W009 and every wrapped W110–W113 finding for
    # every route, so a bug in the newest check could silence the established
    # security findings wholesale.  Failures here cost this route's W017/W019
    # and nothing else.
    try:
        cross_carve, _ = _cross_carve_findings_for_route(route, net, _tool_registry())
        findings.extend(cross_carve)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logger.exception(
            "frisian_mcp: cross-carve check failed for route %r; "
            "other findings for this route are unaffected (non-fatal)",
            route.name,
        )
    return findings


# ---------------------------------------------------------------------------
# Cross-carve required-FK discoverability (GH #67)
# ---------------------------------------------------------------------------
#
# What this checks, and what it deliberately does not.
#
# A route carve removes **tools from a door**.  It does not remove a
# **permission from a principal** -- ``allow_list``/``deny_list`` are resolved
# against tool *names* and never reach a queryset, a serializer, or a
# permission class.  So this cannot predict the host's
# ``422 "Related object not found"``, which comes from a permission-filtered
# queryset inside serializer validation and turns on the principal's ``view``
# grant on the target model.  On the normal deployment -- operators carve
# routes to mirror grants -- writes through a flagged route succeed.
#
# What it *can* determine soundly, from configuration alone and with no
# principal involved, is a property of the carve itself: whether a write's
# required FK points at a model this door exposes no readable action for.
# That is a fact about the configured surface, and it is the whole of the
# claim.  It does not establish that the value is out of reach: nested FK
# representations are not permission-filtered, so another list's rows may
# still hand the identifier back.  Whether they do is runtime state this
# pass does not read -- see W017 below, and the 28-of-34 measurement there.


#: SOFT -- a write on this route requires a relation whose target model has no
#: ``list``/``retrieve`` action on the route's net surface.
#:
#: That is the whole claim, and it is deliberately weaker than the one this code
#: used to make.  It said the caller "cannot look up a valid value", which was
#: measured false on **28 of 34** findings on a real host: nested FK
#: representations are not permission-filtered, so an identifier the route
#: exposes no read for is still handed back inside another list's rows.  Whether
#: that happens depends on what those rows contain at the time, which is runtime
#: state this pass does not read -- so the finding reports the carve and stops.
W017_REQUIRED_FK_UNREADABLE = "frisian_mcp.W017"

#: SOFT -- the check ran but could not reach a verdict for one or more write
#: actions.  Emitted rather than staying silent because an unexamined action is
#: not a clean one, and a check that reports the two identically tells an
#: operator their configuration was inspected when it was not.
W019_CROSS_CARVE_UNEVALUATED = "frisian_mcp.W019"

#: DRF's standard actions mapped to the HTTP method the router binds them to.
#: Custom ``@action`` handlers are not here; their methods come from the
#: decorator's own ``mapping``, which is why *view_class* is needed as well as
#: the action name.
_STANDARD_ACTION_HTTP = {
    "create": "post",
    "update": "put",
    "partial_update": "patch",
    "destroy": "delete",
    "list": "get",
    "retrieve": "get",
}

#: The actions that let a caller discover a valid value for a relation.  A
#: write on the target model is not discovery -- you cannot learn an id by
#: being allowed to create one -- so write-reachability is deliberately not
#: accepted here.
_READ_ACTIONS = frozenset({"list", "retrieve"})

#: The acting side.  PATCH is ``partial_update`` and does not enforce
#: ``required``: the stored object already holds the relation, so an
#: unreachable target cannot make the call unsatisfiable.  DELETE carries no
#: body.  Both are therefore out.
_UNSATISFIABLE_METHODS = frozenset({"post", "put"})


@dataclasses.dataclass(frozen=True)
class CrossCarveReport:
    """
    The outcome of one cross-carve pass -- three states, never two.

    An empty ``findings`` list cannot say whether the check ran and found
    nothing or failed and found nothing, and those must not print the same.
    ``mcp_doctor`` reads *evaluated* before it reads *findings*.

    Attributes:
        evaluated: ``True`` when the pass completed.  ``False`` means it could
            not run at all; *reason* says why and *findings* is empty.
        reason: Why the pass did not complete, or ``None`` when it did.  Never
            populated alongside ``evaluated=True``.
        routes_checked: How many routes were examined.
        actions_checked: How many write actions were examined across them.
        findings: The W017/W019 findings, ready to log beside W008/W009.

    """

    evaluated: bool
    reason: str | None = None
    routes_checked: int = 0
    actions_checked: int = 0
    findings: tuple[Any, ...] = ()


def _http_methods_for_entry(entry: Any) -> frozenset[str]:
    """
    Return the HTTP methods *entry* is reachable by, or empty when undecidable.

    Standard DRF actions resolve from the action name alone.  A custom
    ``@action`` does not: the decorator parks its methods on the function as
    ``mapping``, so the ViewSet class is needed to reach them.  An empty result
    means "could not tell" -- it is never silently read as "not a write".
    """
    action = getattr(entry, "perm_drf_action", None)
    if not action:
        return frozenset()
    standard = _STANDARD_ACTION_HTTP.get(action)
    if standard is not None:
        return frozenset({standard})
    view_class = getattr(entry, "view_class", None)
    if view_class is None:
        return frozenset()
    mapping = getattr(getattr(view_class, action, None), "mapping", None)
    if isinstance(mapping, dict):
        return frozenset(str(method).lower() for method in mapping)
    return frozenset()


def _readable_model_labels(net: frozenset[str], registry: Any) -> set[str]:
    """
    Return the ``"app_label.model_name"`` set this net surface exposes a read for.

    Metadata only: this reads ``perm_app_label`` and ``perm_model``, which the
    registry already carries as plain strings.  It never touches a queryset --
    no ``.all()``, no ``.exists()``, no iteration -- so it constructs nothing,
    issues no query, and stays safe on the app-ready path where the database
    may not be migrated yet.
    """
    labels: set[str] = set()
    for name in net:
        entry = registry.get_entry(name)
        if entry is None:
            continue
        if getattr(entry, "perm_drf_action", None) not in _READ_ACTIONS:
            continue
        app_label = getattr(entry, "perm_app_label", None)
        model = getattr(entry, "perm_model", None)
        if app_label and model:
            labels.add(f"{app_label}.{model}")
    return labels


def _scan_route_writes(
    net: frozenset[str], registry: Any, readable: set[str]
) -> tuple[set[str], set[str], int]:
    """
    Walk one route's net surface and sort its write relations into two buckets.

    Returns ``(unreadable, unevaluated, actions_checked)`` -- relations whose
    target this route exposes no read for, relations we could not reach a
    verdict on, and how many write actions were examined.  The two buckets are
    kept apart all the way to the message: a relation we did not evaluate is
    not a relation we cleared.
    """
    from frisian_mcp._relations import (  # pylint: disable=import-outside-toplevel
        RelationOutcome,
        required_relations_for,
    )

    unreadable: set[str] = set()
    unevaluated: set[str] = set()
    actions_checked = 0

    for tool_name in sorted(net):
        entry = registry.get_entry(tool_name)
        if entry is None or not getattr(entry, "is_write", False):
            continue
        if not getattr(entry, "perm_drf_action", None):
            # Not a DRF ViewSet action -- a hand-registered ``@mcp_tool`` write
            # with an author-written schema.  There is no serializer behind it
            # and so no relation to resolve: the question does not apply, which
            # is different from a question we failed to answer.  Reporting it as
            # unevaluated would be a false could-not-evaluate, and an operator
            # who chases one and finds nothing there stops reading the rest.
            continue
        methods = _http_methods_for_entry(entry)
        if not methods:
            # A DRF action we cannot place on an HTTP method -- a custom
            # ``@action`` whose ``view_class`` did not survive registration, so
            # its decorator ``mapping`` is out of reach.  Silence here would be
            # a guess dressed as a clean result.
            actions_checked += 1
            unevaluated.add(f"{tool_name} (method undetermined)")
            continue
        if not methods & _UNSATISFIABLE_METHODS:
            continue
        actions_checked += 1
        relations = required_relations_for(tool_name)
        if relations is None:
            # Discovery never built a schema for this tool -- a manually
            # registered write, or a custom discovery backend that does not
            # collect relations.  Unknown, not clean.
            unevaluated.add(f"{tool_name} (not examined)")
            continue
        for relation in relations:
            # The emitted schema's ``required`` array decides this, not the
            # resolver's outcome.  A field the schema obliges the caller to send
            # has a discoverability question whatever introspection made of the
            # model field behind it -- and on a real host the inference declines
            # to answer for most of them, so reading the outcome first files the
            # whole population as could-not-evaluate and W017 never fires.  Only
            # a relation the schema does not require AND whose required-ness came
            # back unevaluated is genuinely one we failed to examine.
            undecidable = (
                relation.outcome is RelationOutcome.UNEVALUATED and not relation.schema_required
            )
            if undecidable or relation.target_label is None:
                unevaluated.add(f"{tool_name}.{relation.field_name}")
            elif relation.target_label not in readable:
                unreadable.add(f"{tool_name}.{relation.field_name} -> {relation.target_label}")
    return unreadable, unevaluated, actions_checked


def _cross_carve_findings_for_route(
    route: RouteConfig, net: frozenset[str], registry: Any
) -> tuple[list[Any], int]:
    """
    Compute W017/W019 for one route's net surface.

    Returns the findings and the number of write actions examined.  Findings are
    aggregated -- one W017 and at most one W019 per route -- because the
    *intended* configuration is what makes this large: a single resource carve
    can put several cross-carve relations on one create action, and a
    per-relation finding would emit hundreds of lines per boot, multiplied by
    every worker process.  A finding class that floods gets silenced whatever
    its severity.
    """
    from frisian_mcp.route_grammar import Finding  # pylint: disable=import-outside-toplevel

    readable = _readable_model_labels(net, registry)
    unreadable, unevaluated, actions_checked = _scan_route_writes(net, registry, readable)

    findings: list[Any] = []
    if unreadable:
        findings.append(
            Finding(
                severity="SOFT",
                code="W017",
                message=(
                    f"route {route.name!r}: {len(unreadable)} required relation(s) on write "
                    f"actions point at a model this route exposes no list/retrieve action "
                    f"for ({_cap_names(unreadable)}). A valid value may still be obtainable: "
                    "another response on this route may embed the identifier, and this check "
                    "does not examine response shapes. This reads the route's carve only, "
                    "never a principal's Django grants, so it neither predicts nor rules out "
                    "a write failing."
                ),
                entry=None,
                list_name="allow_list",
                route_name=route.name,
            )
        )
    if unevaluated:
        findings.append(
            Finding(
                severity="SOFT",
                code="W019",
                message=(
                    f"route {route.name!r}: {len(unevaluated)} write relation(s) could not be "
                    f"evaluated for discoverability ({_cap_names(unevaluated)}). These were "
                    "not checked, which is not the same as checked and clean."
                ),
                entry=None,
                list_name="allow_list",
                route_name=route.name,
            )
        )
    return findings, actions_checked


def audit_cross_carve_surface() -> CrossCarveReport:
    """
    Run the cross-carve discoverability pass and return its three-state report.

    Companion to :func:`audit_route_surface`, which logs findings and returns a
    bare list.  A list cannot carry three outcomes -- it is empty both when the
    pass found nothing and when it could not run -- so ``mcp_doctor`` reads this
    instead and prints "clean", "found", or "could not evaluate" accordingly.
    It must never print a green tick for an unevaluated posture.

    Like the rest of the surface audit this never raises: it is advisory and
    must not be able to break discovery or the gateway.  A failure comes back as
    ``evaluated=False`` with a *reason*.

    Silent -- ``evaluated=True`` over zero routes -- when ``FRISIAN_MCP_ROUTES``
    is unset, matching the rest of the audit: the implicit legacy route carves
    nothing, so nothing can be off-surface.

    Returns:
        A :class:`CrossCarveReport`.

    """
    raw = _routes_setting()
    if raw is None:
        return CrossCarveReport(evaluated=True)

    try:
        from frisian_mcp.route_config import (  # pylint: disable=import-outside-toplevel
            parse_route_configs,
        )
        from frisian_mcp.route_grammar import (  # pylint: disable=import-outside-toplevel
            parse_lists,
        )

        configs = parse_route_configs(raw)
        surface = _build_tool_surface()
        registry = _tool_registry()
        findings: list[Any] = []
        actions = 0
        for name in sorted(configs):
            route = configs[name]
            net = parse_lists(route.allow_list, route.deny_list, route_name=route.name).select(
                surface
            )
            route_findings, route_actions = _cross_carve_findings_for_route(route, net, registry)
            findings.extend(route_findings)
            actions += route_actions
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logger.exception("frisian_mcp: cross-carve audit failed (non-fatal)")
        return CrossCarveReport(evaluated=False, reason=f"{type(exc).__name__}: {exc}")

    return CrossCarveReport(
        evaluated=True,
        routes_checked=len(configs),
        actions_checked=actions,
        findings=tuple(findings),
    )


def audit_route_surface() -> list[Any]:
    """
    Run the surface-dependent route audit once and log its findings.

    Called from the tail of
    :meth:`~frisian_mcp.apps.FrisianMcpConfig._run_deferred_discovery`, after
    tool registration and dispatch-group install, so the tool surface is
    populated.  Returns the findings (for tests); logging them is the product
    behaviour.

    Silent when ``FRISIAN_MCP_ROUTES`` is unset — the implicit legacy route is
    not audited, exactly as the config-time pass is silent for it.

    This function never raises: the audit is advisory and must not be able to
    break tool discovery or the gateway.  Any failure is logged and swallowed.
    """
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    raw = _routes_setting()
    if raw is None:
        return []

    findings: list[Any] = []
    try:
        from frisian_mcp.route_config import (  # pylint: disable=import-outside-toplevel
            parse_route_configs,
        )

        configs = parse_route_configs(raw)
        surface = _build_tool_surface()
        for name in sorted(configs):
            findings.extend(_surface_findings_for_route(configs[name], surface))
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # The audit is advisory — a bug in it must never break tool discovery
        # or take down the gateway, so every failure is logged and swallowed.
        logger.exception("frisian_mcp: surface route-audit failed; skipping (non-fatal)")
        return []

    startup_print: bool = getattr(settings, "FRISIAN_MCP_STARTUP_PRINT", True)
    for finding in findings:
        level = _SEVERITY_LOG_LEVEL.get(finding.severity, logging.INFO)
        logger.log(
            level,
            "frisian_mcp route-audit %s [%s] %s",
            finding.severity,
            finding.code,
            finding.message,
        )
        # Mirror LOUD findings to stdout under the same flag the startup summary
        # uses (PKG-9): a host that pins the root logger to WARNING with no
        # handler would otherwise never see a prominent security warning.
        if finding.severity == "LOUD" and startup_print:
            print(  # noqa: T201 — conditionally-on prominent warning; see PKG-9
                f"[frisian-mcp] route-audit LOUD [{finding.code}] {finding.message}",
                flush=True,
            )

    return findings


def force_tool_discovery() -> bool:
    """
    Force the one-shot deferred tool discovery to run now, out of band.

    Django system checks and management commands run with **no HTTP request**, so
    ``_run_deferred_discovery`` (wired to ``request_started`` per the PKG-21
    deferral) never fires and the tool registry stays empty.  An explicit,
    side-effect-accepting caller — ``mcp_doctor`` here, and the ``E003`` surface
    that CR-21 will add — uses this to populate the registry so a
    surface-dependent audit has something real to read.

    This is the shared seam for exactly that.  It must **never** be called from a
    Django check: forcing discovery inside ``manage.py check`` is option (b),
    rejected on sight, because it re-introduces the very ordering bug PKG-21's
    deferral exists to prevent (host apps that append to ``INSTALLED_APPS`` after
    ours would be scanned before their ``ready()`` runs).

    Idempotent: returns ``True`` when this call ran discovery, ``False`` when the
    registry was already populated in this process (e.g. a prior request).  Note
    that ``_run_deferred_discovery`` runs :func:`audit_route_surface` itself as
    its final step, so a caller that also wants the findings can call
    :func:`audit_route_surface` after this returns; the second pass is pure over
    the same snapshot.

    Returns:
        ``True`` if discovery ran on this call, ``False`` if already done.

    """
    from typing import cast  # pylint: disable=import-outside-toplevel

    from django.apps import apps as django_apps  # pylint: disable=import-outside-toplevel

    # ``FrisianMcpConfig`` is a *typing-only* dependency here — the cast is a
    # string forward reference so this module never imports apps at runtime.
    # apps imports route_audit, so a real import closes a cycle (pylint R0401).
    app_config = cast("FrisianMcpConfig", django_apps.get_app_config("frisian_mcp"))
    if app_config._mcp_discovered:  # noqa: SLF001  # pylint: disable=protected-access
        return False
    app_config._run_deferred_discovery()  # noqa: SLF001  # pylint: disable=protected-access
    return True
