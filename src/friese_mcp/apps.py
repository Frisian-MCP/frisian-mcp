"""Django AppConfig for friese-mcp."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.conf import settings
from django.http import HttpRequest

if TYPE_CHECKING:
    from friese_mcp.backends.base import BaseInvocationBackend, ToolDefinition

logger = logging.getLogger(__name__)


def _suppress_dispatcher_shadowed(
    tool_defs: list[ToolDefinition],
    dispatcher_names: frozenset[str],
) -> list[ToolDefinition]:
    """
    Remove auto-discovered tools that are shadowed by a registered dispatcher.

    A discovered tool ``{resource}.{action}`` is suppressed when *resource*
    exactly matches a dispatcher name, or when the dispatcher name is the
    plural form of *resource* (e.g. dispatcher ``"exercises"`` suppresses
    ``"exercise.list"``).

    Args:
        tool_defs: The filtered list of auto-discovered tool definitions.
        dispatcher_names: Dispatcher tool names already in the registry.

    Returns:
        A new list with shadowed tools removed; *tool_defs* is not mutated.

    """
    if not dispatcher_names:
        return tool_defs

    sep: str = getattr(settings, "FRIESE_MCP_TOOL_NAME_SEPARATOR", "_")
    result: list[ToolDefinition] = []
    for tool_def in tool_defs:
        matched: str | None = None
        for dname in dispatcher_names:
            if tool_def.name == dname or tool_def.name.startswith(f"{dname}{sep}") or (
                dname.endswith("s")
                and tool_def.name.startswith(f"{dname[:-1]}{sep}")
            ):
                matched = dname
                break
        if matched is not None:
            logger.info(
                "friese_mcp: suppressing auto-discovered tool %r — shadowed by dispatcher %r",
                tool_def.name,
                matched,
            )
        else:
            result.append(tool_def)
    return result


def _apply_tool_filters(tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    """
    Apply settings-based allowlist / denylist filters to discovered tool definitions.

    * ``FRIESE_MCP_TOOL_ALLOWLIST`` — when present, only tool names in this list
      are retained.  All others are silently dropped.
    * ``FRIESE_MCP_TOOL_DENYLIST``  — tool names in this list are dropped.
      Applied after the allowlist so that denylisting an allowed name still removes it.

    Both settings accept exact tool names (e.g. ``"users.destroy"``).

    Args:
        tool_defs: The raw list of tool definitions returned by discovery.

    Returns:
        A filtered list; the original *tool_defs* list is not mutated.

    """
    raw_allowlist: list[str] | None = getattr(settings, "FRIESE_MCP_TOOL_ALLOWLIST", None)
    raw_denylist: list[str] | None = getattr(settings, "FRIESE_MCP_TOOL_DENYLIST", None)

    result: list[ToolDefinition] = list(tool_defs)
    if raw_allowlist is not None:
        allowed: frozenset[str] = frozenset(raw_allowlist)
        result = [t for t in result if t.name in allowed]
        logger.debug("friese_mcp: ALLOWLIST applied — %d tools retained", len(result))
    if raw_denylist:
        denied: frozenset[str] = frozenset(raw_denylist)
        result = [t for t in result if t.name not in denied]
        logger.debug("friese_mcp: DENYLIST applied — %d tools retained", len(result))
    return result


#: Dotted path of the auto-installed HTTP middleware that strips a trailing
#: slash from the MCP gateway URL.  Hard-coded to the canonical location so
#: that idempotency checks remain stable across :meth:`AppConfig.ready` calls.
TRAILING_SLASH_MIDDLEWARE_PATH: str = "friese_mcp.middleware.McpTrailingSlashMiddleware"

#: Dotted path of Django's CommonMiddleware.  We insert immediately before
#: this entry so that path normalisation runs before APPEND_SLASH redirects.
COMMON_MIDDLEWARE_PATH: str = "django.middleware.common.CommonMiddleware"


def _install_trailing_slash_middleware() -> bool:
    """
    Auto-install :class:`~friese_mcp.middleware.McpTrailingSlashMiddleware`.

    Inserts :data:`TRAILING_SLASH_MIDDLEWARE_PATH` into ``settings.MIDDLEWARE``
    immediately before :data:`COMMON_MIDDLEWARE_PATH`.  When ``CommonMiddleware``
    is not present the entry is prepended at position 0.

    The function is **idempotent**: subsequent calls are no-ops if the
    middleware is already registered.  The MIDDLEWARE list is rebuilt with
    :class:`list` (rather than mutated in place) so that :class:`tuple`
    settings — which Django still accepts — are upgraded transparently.

    Returns:
        ``True`` when the middleware was inserted by this call, ``False`` when
        it was already present (or when ``MIDDLEWARE`` is missing entirely,
        which Django treats as no middleware configured).

    """
    middleware: list[str] | tuple[str, ...] | None = getattr(settings, "MIDDLEWARE", None)
    if middleware is None:
        # MIDDLEWARE undefined: Django treats this as an empty middleware
        # stack.  Set it to a single-entry list so our middleware still runs.
        settings.MIDDLEWARE = [TRAILING_SLASH_MIDDLEWARE_PATH]
        return True

    middleware_list: list[str] = list(middleware)
    if TRAILING_SLASH_MIDDLEWARE_PATH in middleware_list:
        return False

    if COMMON_MIDDLEWARE_PATH in middleware_list:
        idx = middleware_list.index(COMMON_MIDDLEWARE_PATH)
        middleware_list.insert(idx, TRAILING_SLASH_MIDDLEWARE_PATH)
    else:
        middleware_list.insert(0, TRAILING_SLASH_MIDDLEWARE_PATH)

    settings.MIDDLEWARE = middleware_list
    return True


#: Attribute name used as a sentinel to identify auto-registered URL patterns.
#: Prevents double-injection across multiple ready() calls in a single process.
_MCP_AUTO_URL_ATTR: str = "_friese_mcp_auto_url"
_OAUTH_AUTO_URL_ATTR: str = "_friese_mcp_oauth_auto_url"
_WELLKNOWN_AUTO_URL_ATTR: str = "_friese_mcp_wellknown_auto_url"

#: Stable ``dispatch_uid`` for the PKG-21 deferred discovery signal handler so
#: connect / disconnect calls reliably target the same registration even when
#: the closure object identity differs (e.g. across test cycles that re-run
#: ``ready()``, or after a re-import in dev autoreload).
_DEFERRED_DISCOVERY_UID: str = "friese_mcp.apps._on_first_request"


def _install_mcp_url() -> bool:
    """
    Auto-register :class:`~friese_mcp.views.McpView` in the live URL resolver.

    Inserts ``re_path(r'^{path}/?', include('friese_mcp.urls'))`` at position 0
    of the root resolver's ``url_patterns`` and calls
    :func:`~django.urls.clear_url_caches` so subsequent requests pick up the
    new pattern immediately.

    The URL path is read from ``settings.FRIESE_MCP_PATH`` (default: ``'mcp'``);
    leading and trailing slashes are stripped for consistent regex construction.

    The function is **idempotent**: subsequent calls are no-ops when either the
    auto-registered sentinel is already present or the operator has already
    included ``friese_mcp.urls`` explicitly (detected by ``app_name``).

    Returns:
        ``True`` when the URL was injected by this call, ``False`` when already
        present or when ``ROOT_URLCONF`` is not configured.

    """
    if not getattr(settings, "ROOT_URLCONF", None):
        return False

    from django.urls import (  # pylint: disable=import-outside-toplevel
        clear_url_caches,
        get_resolver,
        include,
        re_path,
    )

    resolver = get_resolver()

    # Already auto-registered in this process.
    if any(getattr(p, _MCP_AUTO_URL_ATTR, False) for p in resolver.url_patterns):
        return False

    # Operator already included friese_mcp.urls explicitly — app_name is set
    # by the `app_name = "friese_mcp"` declaration in friese_mcp/urls.py.
    for pattern in resolver.url_patterns:
        if getattr(pattern, "app_name", None) == "friese_mcp":
            return False

    mcp_path = re.escape(getattr(settings, "FRIESE_MCP_PATH", "mcp").strip("/"))
    auto_resolver = re_path(rf"^{mcp_path}/?", include("friese_mcp.urls"))
    setattr(auto_resolver, _MCP_AUTO_URL_ATTR, True)
    resolver.url_patterns.insert(0, auto_resolver)
    clear_url_caches()
    return True


def _install_oauth_urls() -> bool:
    """
    Auto-register OAuth endpoint URLs when ``friese_mcp.contrib.oauth`` is installed.

    Mounts ``friese_mcp.contrib.oauth.urls`` at ``/oauth/`` in the live URL
    resolver so that ``/oauth/authorize/``, ``/oauth/token/``, and
    ``/oauth/register/`` are available without any ``urls.py`` changes by the
    host application.  Idempotent — no-op if the patterns are already present.
    """
    from django.apps import apps  # pylint: disable=import-outside-toplevel

    if not apps.is_installed("friese_mcp.contrib.oauth"):
        return False
    if not getattr(settings, "ROOT_URLCONF", None):
        return False

    from django.urls import (  # pylint: disable=import-outside-toplevel
        clear_url_caches,
        get_resolver,
        include,
        path,
    )

    resolver = get_resolver()
    if any(getattr(p, _OAUTH_AUTO_URL_ATTR, False) for p in resolver.url_patterns):
        return False
    for pattern in resolver.url_patterns:
        if getattr(pattern, "app_name", None) == "friese_mcp_oauth":
            return False

    auto_resolver = path("oauth/", include("friese_mcp.contrib.oauth.urls"))
    setattr(auto_resolver, _OAUTH_AUTO_URL_ATTR, True)
    resolver.url_patterns.insert(0, auto_resolver)
    clear_url_caches()
    return True


def _install_wellknown_urls() -> bool:
    """
    Auto-register well-known discovery URLs when ``friese_mcp.contrib.oauth`` is installed.

    Mounts ``friese_mcp.contrib.oauth.wellknown_urls`` at ``/.well-known/`` so
    that RFC 8414 and RFC 9728 discovery endpoints are reachable without any
    ``urls.py`` changes by the host application.  Idempotent.
    """
    from django.apps import apps  # pylint: disable=import-outside-toplevel

    if not apps.is_installed("friese_mcp.contrib.oauth"):
        return False
    if not getattr(settings, "ROOT_URLCONF", None):
        return False

    from django.urls import (  # pylint: disable=import-outside-toplevel
        clear_url_caches,
        get_resolver,
        include,
        path,
    )

    resolver = get_resolver()
    if any(getattr(p, _WELLKNOWN_AUTO_URL_ATTR, False) for p in resolver.url_patterns):
        return False
    for pattern in resolver.url_patterns:
        if getattr(pattern, "app_name", None) == "friese_mcp_oauth_wellknown":
            return False

    auto_resolver = path(".well-known/", include("friese_mcp.contrib.oauth.wellknown_urls"))
    setattr(auto_resolver, _WELLKNOWN_AUTO_URL_ATTR, True)
    resolver.url_patterns.insert(0, auto_resolver)
    clear_url_caches()
    return True


#: Match ``api/`` as a path segment — at the start of the prefix or after a
#: ``/``.  ``DRFSyncDiscovery`` populates ``url_path`` from the resolver-tree
#: walk, which produces unanchored strings like ``api/dcim/^devices/$``
#: (segment at start, no leading slash) or ``some/api/path/^x/$`` (segment in
#: the middle).  A bare ``"/api/" in url_path`` substring check misses the
#: leading-segment case and silently picks the wrong winner; the regex form
#: catches both shapes and still rejects ``notapi/`` / ``myapi/`` as
#: non-segment substrings.
_API_PATH_SEGMENT_RE = re.compile(r"(^|/)api/")


def _prefer_api_tool(
    existing: ToolDefinition | None, candidate: ToolDefinition
) -> ToolDefinition:
    """
    Resolve a basename collision between two discovered ToolDefinitions.

    PKG-22: when a host app exposes both UI and API ViewSets that share the
    same model object name (e.g. ``WidgetUIViewSet`` alongside
    ``WidgetViewSet`` in an ``api`` submodule), DRF routers register both
    under the same basename.  Walk-order alone is non-deterministic across
    plugin loading, so the merge step must pick a winner explicitly.

    Rule: prefer the tool whose ``url_path`` contains an ``api/`` path
    segment — that is the canonical REST surface.  When neither path has
    one or both do, fall back to first-seen so the existing-installed-base
    behaviour for pure-API hosts is unchanged.

    The path-segment regex (``(^|/)api/``) covers both ``api/dcim/...`` (no
    leading slash, segment at start) and ``some/api/...`` (segment in
    middle), while still rejecting ``notapi/`` and ``myapi/`` substrings.
    """
    if existing is None:
        return candidate
    existing_is_api = bool(_API_PATH_SEGMENT_RE.search(existing.url_path))
    candidate_is_api = bool(_API_PATH_SEGMENT_RE.search(candidate.url_path))
    if candidate_is_api and not existing_is_api:
        return candidate
    if existing_is_api and not candidate_is_api:
        return existing
    # Both API or both non-API: keep first-seen (the existing behaviour for
    # any host without parallel UI/API surfaces).
    return existing


def _find_group_members(
    all_names: list[str], prefix_set: frozenset[str], sep: str
) -> set[str]:
    """Return the subset of *all_names* whose resource prefix is in *prefix_set*."""
    members: set[str] = set()
    for tool_name in all_names:
        for prefix in prefix_set:
            if tool_name == prefix or tool_name.startswith(f"{prefix}{sep}"):
                members.add(tool_name)
                break
    return members


def _install_dispatch_groups() -> tuple[int, int]:
    """
    Build group dispatcher tools from ``settings.FRIESE_MCP_DISPATCH_GROUPS``.

    Reads the mapping ``{group_name: [resource_prefix, ...]}`` from settings
    and, for each group, registers ONE dispatcher tool that routes
    ``{"resource": R, "action": A, "params": P}`` to the registered flat
    tool ``f"{R}.{A}"``.  Member flat tools are marked hidden so they no
    longer appear in ``tools/list`` (they remain dispatchable by name for
    advanced callers and for the group dispatcher's own routing).

    Idempotency is implicit: this is called once per ``ready()`` invocation
    via the ``_mcp_ready`` guard in :meth:`FrieseMcpConfig.ready`.

    Returns:
        A 2-tuple ``(group_count, bundled_tool_count)`` where *group_count* is
        the number of group dispatchers registered and *bundled_tool_count* is
        the total number of flat tools bundled across all groups (each bundled
        tool counted once even if matched by multiple groups).

    """
    groups: dict[str, list[str]] | None = getattr(
        settings, "FRIESE_MCP_DISPATCH_GROUPS", None
    )
    if not groups:
        return 0, 0

    # Deferred imports: backends.group_dispatcher imports from registry which
    # depends on django.contrib.auth — safe only after AppConfig.ready().
    from friese_mcp.backends.group_dispatcher import (  # pylint: disable=import-outside-toplevel
        build_group_input_schema,
        make_group_invoke,
    )
    from friese_mcp.registry import tool_registry  # pylint: disable=import-outside-toplevel

    registered_count = 0
    all_bundled: set[str] = set()
    all_names = tool_registry.list_names()
    sep: str = getattr(settings, "FRIESE_MCP_TOOL_NAME_SEPARATOR", "_")

    for group_name, resource_prefixes in groups.items():
        prefix_set = frozenset(resource_prefixes)
        member_tools = _find_group_members(all_names, prefix_set, sep)

        if not member_tools:
            logger.warning(
                "FRIESE_MCP_DISPATCH_GROUPS: group %r has no matching resources "
                "(prefixes=%s) — no dispatcher registered",
                group_name,
                sorted(prefix_set),
            )
            continue

        invoke_fn = make_group_invoke(
            group_name, frozenset(member_tools), tool_registry, prefix_set
        )
        tool_registry.register(
            name=group_name,
            fn=invoke_fn,
            description=(
                f"Group dispatcher for {len(member_tools)} tools across "
                f"{len(prefix_set)} resources. Use action='help' to discover."
            ),
            input_schema=build_group_input_schema(),
            permission_classes=[],
            permission_tier="read",
        )
        for member_name in member_tools:
            tool_registry.set_hidden(member_name, True)

        registered_count += 1
        all_bundled.update(member_tools)
        logger.info(
            "friese_mcp: registered group dispatcher %r bundling %d tools",
            group_name,
            len(member_tools),
        )

    return registered_count, len(all_bundled)


def _make_invocation_fn(
    tool_def: ToolDefinition,
    invocation: BaseInvocationBackend,
) -> Callable[[dict[str, Any], HttpRequest], Any]:
    """
    Return a registry-compatible callable that invokes *tool_def* via *invocation*.

    Uses a factory function to capture *tool_def* and *invocation* correctly in
    the closure, avoiding the classic Python loop variable late-binding issue.

    Args:
        tool_def: The discovered :class:`~friese_mcp.backends.base.ToolDefinition`.
        invocation: The configured invocation backend instance.

    Returns:
        A callable with signature ``(arguments, request) -> Any`` that delegates
        to :meth:`~friese_mcp.backends.base.BaseInvocationBackend.invoke` and
        returns the result content.  Raises :exc:`RuntimeError` when the
        invocation backend signals a tool-level error via ``ToolResult.is_error``.

    """

    def _invoke(arguments: dict[str, Any], request: HttpRequest) -> Any:
        from friese_mcp.registry import (  # pylint: disable=import-outside-toplevel
            ToolInvocationError,
        )

        result = invocation.invoke(tool_def, arguments, request)
        if result.is_error:
            raise ToolInvocationError(result.content)
        return result.content

    return _invoke


class FrieseMcpConfig(AppConfig):
    """AppConfig for the friese-mcp Django application."""

    name = "friese_mcp"
    verbose_name = "Friese MCP Gateway"
    default_auto_field = "django.db.models.BigAutoField"
    _mcp_ready: bool = False
    #: Tracks whether deferred discovery has run.  Distinct from ``_mcp_ready``
    #: because discovery now happens on the first request (PKG-21), not at
    #: ``ready()`` time.  Reset by test fixtures that drive multiple cycles.
    _mcp_discovered: bool = False

    def ready(self) -> None:
        """
        Run startup logic when Django is fully loaded.

        When both ``FRIESE_MCP_ENABLED`` (default ``True``) and
        ``FRIESE_MCP_AUTODISCOVER`` (default ``True``) are truthy, scans the
        Django URL resolver tree for DRF ViewSet actions and registers each
        discovered action as an MCP tool in
        :data:`~friese_mcp.registry.tool_registry`.

        Discovery is delegated to the backend configured via
        ``settings.FRIESE_MCP_DISCOVERY_BACKEND`` (default:
        :class:`~friese_mcp.backends.discovery.DRFSyncDiscovery`).  Invocation
        wrappers are built with ``settings.FRIESE_MCP_INVOCATION_BACKEND``
        (default: :class:`~friese_mcp.backends.invocation.SyncInvocation`).

        Each discovered tool is registered in :data:`~friese_mcp.registry.tool_registry`
        under the name ``{resource}.{action}`` (e.g. ``"users.list"``), with the
        ViewSet's ``permission_classes`` inherited verbatim.  ViewSets and
        individual actions decorated with ``@mcp_ignore`` are skipped.

        This method is **idempotent**: a second call (e.g. from a test runner
        that reloads apps, or from ``@override_settings``) is a no-op.

        """
        # Idempotency guard: test runners, @override_settings, and reload scenarios
        # can call ready() more than once.  A second call would re-register all
        # auto-discovered tools, silently overwriting existing registrations and
        # producing duplicate log entries.  Guard against this with a simple flag.
        if self._mcp_ready:
            logger.debug("friese_mcp ready() called again — skipping duplicate auto-discovery")
            return
        self._mcp_ready = True

        # SEC-4: import the checks module so its @register decorators fire and
        # ``manage.py check`` runs them.  Importing here (rather than at module
        # top) keeps the dependency chain inside ready() — the same point where
        # Django guarantees the app registry is populated.
        # pylint: disable-next=import-outside-toplevel,unused-import
        from friese_mcp import checks  # noqa: F401

        if not getattr(settings, "FRIESE_MCP_ENABLED", True):
            logger.debug("friese_mcp disabled — skipping auto-discovery")
            return

        # Auto-install the trailing-slash HTTP middleware before any other
        # discovery work.  This must happen on every ready() invocation where
        # the gateway is enabled because settings.MIDDLEWARE may have been
        # rebound by @override_settings in tests; the helper itself is
        # idempotent so re-running is safe in normal startup as well.
        if _install_trailing_slash_middleware():
            logger.debug(
                "friese_mcp: auto-installed %s before CommonMiddleware",
                TRAILING_SLASH_MIDDLEWARE_PATH,
            )

        if _install_mcp_url():
            logger.debug(
                "friese_mcp: auto-registered McpView URL at path %r",
                getattr(settings, "FRIESE_MCP_PATH", "mcp").strip("/"),
            )

        if _install_oauth_urls():
            logger.debug("friese_mcp: auto-registered OAuth URLs at /oauth/")

        if _install_wellknown_urls():
            logger.debug("friese_mcp: auto-registered well-known URLs at /.well-known/")

        if not getattr(settings, "FRIESE_MCP_AUTODISCOVER", True):
            logger.debug("friese_mcp auto-discovery disabled — skipping")
            return

        # PKG-21: defer the URL-tree scan and tool registration to the first
        # incoming request.  AppConfig.ready() runs in INSTALLED_APPS order, so
        # any plugin / app appended after friese_mcp (e.g. host plugin loaders
        # that append to INSTALLED_APPS at config-evaluation time) hasn't run
        # its own ready() yet — and many register URL patterns there.
        # Scanning now would silently miss every late-bound tool with no
        # error or warning.
        # request_started fires once per request before view dispatch, so the
        # registry is fully populated by the time tools/list is served.  The
        # handler disconnects itself + the ``_mcp_discovered`` flag prevents
        # any race-window double-execution.
        from django.core.signals import (  # pylint: disable=import-outside-toplevel
            request_started,
        )

        def _on_first_request(
            sender: Any = None,  # pylint: disable=unused-argument
            **_: Any,
        ) -> None:
            try:
                request_started.disconnect(dispatch_uid=_DEFERRED_DISCOVERY_UID)
            except Exception:  # noqa: S110, BLE001  # pylint: disable=broad-exception-caught
                # Disconnect can race with a parallel signal fire under ASGI;
                # the _mcp_discovered guard inside _run_deferred_discovery is
                # the authoritative idempotency protection — losing the
                # disconnect is harmless (the next call short-circuits).
                pass
            self._run_deferred_discovery()

        request_started.connect(
            _on_first_request,
            weak=False,
            dispatch_uid=_DEFERRED_DISCOVERY_UID,
        )

    def _run_deferred_discovery(self) -> None:
        """
        Run URL-tree scan + tool registration + dispatch-group install.

        Called once on the first request (PKG-21), or directly by tests that
        need to drive the full startup pipeline without a real HTTP request.
        Idempotent via the ``_mcp_discovered`` flag — repeat calls are no-ops.
        """
        if self._mcp_discovered:
            return
        self._mcp_discovered = True

        # Deferred imports: friese_mcp.backends transitively imports
        # django.contrib.auth models, which require the app registry to be ready.
        # AppConfig.ready() is the first safe point after full app loading.
        from friese_mcp.backends import (  # pylint: disable=import-outside-toplevel
            get_discovery_backends,
            get_invocation_backend,
        )
        from friese_mcp.middleware import load_middleware  # pylint: disable=import-outside-toplevel
        from friese_mcp.registry import tool_registry  # pylint: disable=import-outside-toplevel

        invocation = get_invocation_backend()
        dispatcher_names = tool_registry.list_dispatcher_names()

        # Collect tool definitions from all configured backends.  PKG-22:
        # on basename collision (e.g. UI + API ViewSets sharing the same
        # ``model._meta.object_name``) prefer the entry whose ``url_path``
        # contains ``/api/``.  Without this, URL-tree walk order alone
        # picks the winner — non-deterministic across plugin loading and
        # silently mis-routes tools to whichever flavour happened to be
        # walked last.  Any DRF host with parallel UI + API ViewSets
        # benefits.
        merged: dict[str, Any] = {}
        for discovery in get_discovery_backends():
            for tool_def in _apply_tool_filters(discovery.discover_tools()):
                merged[tool_def.name] = _prefer_api_tool(
                    merged.get(tool_def.name), tool_def
                )

        tool_defs = _suppress_dispatcher_shadowed(list(merged.values()), dispatcher_names)

        for tool_def in tool_defs:
            tool_registry.register(
                name=tool_def.name,
                fn=_make_invocation_fn(tool_def, invocation),
                description=tool_def.description,
                input_schema=tool_def.input_schema,
                permission_classes=list(tool_def.permission_classes),
                permission_tier=tool_def.permission_tier,
            )

        load_middleware()

        # Always emit the startup summary via both logger AND print() so that
        # operators can verify the package loaded regardless of how the host
        # app has configured the 'friese_mcp' logger.  Many host apps set
        # the root logger to WARNING and never configure a 'friese_mcp'
        # handler, which silently drops INFO messages.  See PKG-9.
        mcp_path = getattr(settings, "FRIESE_MCP_PATH", "mcp").strip("/")
        if tool_defs:
            logger.info("friese_mcp: auto-discovery registered %d tools", len(tool_defs))
            print(  # noqa: T201 — intentional always-on startup summary; see PKG-9
                f"[friese-mcp] registered {len(tool_defs)} tools at /{mcp_path}/",
                flush=True,
            )
        else:
            logger.warning(
                "friese_mcp: auto-discovery found 0 tools. "
                "If your project uses @api_view FBVs, use @mcp_tool for manual registration."
            )
            print(  # noqa: T201 — intentional always-on startup summary; see PKG-9
                f"[friese-mcp] registered 0 tools at /{mcp_path}/ "
                "(use @mcp_tool for manual registration if you rely on @api_view FBVs)",
                flush=True,
            )

        # Group dispatchers run last so they can bundle every tool registered
        # above (auto-discovered + decorator + dispatcher).  Bundled flat tools
        # are marked hidden and disappear from tools/list.
        group_count, bundled_count = _install_dispatch_groups()
        if group_count:
            print(  # noqa: T201 — intentional always-on startup summary; see PKG-9
                f"[friese-mcp] {group_count} dispatch group(s) bundling "
                f"{bundled_count} tools",
                flush=True,
            )

        tool_hints: dict | None = getattr(settings, "FRIESE_MCP_TOOL_HINTS", None)
        if tool_hints:
            print(  # noqa: T201 — intentional always-on startup summary; see PKG-9
                f"[friese-mcp] {len(tool_hints)} tool hint(s) configured "
                "(surfaced via action='help')",
                flush=True,
            )
