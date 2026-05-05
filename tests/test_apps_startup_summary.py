"""
Tests for the always-on startup summary printed by ``FrieseMcpConfig.ready()``.

PKG-9: host apps that don't explicitly configure a ``friese_mcp`` logger handler
silently drop the package's INFO-level startup messages, so operators cannot
tell whether the package loaded.  ``ready()`` must therefore emit the key
summary lines via ``print()`` in addition to the existing ``logger.info``
calls so they are always visible in container logs.
"""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
# pylint: disable=import-outside-toplevel,missing-function-docstring
from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from django.apps import apps
from django.test import override_settings
from django.urls import clear_url_caches, get_resolver

from friese_mcp.apps import FrieseMcpConfig
from friese_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubInvocation:
    """
    Minimal invocation backend stub.

    ``ready()`` calls ``get_invocation_backend()`` unconditionally and feeds
    the returned object into ``_make_invocation_fn`` for every discovered
    tool.  The actual ``invoke()`` is never exercised here — the closure
    only runs when a request hits the registered tool — so a tiny stub is
    sufficient to satisfy the wiring.
    """

    def invoke(
        self, tool_def: Any, arguments: dict[str, Any], request: Any
    ) -> Any:  # pragma: no cover — never called by these tests
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_app_config() -> Generator[FrieseMcpConfig, None, None]:
    """
    Yield the live FrieseMcpConfig with both idempotency flags reset.

    AppConfig.ready() is called once at Django startup; the ``_mcp_ready``
    flag prevents subsequent invocations from re-running its body, and
    PKG-21's ``_mcp_discovered`` flag guards the deferred discovery path.
    These tests need to drive both repeatedly so we reset both for the
    duration of each test and restore them afterwards.

    Also disconnects any deferred-discovery handler the test connects via
    ``ready()`` so subsequent tests do not see stale signal receivers from
    earlier ``ready()`` calls.
    """
    from django.core.signals import request_started

    from friese_mcp.apps import _DEFERRED_DISCOVERY_UID

    config = apps.get_app_config("friese_mcp")
    assert isinstance(config, FrieseMcpConfig)
    original_ready = config._mcp_ready
    original_discovered = config._mcp_discovered
    config._mcp_ready = False
    config._mcp_discovered = False
    request_started.disconnect(dispatch_uid=_DEFERRED_DISCOVERY_UID)
    try:
        yield config
    finally:
        config._mcp_ready = original_ready
        config._mcp_discovered = original_discovered
        request_started.disconnect(dispatch_uid=_DEFERRED_DISCOVERY_UID)


@pytest.fixture()
def isolated_registry() -> Generator[ToolRegistry, None, None]:
    """
    Patch the module-level ``tool_registry`` with a fresh instance.

    ``ready()`` writes into ``friese_mcp.registry.tool_registry`` directly;
    swapping it with a fresh registry isolates each test from process-wide
    side effects (other tests, or a previous ``ready()`` call, may have
    populated the real registry).
    """
    fresh = ToolRegistry()
    with patch("friese_mcp.registry.tool_registry", fresh):
        yield fresh


@pytest.fixture()
def empty_urlconf() -> Generator[str, None, None]:
    """Register an in-process URLconf with no patterns and yield its name."""
    import sys
    import types

    name = "_test_apps_startup_urlconf"
    mod = types.ModuleType(name)
    mod.urlpatterns = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    yield name
    sys.modules.pop(name, None)


@pytest.fixture()
def reset_resolver(
    empty_urlconf: str, settings: Any
) -> Generator[None, None, None]:
    """
    Point ROOT_URLCONF at the empty fixture and clear the resolver.

    Pinning ``ROOT_URLCONF`` here (instead of via ``@override_settings`` on
    each test method) guarantees the resolver fixture runs against the
    fixture URLconf — fixture setup happens *before* method-level decorators
    take effect, which would otherwise cause ``AttributeError`` on
    ``settings.ROOT_URLCONF`` in environments that do not define it.

    ``_install_mcp_url`` mutates ``get_resolver().url_patterns`` in place; we
    must restore it so unrelated URL-resolution tests are not affected.
    """
    settings.ROOT_URLCONF = empty_urlconf
    resolver = get_resolver()
    saved = list(resolver.url_patterns)
    resolver.url_patterns.clear()
    clear_url_caches()
    try:
        yield
    finally:
        resolver.url_patterns.clear()
        resolver.url_patterns.extend(saved)
        clear_url_caches()


# ---------------------------------------------------------------------------
# Startup summary — happy path
# ---------------------------------------------------------------------------


class TestStartupSummaryPrint:
    """ready() must print the tools-registered summary regardless of log config."""

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="mcp",
    )
    def test_summary_printed_with_zero_tools(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,  # noqa: ARG002
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Even when discovery finds no tools, the summary line is printed."""
        # Patch discovery to return zero tools so we exercise the 0-tool branch
        # without depending on the test URLconf to expose anything.
        with patch(
            "friese_mcp.backends.get_discovery_backends", return_value=[]
        ), patch(
            "friese_mcp.backends.get_invocation_backend",
            return_value=_StubInvocation(),
        ):
            fresh_app_config.ready()
            # PKG-21: discovery is now deferred to the first request.
            fresh_app_config._run_deferred_discovery()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "[friese-mcp]" in combined
        assert "registered 0 tools at /mcp/" in combined

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="mcp",
    )
    def test_summary_printed_with_discovered_tools(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,  # noqa: ARG002
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When discovery finds tools, the count appears in the printed line."""
        from friese_mcp.backends.base import ToolDefinition

        fake_tool = ToolDefinition(
            name="widget.list",
            description="stub",
            input_schema={"type": "object", "properties": {}},
            permission_classes=(),
            source="auto",
            permission_tier="read",
        )

        class _StubBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                return [fake_tool]

        with patch(
            "friese_mcp.backends.get_discovery_backends", return_value=[_StubBackend()]
        ), patch(
            "friese_mcp.backends.get_invocation_backend",
            return_value=_StubInvocation(),
        ):
            fresh_app_config.ready()
            # PKG-21: discovery is now deferred to the first request.
            fresh_app_config._run_deferred_discovery()

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "[friese-mcp] registered 1 tools at /mcp/" in combined

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="api/mcp",
    )
    def test_summary_uses_custom_mcp_path(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,  # noqa: ARG002
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The printed path reflects FRIESE_MCP_PATH after slash stripping."""
        with patch(
            "friese_mcp.backends.get_discovery_backends", return_value=[]
        ), patch(
            "friese_mcp.backends.get_invocation_backend",
            return_value=_StubInvocation(),
        ):
            fresh_app_config.ready()
            # PKG-21: discovery is now deferred to the first request.
            fresh_app_config._run_deferred_discovery()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "/api/mcp/" in combined


# ---------------------------------------------------------------------------
# Visibility independent of host logging configuration
# ---------------------------------------------------------------------------


class TestStartupSummaryLoggerIndependence:
    """The summary must appear even when the friese_mcp logger drops INFO."""

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="mcp",
    )
    def test_summary_visible_when_logger_level_is_warning(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,  # noqa: ARG002
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Raising the logger level above INFO must not silence the summary."""
        import logging

        friese_logger = logging.getLogger("friese_mcp.apps")
        original_level = friese_logger.level
        original_handlers = list(friese_logger.handlers)
        friese_logger.setLevel(logging.WARNING)
        # Strip every handler so logger.info() really does end up dropped.
        for handler in original_handlers:
            friese_logger.removeHandler(handler)
        try:
            with patch(
                "friese_mcp.backends.get_discovery_backends", return_value=[]
            ), patch(
                "friese_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ):
                fresh_app_config.ready()
                # PKG-21: discovery is now deferred to the first request.
                fresh_app_config._run_deferred_discovery()
            captured = capsys.readouterr()
            combined = captured.out + captured.err
            assert "[friese-mcp]" in combined
            assert "registered 0 tools at /mcp/" in combined
        finally:
            friese_logger.setLevel(original_level)
            for handler in original_handlers:
                friese_logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Dispatch group summary
# ---------------------------------------------------------------------------


class TestDispatchGroupSummary:
    """When dispatch groups are configured, the summary line is also printed."""

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="mcp",
        FRIESE_MCP_DISPATCH_GROUPS={"widgets": ["widget"]},
    )
    def test_group_summary_printed_when_groups_configured(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A configured group with at least one matching resource is summarised."""
        from friese_mcp.backends.base import ToolDefinition

        # Two flat tools that both match the widget prefix; they will be
        # bundled into the single 'widgets' group dispatcher.
        defs = [
            ToolDefinition(
                name="widget_list",
                description="stub",
                input_schema={"type": "object", "properties": {}},
                permission_classes=(),
                source="auto",
                permission_tier="read",
            ),
            ToolDefinition(
                name="widget_retrieve",
                description="stub",
                input_schema={"type": "object", "properties": {}},
                permission_classes=(),
                source="auto",
                permission_tier="read",
            ),
        ]

        class _StubBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                return defs

        # _install_dispatch_groups reads from friese_mcp.registry.tool_registry,
        # so the isolated_registry fixture (which patches that name) is what
        # the dispatcher will inspect after ready() registers the flat tools.
        with patch(
            "friese_mcp.backends.get_discovery_backends", return_value=[_StubBackend()]
        ), patch(
            "friese_mcp.backends.get_invocation_backend",
            return_value=_StubInvocation(),
        ):
            fresh_app_config.ready()
            # PKG-21: discovery is now deferred to the first request.
            fresh_app_config._run_deferred_discovery()

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "[friese-mcp] 1 dispatch group(s) bundling 2 tools" in combined
        # And we still expect the auto-discovery summary line above it.
        assert "[friese-mcp] registered 2 tools at /mcp/" in combined

    @override_settings(
        FRIESE_MCP_ENABLED=True,
        FRIESE_MCP_AUTODISCOVER=True,
        FRIESE_MCP_PATH="mcp",
    )
    def test_no_group_summary_when_setting_absent(
        self,
        fresh_app_config: FrieseMcpConfig,
        isolated_registry: ToolRegistry,  # noqa: ARG002
        reset_resolver: None,  # noqa: ARG002
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without FRIESE_MCP_DISPATCH_GROUPS no group summary line is emitted."""
        with patch(
            "friese_mcp.backends.get_discovery_backends", return_value=[]
        ), patch(
            "friese_mcp.backends.get_invocation_backend",
            return_value=_StubInvocation(),
        ):
            fresh_app_config.ready()
            # PKG-21: discovery is now deferred to the first request.
            fresh_app_config._run_deferred_discovery()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "dispatch group" not in combined


# ---------------------------------------------------------------------------
# ``_install_dispatch_groups`` direct unit tests for the new tuple return
# ---------------------------------------------------------------------------


class TestInstallDispatchGroupsReturn:
    """Verify the new (group_count, bundled_count) tuple contract directly."""

    def test_returns_zero_zero_when_setting_absent(self, settings: Any) -> None:
        """Without the setting both elements of the tuple are zero."""
        from friese_mcp.apps import _install_dispatch_groups

        if hasattr(settings, "FRIESE_MCP_DISPATCH_GROUPS"):
            del settings.FRIESE_MCP_DISPATCH_GROUPS
        result = _install_dispatch_groups()
        assert result == (0, 0)

    def test_returns_zero_zero_when_no_resources_match(self, settings: Any) -> None:
        """A group whose prefixes match nothing yields (0, 0)."""
        from friese_mcp.apps import _install_dispatch_groups

        settings.FRIESE_MCP_DISPATCH_GROUPS = {"empty": ["doesnotexist"]}
        # Use a fresh isolated registry so the result is deterministic.
        fresh = ToolRegistry()
        with patch("friese_mcp.registry.tool_registry", fresh):
            result = _install_dispatch_groups()
        assert result == (0, 0)
