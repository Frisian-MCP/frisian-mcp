"""Django AppConfig for friese-mcp."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.conf import settings
from django.http import HttpRequest

if TYPE_CHECKING:
    from friese_mcp.backends.base import BaseInvocationBackend, ToolDefinition

logger = logging.getLogger(__name__)


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
        if not getattr(settings, "FRIESE_MCP_AUTODISCOVER", True):
            logger.debug("friese_mcp auto-discovery disabled — skipping")
            return

        # Deferred imports: friese_mcp.backends transitively imports
        # django.contrib.auth models, which require the app registry to be ready.
        # AppConfig.ready() is the first safe point after full app loading.
        from friese_mcp.backends import (  # pylint: disable=import-outside-toplevel
            get_discovery_backend,
            get_invocation_backend,
        )
        from friese_mcp.registry import tool_registry  # pylint: disable=import-outside-toplevel

        discovery = get_discovery_backend()
        invocation = get_invocation_backend()
        tool_defs = _apply_tool_filters(discovery.discover_tools())

        for tool_def in tool_defs:
            tool_registry.register(
                name=tool_def.name,
                fn=_make_invocation_fn(tool_def, invocation),
                description=tool_def.description,
                input_schema=tool_def.input_schema,
                permission_classes=list(tool_def.permission_classes),
            )

        if tool_defs:
            logger.info("friese_mcp: auto-discovery registered %d tools", len(tool_defs))
        else:
            logger.warning(
                "friese_mcp: auto-discovery found 0 tools. "
                "If your project uses @api_view FBVs, use @mcp_tool for manual registration."
            )
