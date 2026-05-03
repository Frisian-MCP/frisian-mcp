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

    result: list[ToolDefinition] = []
    for tool_def in tool_defs:
        prefix = tool_def.name.split(".")[0] if "." in tool_def.name else tool_def.name
        matched: str | None = None
        for dname in dispatcher_names:
            if prefix == dname or (dname.endswith("s") and prefix == dname[:-1]):
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

    for group_name, resource_prefixes in groups.items():
        prefix_set = frozenset(resource_prefixes)
        member_tools: set[str] = set()
        for tool_name in all_names:
            prefix = tool_name.split(".", 1)[0] if "." in tool_name else tool_name
            if prefix in prefix_set:
                member_tools.add(tool_name)

        if not member_tools:
            logger.warning(
                "FRIESE_MCP_DISPATCH_GROUPS: group %r has no matching resources "
                "(prefixes=%s) — no dispatcher registered",
                group_name,
                sorted(prefix_set),
            )
            continue

        invoke_fn = make_group_invoke(
            group_name, frozenset(member_tools), tool_registry
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
        result = invocation.invoke(tool_def, arguments, request)
        if result.is_error:
            raise RuntimeError(str(result.content))
        return result.content

    return _invoke


class FrieseMcpConfig(AppConfig):
    """AppConfig for the friese-mcp Django application."""

    name = "friese_mcp"
    verbose_name = "Friese MCP Gateway"
    default_auto_field = "django.db.models.BigAutoField"
    _mcp_ready: bool = False

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

        if not getattr(settings, "FRIESE_MCP_AUTODISCOVER", True):
            logger.debug("friese_mcp auto-discovery disabled — skipping")
            return

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

        # Collect tool definitions from all configured backends; later backends
        # win on name clashes (dict preserves insertion order, last write wins).
        merged: dict[str, Any] = {}
        for discovery in get_discovery_backends():
            for tool_def in _apply_tool_filters(discovery.discover_tools()):
                merged[tool_def.name] = tool_def

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
        # app has configured the 'friese_mcp' logger.  Most host apps (e.g.
        # Nautobot) set the root logger to WARNING and never configure a
        # 'friese_mcp' handler, which silently drops INFO messages.  See PKG-9.
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
