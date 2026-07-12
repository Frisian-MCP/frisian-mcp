"""
Per-route materialised tool views for the frisian-mcp gateway.

This module is the seam every per-route permission concern bolts onto (PR-1
ADR).  A :class:`RouteView` is an immutable snapshot of exactly the tools one
configured route exposes, computed once at build time as
``allow_list − deny_list`` over the global registry surface.  The absence
property is the whole point: a tool a route denies is **not present** in that
route's view — not at discovery, not at invocation, not in a group's advertised
counts, not in its hints.  A denied name behaves byte-for-byte like a name that
was never registered anywhere.

Two entry kinds, deliberately asymmetric (BLOCKER-2 ruling, option (a))
-----------------------------------------------------------------------

* **Flat tools are shared by reference.**  A plain ``_ToolEntry`` that survives
  the filter is the *same object* the global registry holds — no copy, no
  double registration.  Its own ``permission_tier`` and ``permission_classes``
  still gate it through :meth:`ToolRegistry.dispatch`.

* **Group dispatchers are rebuilt per route.**  A group dispatcher with a
  partial carve-out cannot be shared, because the denied resources live inside
  four surfaces frozen at registration time.  Sharing the global entry and
  filtering at call time (option (b)) would make a denied resource *rejected*
  rather than *absent*, and would put a standing correctness obligation on every
  call site.  Instead the entry is rebuilt from a pruned member set so every
  derived surface is correct by construction.  Four surfaces must be pruned:

  1. the ``tool_names`` frozenset closed over by ``make_group_invoke`` — its
     membership gate makes a denied resource unroutable;
  2. the ``resource_prefixes`` set closed over by the same closure — otherwise a
     denied resource is named back in a ``difflib`` "did you mean?" and in the
     help resource tree;
  3. the advertised count frozen into the ``description`` string — otherwise the
     count disagrees with what ``help`` lists (WI-1);
  4. ``_ToolEntry.group_tool_names`` — otherwise downstream membership reads the
     full, un-pruned set.

  Pruning site #1 fixes hints and the help resource tree for free — both iterate
  the same closed-over ``tool_names``.  A dispatcher whose entire group is denied
  is dropped from the view entirely rather than rebuilt empty.

Rebuild triggers (watch-item 4)
-------------------------------

A view is immutable once built; a rebuild produces a *new* view and swaps the
pointer under a lock in a single assignment — never mount-unfiltered-then-prune,
never clear-then-repopulate.  Rebuilds fire at process start / app reload only.
There is **no** polling, filesystem watcher, or registry watchdog thread; a host
with genuine runtime plugin registration calls :meth:`RouteViewRegistry.rebuild_all`
from its own discovery backend.

Permission resolution and anonymous-reachability
------------------------------------------------

:func:`route_effective_permission_classes` and the two anonymous-reachability
predicates live here (not in ``checks.py``) so there is a single definition the
startup audit imports rather than re-derives — a predicate split across two
modules is how the audit and the runtime end up disagreeing.  ``checks.py``
(PR-9b) consumes these; it must not re-implement them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.permissions import (
    AllowAny,
    BasePermission,
    IsAdminUser,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
)

from frisian_mcp.registry import (
    _TIER_RANK,
    ToolNotFoundError,
    _normalize_argument_keys,
    tool_registry,
)
from frisian_mcp.route_config import RouteConfig, parse_route_configs
from frisian_mcp.route_grammar import ToolSurface, parse_lists
from frisian_mcp.route_paths import validate_route_paths

if TYPE_CHECKING:
    from django.http import HttpRequest

    from frisian_mcp.registry import ToolRegistry, _ToolEntry

__all__ = [
    "LEGACY_ROUTE_NAME",
    "RouteView",
    "RouteViewRegistry",
    "resolve_route_ceiling",
    "route_effective_permission_classes",
    "route_is_anonymous_reachable",
    "route_is_anonymous_sse_reachable",
    "route_views",
]

#: Name of the implicit view mounted when ``FRISIAN_MCP_ROUTES`` is unset — the
#: backwards-compatible surface (allow everything, no ceiling).  Absence of the
#: *setting* means legacy; absence of a *key* on a configured route means secure
#: default (PR-7).  These are different absences and must not be conflated.
LEGACY_ROUTE_NAME: str = "__legacy_default__"

#: Secure per-tier-key ceiling default applied when a configured route omits
#: ``highest_tier``.  Keyed by the route's config name, not by ``highest_tier``.
#: PR-7 owns the ``min(token, ceiling, MAX_TIER)`` resolution that reads this;
#: it lives here so the resolver and the view share one table.
SECURE_DEFAULT_CEILING: Mapping[str, str] = MappingProxyType(
    {"default": "read", "elevated": "read_write", "admin": "admin"}
)


def _min_tier(*tiers: str | None) -> str | None:
    """
    Return the most restrictive of *tiers*; ``None`` values mean "no cap".

    This is the ``min`` in ``min(token_tier, route_ceiling, MAX_TIER)`` (ADR-010
    §8).  ``min`` is monotone — it can only narrow, never widen.  A tier string
    absent from ``_TIER_RANK`` ranks as ``read`` (most restrictive), so an
    unrecognised value fails closed rather than open.
    """
    present = [t for t in tiers if t is not None]
    if not present:
        return None
    return min(present, key=lambda t: _TIER_RANK.get(t, 0))


def resolve_route_ceiling(route: RouteConfig) -> str | None:
    """
    Return *route*'s effective tier ceiling, or ``None`` for genuinely uncapped.

    On a **configured** route, an omitted ``highest_tier`` NEVER means uncapped
    (PM ruling, ADR-010 §8): it resolves to the route key's secure default from
    :data:`SECURE_DEFAULT_CEILING`.  Otherwise an open ``default`` route with no
    ``highest_tier`` would be strictly more dangerous than the audit's own FATAL
    condition, and silent.  Only the implicit :data:`LEGACY_ROUTE_NAME` view —
    which exists only when ``FRISIAN_MCP_ROUTES`` is unset — stays uncapped;
    absence of the *setting* means legacy, absence of the *key* means secure
    default, and the two absences must not be conflated.
    """
    if route.name == LEGACY_ROUTE_NAME:
        return route.highest_tier
    if route.highest_tier is not None:
        return route.highest_tier
    # Route names are validated against TIER_KEYS at parse time; the "read"
    # fallback is fail-closed belt-and-braces, not a reachable branch.
    return SECURE_DEFAULT_CEILING.get(route.name, "read")


# ---------------------------------------------------------------------------
# Permission-class resolution + bucket predicate (single definition; PR-9b
# imports these, never re-derives them).
# ---------------------------------------------------------------------------

#: Permission classes that require an authenticated principal for every method.
_AUTH_REQUIRING: tuple[type[BasePermission], ...] = (IsAuthenticated, IsAdminUser)

#: Permission classes that permit anonymous *safe-method* (GET/HEAD/OPTIONS)
#: access but deny anonymous writes.  These make a route's SSE channel
#: anonymously reachable while its POST tool surface stays gated.
_PARTIAL_ANONYMOUS: tuple[type[BasePermission], ...] = (IsAuthenticatedOrReadOnly,)

# Bucket labels returned by :func:`_bucket`.
BUCKET_AUTH_REQUIRING: str = "auth_requiring"
BUCKET_ANONYMOUS_GRANTING: str = "anonymous_granting"
BUCKET_PARTIAL_ANONYMOUS: str = "partial_anonymous"
BUCKET_OPAQUE: str = "opaque"


def _is_anonymous_granting(cls: type) -> bool:
    """
    Return ``True`` when *cls* is an unmodified ``AllowAny`` grant.

    ``cls is AllowAny`` fails open: ``class OpenPerm(AllowAny): pass`` defeats it
    while still granting every request.  ``issubclass`` catches the subclass; the
    ``has_permission`` identity clause keeps the test *literal* — a subclass that
    actually overrides the gate is no longer semantically ``AllowAny`` and falls
    through to the opaque bucket where it is judged on its own merits.
    """
    return (
        isinstance(cls, type)
        and issubclass(cls, AllowAny)
        and cls.has_permission is AllowAny.has_permission
    )


def _bucket(cls: type) -> str:
    """Classify a permission class for the startup anonymous-access audit."""
    if _is_anonymous_granting(cls):
        return BUCKET_ANONYMOUS_GRANTING
    if isinstance(cls, type) and issubclass(cls, _AUTH_REQUIRING):
        return BUCKET_AUTH_REQUIRING
    if isinstance(cls, type) and issubclass(cls, _PARTIAL_ANONYMOUS):
        return BUCKET_PARTIAL_ANONYMOUS
    return BUCKET_OPAQUE


def _resolve_global_permission_classes() -> list[type]:
    """
    Return ``settings.FRISIAN_MCP_PERMISSION_CLASSES`` as class objects.

    Accepts dotted-path strings or class objects, mirroring ``McpView``'s own
    resolution.  An unset setting and an empty list both yield ``[]`` — both mean
    "no gateway-level permission class was configured", which the resolver treats
    as the trigger for per-route secure defaults.
    """
    raw = getattr(settings, "FRISIAN_MCP_PERMISSION_CLASSES", None)
    if not raw:
        return []
    resolved: list[type] = []
    for item in raw:
        resolved.append(import_string(item) if isinstance(item, str) else item)
    return resolved


def route_effective_permission_classes(route: RouteConfig) -> list[type]:
    """
    Return the permission classes that gate *route*'s HTTP surface.

    Resolution (BLOCKER-1 ruling):

    * A non-empty global ``FRISIAN_MCP_PERMISSION_CLASSES`` wins **verbatim** for
      every route — the operator's explicit choice is authoritative.
    * With no global classes, a ``default`` route stays open (``[]``) and an
      ``elevated`` / ``admin`` route gets ``[IsAuthenticated]``.  Rule 3 is
      load-bearing: without it, an ``admin`` route with no global classes would
      silently serve anonymous traffic and nothing else would catch it.
    """
    global_classes = _resolve_global_permission_classes()
    if global_classes:
        return list(global_classes)
    if route.name == "default":
        return []
    return [IsAuthenticated]


def route_is_anonymous_reachable(route: RouteConfig) -> bool:
    """
    Return ``True`` when an anonymous caller can reach *route*'s POST tool surface.

    POST is not a safe method, so ``IsAuthenticatedOrReadOnly`` and opaque classes
    do **not** count as reachable here — only an empty class list or an
    unmodified ``AllowAny`` grant.  Feeds the anonymous-admin FATAL and the
    ``auto_register``-on-anonymous LOUD (L3).  This is deliberately narrower than
    :func:`route_is_anonymous_sse_reachable`; conflating them ships a false
    positive on L3 — the exact flag this project exists to make trustworthy.
    """
    classes = route_effective_permission_classes(route)
    if not classes:
        return True
    return any(_is_anonymous_granting(c) for c in classes)


def route_is_anonymous_sse_reachable(route: RouteConfig) -> bool:
    """
    Return ``True`` when an anonymous caller can open *route*'s GET SSE channel.

    GET is a safe method, so ``IsAuthenticatedOrReadOnly`` (the
    ``_PARTIAL_ANONYMOUS`` bucket) counts here even though it denies anonymous
    POST.  Feeds the ``_PARTIAL_ANONYMOUS`` LOUD, whose message must name the SSE
    mechanism — the risk is a worker-pinning keepalive, not tool disclosure, so
    the severity is never conditioned on ``highest_tier``.
    """
    classes = route_effective_permission_classes(route)
    if not classes:
        return True
    return any(
        _is_anonymous_granting(c) or (isinstance(c, type) and issubclass(c, _PARTIAL_ANONYMOUS))
        for c in classes
    )


# ---------------------------------------------------------------------------
# Discovery shaping over an arbitrary entries mapping.
# ---------------------------------------------------------------------------


def _list_entries(
    entries: Mapping[str, _ToolEntry],
    *,
    max_tier: str | None,
    entry_filter: Callable[[Any], bool] | None,
    action_filter_factory: Callable[[Any], Callable[[str, Any], bool] | None] | None,
) -> list[dict[str, Any]]:
    """
    Shape *entries* into MCP ``tools/list`` format, tier-filtered.

    Mirrors :meth:`ToolRegistry.list_tools` but iterates a supplied mapping so a
    :class:`RouteView` filters over its own (already deny-carved) entries.  Group
    dispatchers are hidden when none of their perm-aware children pass
    *entry_filter*, and their action enum is rebuilt to the caller's tier; a
    dispatcher with zero visible actions is omitted.  Child lookups resolve
    against *entries*, so a denied child is never counted.
    """
    # pylint: disable=import-outside-toplevel
    from frisian_mcp.backends.dispatcher import _build_dispatcher_input_schema

    max_rank = _TIER_RANK.get(max_tier, 2) if max_tier is not None else 2
    tools: list[dict[str, Any]] = []
    for entry in entries.values():
        if entry.hidden:
            continue
        if _TIER_RANK.get(entry.permission_tier, 0) > max_rank:
            continue
        if entry_filter is not None and not entry_filter(entry):
            continue

        if entry.group_tool_names and entry_filter is not None:
            perm_children = [
                entries[t]
                for t in entry.group_tool_names
                if t in entries
                and entries[t].perm_app_label is not None
                and entries[t].perm_model is not None
            ]
            if perm_children and not any(entry_filter(c) for c in perm_children):
                continue

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

        action_filter = action_filter_factory(entry) if action_filter_factory is not None else None
        filtered_schema = _build_dispatcher_input_schema(
            entry.dispatcher_meta, max_tier=max_tier, action_filter=action_filter
        )
        visible_actions = filtered_schema["properties"]["action"]["enum"]
        if max_tier is not None and not visible_actions:
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


# ---------------------------------------------------------------------------
# RouteView
# ---------------------------------------------------------------------------


class RouteView:  # pylint: disable=too-many-instance-attributes
    """
    Immutable snapshot of the tools one route exposes.

    Construct via :meth:`build`; never mutate ``entries`` after construction.
    ``entries`` maps tool name to ``_ToolEntry`` — flat entries are the global
    registry's own objects; rebuilt group-dispatcher entries are route-local.
    """

    __slots__ = (
        "_local_dispatch_names",
        "_registry",
        "advertised_counts",
        "ceiling",
        "entries",
        "hint_key_allow",
        "path",
        "route_name",
    )

    def __init__(
        self,
        *,
        route_name: str,
        path: str,
        ceiling: str | None,
        entries: Mapping[str, _ToolEntry],
        advertised_counts: Mapping[str, int],
        hint_key_allow: frozenset[str],
        local_dispatch_names: frozenset[str],
        registry: ToolRegistry,
    ) -> None:
        """Freeze the materialised view.  Prefer :meth:`build` over calling this."""
        # MappingProxyType over a dict that is never re-bound after __init__ —
        # prevents accidental mutation without preventing GC of the old view on
        # an atomic pointer swap.
        self.route_name = route_name
        self.path = path
        self.ceiling = ceiling
        self.entries: Mapping[str, _ToolEntry] = MappingProxyType(dict(entries))
        self.advertised_counts: Mapping[str, int] = MappingProxyType(dict(advertised_counts))
        self.hint_key_allow = hint_key_allow
        self._local_dispatch_names = local_dispatch_names
        # The registry this view was materialised against — flat-tool dispatch
        # delegates here.  In production this is the global singleton; threading
        # it keeps the view honest about its backing store and testable in
        # isolation.
        self._registry = registry

    # -- construction -------------------------------------------------------

    @classmethod
    def build(cls, registry: ToolRegistry, config: RouteConfig) -> RouteView:
        """
        Materialise the view for *config* against *registry*'s current surface.

        Computes ``allow_list − deny_list`` via the grammar matcher, then walks
        the registry once: flat survivors are shared by reference; group
        dispatchers with a partial carve-out are rebuilt from their pruned member
        set; fully-denied groups are dropped.
        """
        sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
        raw_entries = registry.entries_snapshot()

        group_members: dict[str, frozenset[str]] = {
            name: entry.group_tool_names
            for name, entry in raw_entries.items()
            if entry.group_tool_names is not None
        }
        surface = ToolSurface.build(
            tool_names=frozenset(raw_entries),
            group_members=group_members,
            tool_separator=sep,
        )
        matcher = parse_lists(config.allow_list, config.deny_list, route_name=config.name)
        visible: frozenset[str] = matcher.select(surface)

        return cls._assemble(registry, config, raw_entries, visible, sep)

    @classmethod
    def _assemble(  # pylint: disable=too-many-locals
        cls,
        registry: ToolRegistry,
        config: RouteConfig,
        raw_entries: Mapping[str, _ToolEntry],
        visible: frozenset[str],
        sep: str,
    ) -> RouteView:
        """Walk the surface once and produce the frozen view fields."""
        group_prefixes = _configured_group_prefixes()

        entries: dict[str, _ToolEntry] = {}
        advertised_counts: dict[str, int] = {}
        local_dispatch_names: set[str] = set()

        for name, entry in raw_entries.items():
            if entry.group_tool_names is not None:
                # Group dispatcher — decided by its surviving members, not by
                # whether its own name is in ``visible``.
                survivors = frozenset(m for m in entry.group_tool_names if m in visible)
                if not survivors:
                    continue  # entire group denied → absent, not empty
                if survivors == entry.group_tool_names:
                    entries[name] = entry  # whole group intact → share by reference
                    advertised_counts[name] = len(survivors)
                else:
                    rebuilt = _rebuild_group_dispatcher(
                        registry, entry, survivors, group_prefixes.get(name, frozenset()), sep
                    )
                    entries[name] = rebuilt
                    local_dispatch_names.add(name)
                    advertised_counts[name] = len(rebuilt.group_tool_names or frozenset())
                continue

            if name in visible:
                entries[name] = entry  # flat survivor → share by reference

        # Hint keys visible on this route are exactly the visible member/flat
        # names; ``make_group_invoke`` filters hints against the (pruned) member
        # set, so pruning site #1 already enforces this at invoke time.  The
        # field pins it for the absence-invariant tests (WI-1).
        hint_key_allow = frozenset(n for n in visible if n in raw_entries)

        return cls(
            route_name=config.name,
            path=config.path,
            ceiling=config.highest_tier,
            entries=entries,
            advertised_counts=advertised_counts,
            hint_key_allow=hint_key_allow,
            local_dispatch_names=frozenset(local_dispatch_names),
            registry=registry,
        )

    # -- discovery ----------------------------------------------------------

    def list_tools(
        self,
        max_tier: str | None = None,
        entry_filter: Callable[[Any], bool] | None = None,
        action_filter_factory: Callable[[Any], Callable[[str, Any], bool] | None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return this route's ``tools/list`` payload.

        Same signature as :meth:`ToolRegistry.list_tools`; iterates the route's
        deny-carved ``entries`` so a denied tool is absent from discovery exactly
        as if it were never registered.  PR-7 passes the *capped* effective tier
        as ``max_tier`` so a write-capable token on a ``read``-ceiling route never
        sees write actions here.
        """
        return _list_entries(
            self.entries,
            max_tier=max_tier,
            entry_filter=entry_filter,
            action_filter_factory=action_filter_factory,
        )

    # -- invocation ---------------------------------------------------------

    def dispatch(self, request: HttpRequest, name: str, arguments: dict[str, Any]) -> Any:
        """
        Validate, authorise, and invoke *name* within this route.

        A name absent from :attr:`entries` raises :class:`ToolNotFoundError` with
        the byte-identical message the global registry raises for a
        never-registered tool (WI-1 error parity).  Flat tools and un-pruned
        dispatchers delegate to :meth:`ToolRegistry.dispatch` — the route entry is
        the same object, so validation, tier, and permission checks are
        unchanged.  A *rebuilt* (pruned) group dispatcher is invoked through its
        route-local closure so denied resources are unroutable; its member calls
        still re-enter ``registry.dispatch`` for full per-tool enforcement.
        """
        entry = self.entries.get(name)
        if entry is None:
            raise ToolNotFoundError(f"No tool registered with name {name!r}")

        if name in self._local_dispatch_names:
            if getattr(settings, "FRISIAN_MCP_NORMALIZE_INPUT_CASE", True):
                arguments = _normalize_argument_keys(arguments)
            return entry.fn(arguments, request)

        return self._registry.dispatch(request, name, arguments)


def _configured_group_prefixes() -> dict[str, frozenset[str]]:
    """
    Return ``{group_name: normalised resource-prefix set}`` from settings.

    Reads ``FRISIAN_MCP_DISPATCH_GROUPS`` and applies the same ``"-"``→``"_"``
    normalisation ``apps._install_dispatch_groups`` uses, so a rebuilt dispatcher
    prunes against the identical prefix vocabulary the original was built from.
    """
    groups: dict[str, list[str]] | None = getattr(settings, "FRISIAN_MCP_DISPATCH_GROUPS", None)
    if not groups:
        return {}
    return {
        group: frozenset(p.replace("-", "_") for p in prefixes)
        for group, prefixes in groups.items()
    }


def _rebuild_group_dispatcher(
    registry: ToolRegistry,
    entry: _ToolEntry,
    survivors: frozenset[str],
    prefix_set: frozenset[str],
    sep: str,
) -> _ToolEntry:
    """
    Return a route-local group-dispatcher entry pruned to *survivors*.

    Prunes all four registration-frozen surfaces: the closure's member set and
    resource-prefix set (via a fresh :func:`make_group_invoke`), the advertised
    count in ``description``, and ``group_tool_names`` on the entry itself.  The
    fresh closure routes member calls through *registry* — the same store the
    view was built against — so a rebuilt dispatcher never re-enters the global
    singleton behind the view's back.

    When *prefix_set* is available (from ``FRISIAN_MCP_DISPATCH_GROUPS``) the
    surviving resource labels are the configured prefixes that still match a
    survivor — this preserves multi-word resources (``location_type``).  When it
    is empty, the labels are derived from the survivor names by splitting on the
    separator; multi-word resources only exist when a prefix set was configured,
    so the split is unambiguous in that fallback.
    """
    # pylint: disable=import-outside-toplevel
    from frisian_mcp.backends.group_dispatcher import make_group_invoke
    from frisian_mcp.registry import _ToolEntry as ToolEntry

    if prefix_set:
        surviving_prefixes = frozenset(
            p for p in prefix_set if any(m == p or m.startswith(f"{p}{sep}") for m in survivors)
        )
    else:
        surviving_prefixes = frozenset(m.split(sep, 1)[0] for m in survivors)
    invoke_fn = make_group_invoke(entry.name, survivors, registry, surviving_prefixes)
    rebuilt = ToolEntry(
        name=entry.name,
        fn=invoke_fn,
        description=(
            f"Group dispatcher for {len(survivors)} tools across "
            f"{len(surviving_prefixes)} resources. Use action='help' to discover."
        ),
        input_schema=entry.input_schema,
        permission_classes=list(entry.permission_classes),
        is_dispatcher=True,
        permission_tier=entry.permission_tier,
        group_tool_names=survivors,
    )
    return rebuilt


# ---------------------------------------------------------------------------
# RouteViewRegistry
# ---------------------------------------------------------------------------


class RouteViewRegistry:
    """
    Process-scoped holder of per-route :class:`RouteView` snapshots.

    A rebuild constructs the new view fully, then replaces the pointer in a
    single locked dict assignment (watch-item 4): a reader that grabbed the old
    view keeps a consistent snapshot for its whole request, and no window of
    unfiltered exposure ever exists.
    """

    def __init__(self) -> None:
        """Initialise an empty, unlocked registry."""
        self._views: dict[str, RouteView] = {}
        self._lock = threading.Lock()

    def get(self, route_name: str) -> RouteView | None:
        """Return the current view for *route_name*, or ``None`` if unmounted."""
        with self._lock:
            return self._views.get(route_name)

    def names(self) -> frozenset[str]:
        """Return the set of currently-mounted route names."""
        with self._lock:
            return frozenset(self._views)

    def rebuild(self, config: RouteConfig, registry: ToolRegistry | None = None) -> RouteView:
        """
        Build a fresh view for *config* and atomically swap it in.

        The view is constructed in full *before* the lock is taken, so the
        critical section is a single dict assignment.
        """
        view = RouteView.build(registry or tool_registry, config)
        with self._lock:
            self._views[config.name] = view
        return view

    def rebuild_all(self, registry: ToolRegistry | None = None) -> None:
        """
        Rebuild every configured route from ``settings.FRISIAN_MCP_ROUTES``.

        Called once at the end of deferred discovery (``apps.py``) and, for hosts
        with genuine runtime plugin registration, by the host's own discovery
        backend.  When ``FRISIAN_MCP_ROUTES`` is unset, mounts the single legacy
        view (allow everything, no ceiling) so behaviour matches today's exactly.
        This method does not poll, watch, or spawn a thread.
        """
        reg = registry or tool_registry
        configs = _configured_route_configs()
        built: dict[str, RouteView] = {
            name: RouteView.build(reg, cfg) for name, cfg in configs.items()
        }
        with self._lock:
            self._views = built


def _configured_route_configs() -> dict[str, RouteConfig]:
    """
    Return the parsed, path-validated route configs, or the legacy fallback.

    ``FRISIAN_MCP_ROUTES`` unset → a single :data:`LEGACY_ROUTE_NAME` config
    exposing everything with no ceiling (today's behaviour).  When set, paths are
    normalised and collision-checked (PR-5) and canonical paths replace the raw
    strings so mounting and validation cannot drift.
    """
    raw = getattr(settings, "FRISIAN_MCP_ROUTES", None)
    if not raw:
        legacy = RouteConfig(
            name=LEGACY_ROUTE_NAME,
            path=getattr(settings, "FRISIAN_MCP_PATH", "mcp"),
            highest_tier=None,
            allow_list=("*",),
            deny_list=(),
        )
        return {LEGACY_ROUTE_NAME: legacy}

    configs = parse_route_configs(raw)
    canonical = validate_route_paths(configs)
    return {
        name: RouteConfig(
            name=cfg.name,
            path=canonical[name],
            highest_tier=cfg.highest_tier,
            auto_discover=cfg.auto_discover,
            auto_register=cfg.auto_register,
            allow_list=cfg.allow_list,
            deny_list=cfg.deny_list,
        )
        for name, cfg in configs.items()
    }


#: Process-scoped singleton.  ``McpView`` subclasses (PR-6 wiring) resolve their
#: view from this; custom discovery backends call ``route_views.rebuild_all()``.
route_views: RouteViewRegistry = RouteViewRegistry()
