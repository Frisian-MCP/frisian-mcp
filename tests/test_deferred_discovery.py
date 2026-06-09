"""
PKG-21 — auto-discovery deferred until after all AppConfig.ready() hooks complete.

Django's INSTALLED_APPS ordering means a plugin appended after frisian_mcp
runs its own ``ready()`` (and registers URL patterns) AFTER frisian_mcp's
``ready()`` has already scanned the URL tree.  PKG-21 moves the scan into a
one-shot ``request_started`` signal handler so plugin URL patterns are
visible by the time discovery runs.
"""

# pylint: disable=redefined-outer-name,protected-access,import-outside-toplevel
# pylint: disable=unused-argument  # fixtures with side-effects (registry / signal-cleanup)
from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.signals import request_started
from django.test import RequestFactory, override_settings

from frisian_mcp.apps import FrisianMcpConfig
from frisian_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Stubs and fixtures
# ---------------------------------------------------------------------------


class _StubInvocation:
    """Minimal invocation backend stub — never invoked in these tests."""

    def invoke(
        self, tool_def: Any, arguments: dict[str, Any], request: Any
    ) -> Any:  # pragma: no cover
        """Never called in these tests; real backend is patched at the call site."""
        raise NotImplementedError


@pytest.fixture()
def fresh_app_config() -> Generator[FrisianMcpConfig, None, None]:
    """
    Yield FrisianMcpConfig with both idempotency flags reset for the test.

    Also disconnects any deferred-discovery handler at setup AND teardown
    via the stable ``_DEFERRED_DISCOVERY_UID``.  Without this, a handler
    connected by a previous test (and not yet fired) would still receive
    ``request_started.send()`` calls from subsequent tests, polluting the
    registry and call counters.
    """
    from frisian_mcp.apps import _DEFERRED_DISCOVERY_UID

    config = apps.get_app_config("frisian_mcp")
    assert isinstance(config, FrisianMcpConfig)
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
    """Patch the module-level tool_registry so each test starts clean."""
    fresh = ToolRegistry()
    with patch("frisian_mcp.registry.tool_registry", fresh):
        yield fresh


def _fire_deferred_only() -> None:
    """
    Fire ``request_started`` to only the frisian-mcp deferred-discovery handler.

    Django ships built-in ``request_started`` receivers (e.g.
    ``close_old_connections``) that may fail on test DB state when triggered
    outside a real request lifecycle.  These tests only care that the
    frisian-mcp handler runs, so we temporarily filter the receiver list to
    just the one identified by :data:`_DEFERRED_DISCOVERY_UID`, fire, and
    restore.  This keeps the test focused on PKG-21's behaviour without
    dragging unrelated Django machinery in.
    """
    from frisian_mcp.apps import _DEFERRED_DISCOVERY_UID

    saved = list(request_started.receivers)
    # Django stores ``connect(dispatch_uid=X)`` with lookup_key
    # ``(X, _make_id(sender))`` — the dispatch_uid string itself, NOT its
    # hash.  Match by lookup[0] alone (the dispatch_uid slot) since we
    # don't constrain sender; this is robust across Django versions where
    # the receivers tuple width varies.
    request_started.receivers = [entry for entry in saved if entry[0][0] == _DEFERRED_DISCOVERY_UID]
    request_started.sender_receivers_cache.clear()
    try:
        request_started.send(sender=None)
    finally:
        request_started.receivers = saved
        request_started.sender_receivers_cache.clear()


# ---------------------------------------------------------------------------
# Discovery is NOT run during ready()
# ---------------------------------------------------------------------------


class TestDiscoveryDeferredFromReady:
    """ready() must not scan the URL tree — that work is now deferred."""

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=True)
    def test_ready_does_not_invoke_discovery_backends(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
    ) -> None:
        """No discover_tools() call should fire during ready() itself."""
        from frisian_mcp.backends.base import ToolDefinition

        calls: list[str] = []

        class _TrackingBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                """Record the call and return an empty discovery."""
                calls.append("discover_tools")
                return []

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_TrackingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()

        # ready() must NOT have run discovery.
        assert not calls
        # The registry must therefore still be empty.
        assert len(isolated_registry.list_names()) == 0

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=True)
    def test_first_request_runs_discovery(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
        rf: RequestFactory,
    ) -> None:
        """A request_started signal triggers the deferred discovery exactly once."""
        from frisian_mcp.backends.base import ToolDefinition

        calls: list[str] = []

        class _TrackingBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                """Record the call and return one fixed tool."""
                calls.append("discover_tools")
                return [
                    ToolDefinition(
                        name="late.list",
                        description="discovered after ready()",
                        input_schema={"type": "object", "properties": {}},
                        permission_classes=(),
                        source="auto",
                        permission_tier="read",
                    )
                ]

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_TrackingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()
            # Simulate Django's per-request signal at the start of the next request.
            _fire_deferred_only()

        assert calls == ["discover_tools"]
        names = {t["name"] for t in isolated_registry.list_tools()}
        assert "late.list" in names

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=True)
    def test_subsequent_requests_do_not_redrive_discovery(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
        rf: RequestFactory,
    ) -> None:
        """The signal handler disconnects after firing — only the first request scans."""
        from frisian_mcp.backends.base import ToolDefinition

        calls: list[str] = []

        class _TrackingBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                """Record the call and return an empty discovery."""
                calls.append("discover_tools")
                return []

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_TrackingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()
            _fire_deferred_only()
            _fire_deferred_only()
            _fire_deferred_only()

        # Discovery scans exactly once, regardless of how many requests fire.
        assert calls == ["discover_tools"]


# ---------------------------------------------------------------------------
# The PKG-21 regression: late-loaded URL patterns are visible
# ---------------------------------------------------------------------------


class TestLateRegisteredUrlsDiscovered:
    """
    The exact symptom PKG-21 fixes: a plugin appended after frisian_mcp.

    In production this is any host plugin loader that appends to
    INSTALLED_APPS after the host's settings module has appended
    frisian_mcp.  Plugin AppConfig.ready() runs AFTER ours and registers
    URL patterns there.  Pre-PKG-21, those URLs were not in the resolver
    tree when our scan ran, so the tools were silently absent from
    tools/list.
    """

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=True)
    def test_url_pattern_added_after_ready_is_discovered_on_first_request(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
        rf: RequestFactory,
    ) -> None:
        """Simulate a plugin registering tools AFTER frisian_mcp.ready() has run."""
        from frisian_mcp.backends.base import ToolDefinition

        # Stage 1: at ready() time, no plugin tools are visible.
        plugin_tools: list[ToolDefinition] = []

        class _LateBindingBackend:
            """Returns whatever plugin_tools holds at the moment of the call."""

            def discover_tools(self) -> list[ToolDefinition]:
                """Snapshot plugin_tools at call time, simulating late URL binding."""
                return list(plugin_tools)

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_LateBindingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()

            # Stage 2: a plugin's later AppConfig.ready() registers its tools.
            plugin_tools.append(
                ToolDefinition(
                    name="plugin_resource.list",
                    description="late-bound plugin tool",
                    input_schema={"type": "object", "properties": {}},
                    permission_classes=(),
                    source="auto",
                    permission_tier="read",
                )
            )

            # Stage 3: first request arrives — discovery runs NOW, sees the plugin.
            _fire_deferred_only()

        names = {t["name"] for t in isolated_registry.list_tools()}
        assert "plugin_resource.list" in names, (
            "PKG-21 regression: late-bound plugin tools must appear once any " "request fires"
        )


# ---------------------------------------------------------------------------
# Idempotency of the deferred path
# ---------------------------------------------------------------------------


class TestDeferredDiscoveryIdempotency:
    """_run_deferred_discovery() and the signal handler must be idempotent."""

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=True)
    def test_direct_call_to_run_deferred_discovery_is_idempotent(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
    ) -> None:
        """Calling _run_deferred_discovery() twice does not duplicate work."""
        from frisian_mcp.backends.base import ToolDefinition

        calls: list[str] = []

        class _TrackingBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                """Record the call and return an empty discovery."""
                calls.append("discover_tools")
                return []

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_TrackingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()
            fresh_app_config._run_deferred_discovery()
            fresh_app_config._run_deferred_discovery()
            fresh_app_config._run_deferred_discovery()

        assert calls == ["discover_tools"]


# ---------------------------------------------------------------------------
# AUTODISCOVER=False short-circuits before connecting the handler
# ---------------------------------------------------------------------------


class TestAutodiscoverDisabled:
    """When FRISIAN_MCP_AUTODISCOVER is False, no signal handler is connected."""

    @override_settings(FRISIAN_MCP_ENABLED=True, FRISIAN_MCP_AUTODISCOVER=False)
    def test_no_discovery_when_autodiscover_disabled(
        self,
        fresh_app_config: FrisianMcpConfig,
        isolated_registry: ToolRegistry,
        rf: RequestFactory,
    ) -> None:
        """A request must NOT trigger discovery when AUTODISCOVER is disabled."""
        from frisian_mcp.backends.base import ToolDefinition

        calls: list[str] = []

        class _TrackingBackend:
            def discover_tools(self) -> list[ToolDefinition]:
                """Record the call and return an empty discovery."""
                calls.append("discover_tools")
                return []

        with (
            patch(
                "frisian_mcp.backends.get_discovery_backends",
                return_value=[_TrackingBackend()],
            ),
            patch(
                "frisian_mcp.backends.get_invocation_backend",
                return_value=_StubInvocation(),
            ),
        ):
            fresh_app_config.ready()
            _fire_deferred_only()

        assert not calls


# ---------------------------------------------------------------------------
# rf fixture local to this module so it doesn't depend on other test files
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()
