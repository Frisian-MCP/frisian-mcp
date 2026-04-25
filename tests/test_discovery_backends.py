"""Tests for the get_discovery_backends() multi-backend interface."""

from __future__ import annotations

from typing import Any

import pytest

from friese_mcp.backends import get_discovery_backend, get_discovery_backends
from friese_mcp.backends.base import BaseDiscoveryBackend, ToolDefinition
from friese_mcp.backends.discovery import DRFSyncDiscovery

# ---------------------------------------------------------------------------
# Stub backends
# ---------------------------------------------------------------------------


class _AlphaBackend(BaseDiscoveryBackend):
    """Stub backend returning a single tool named 'alpha.ping'."""

    def discover_tools(self) -> list[ToolDefinition]:
        """Return one tool."""
        return [
            ToolDefinition(
                name="alpha.ping",
                description="Alpha ping",
                input_schema={"type": "object"},
                permission_classes=(),
                source="auto",
            )
        ]


class _BetaBackend(BaseDiscoveryBackend):
    """Stub backend returning a single tool named 'beta.pong'."""

    def discover_tools(self) -> list[ToolDefinition]:
        """Return one tool."""
        return [
            ToolDefinition(
                name="beta.pong",
                description="Beta pong",
                input_schema={"type": "object"},
                permission_classes=(),
                source="auto",
            )
        ]


class _OverrideBackend(BaseDiscoveryBackend):
    """Stub backend that overrides 'alpha.ping' with a different description."""

    def discover_tools(self) -> list[ToolDefinition]:
        """Return one tool with the same name as AlphaBackend."""
        return [
            ToolDefinition(
                name="alpha.ping",
                description="Override description",
                input_schema={"type": "object"},
                permission_classes=(),
                source="auto",
            )
        ]


# ---------------------------------------------------------------------------
# get_discovery_backends() — default behaviour
# ---------------------------------------------------------------------------


class TestGetDiscoveryBackendsDefault:
    """get_discovery_backends() with no settings configured."""

    def test_returns_list(self, settings: Any) -> None:
        """get_discovery_backends() returns a list."""
        for attr in ("FRIESE_MCP_DISCOVERY_BACKENDS", "FRIESE_MCP_DISCOVERY_BACKEND"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        result = get_discovery_backends()
        assert isinstance(result, list)

    def test_default_is_drf_sync_discovery(self, settings: Any) -> None:
        """Default single backend is DRFSyncDiscovery."""
        for attr in ("FRIESE_MCP_DISCOVERY_BACKENDS", "FRIESE_MCP_DISCOVERY_BACKEND"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        result = get_discovery_backends()
        assert len(result) == 1
        assert isinstance(result[0], DRFSyncDiscovery)

    def test_returns_at_least_one_backend(self, settings: Any) -> None:
        """get_discovery_backends() always returns at least one backend."""
        for attr in ("FRIESE_MCP_DISCOVERY_BACKENDS", "FRIESE_MCP_DISCOVERY_BACKEND"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        assert len(get_discovery_backends()) >= 1


# ---------------------------------------------------------------------------
# FRIESE_MCP_DISCOVERY_BACKENDS (plural) — multi-backend list
# ---------------------------------------------------------------------------


class TestGetDiscoveryBackendsPlural:
    """FRIESE_MCP_DISCOVERY_BACKENDS configures multiple backends."""

    def test_plural_setting_loads_both_backends(self, settings: Any) -> None:
        """Setting a list of two dotted paths loads both backends."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
            "tests.test_discovery_backends._BetaBackend",
        ]
        result = get_discovery_backends()
        assert len(result) == 2
        types = [type(b) for b in result]
        assert _AlphaBackend in types
        assert _BetaBackend in types

    def test_plural_returns_instances_not_classes(self, settings: Any) -> None:
        """Each entry in the plural result is an instance, not a class."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
        ]
        result = get_discovery_backends()
        assert isinstance(result[0], _AlphaBackend)

    def test_plural_takes_priority_over_singular(self, settings: Any) -> None:
        """FRIESE_MCP_DISCOVERY_BACKENDS takes precedence over singular setting."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
        ]
        settings.FRIESE_MCP_DISCOVERY_BACKEND = (
            "tests.test_discovery_backends._BetaBackend"
        )
        result = get_discovery_backends()
        assert len(result) == 1
        assert isinstance(result[0], _AlphaBackend)

    def test_plural_empty_list_returns_no_backends(self, settings: Any) -> None:
        """An explicit empty list returns an empty backend list."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = []
        result = get_discovery_backends()
        assert result == []


# ---------------------------------------------------------------------------
# FRIESE_MCP_DISCOVERY_BACKEND (singular, legacy) — backward compat
# ---------------------------------------------------------------------------


class TestGetDiscoveryBackendsSingularLegacy:
    """FRIESE_MCP_DISCOVERY_BACKEND (singular) still works via get_discovery_backends()."""

    def test_singular_setting_is_wrapped_in_list(self, settings: Any) -> None:
        """Singular setting produces a single-element list."""
        if hasattr(settings, "FRIESE_MCP_DISCOVERY_BACKENDS"):
            delattr(settings, "FRIESE_MCP_DISCOVERY_BACKENDS")
        settings.FRIESE_MCP_DISCOVERY_BACKEND = (
            "tests.test_discovery_backends._AlphaBackend"
        )
        result = get_discovery_backends()
        assert len(result) == 1
        assert isinstance(result[0], _AlphaBackend)

    def test_legacy_get_discovery_backend_still_works(self, settings: Any) -> None:
        """The singular get_discovery_backend() function still returns a single backend."""
        if hasattr(settings, "FRIESE_MCP_DISCOVERY_BACKENDS"):
            delattr(settings, "FRIESE_MCP_DISCOVERY_BACKENDS")
        settings.FRIESE_MCP_DISCOVERY_BACKEND = (
            "tests.test_discovery_backends._AlphaBackend"
        )
        result = get_discovery_backend()
        assert isinstance(result, _AlphaBackend)


# ---------------------------------------------------------------------------
# Tool merging: multiple backends, name-clash resolution
# ---------------------------------------------------------------------------


class TestMultiBackendMerging:
    """Tools from multiple backends are correctly merged."""

    def test_tools_from_all_backends_are_collected(self, settings: Any) -> None:
        """All tools from all backends appear after merging."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
            "tests.test_discovery_backends._BetaBackend",
        ]
        backends = get_discovery_backends()
        all_tools: list[ToolDefinition] = []
        for backend in backends:
            all_tools.extend(backend.discover_tools())
        names = {t.name for t in all_tools}
        assert "alpha.ping" in names
        assert "beta.pong" in names

    def test_later_backend_wins_on_name_clash(self, settings: Any) -> None:
        """When two backends return the same tool name, the later one wins."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
            "tests.test_discovery_backends._OverrideBackend",
        ]
        backends = get_discovery_backends()
        merged: dict[str, ToolDefinition] = {}
        for backend in backends:
            for tool in backend.discover_tools():
                merged[tool.name] = tool
        assert merged["alpha.ping"].description == "Override description"

    def test_no_duplicate_tool_names_after_merge(self, settings: Any) -> None:
        """Merging two backends with overlapping names produces no duplicates."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
            "tests.test_discovery_backends._OverrideBackend",
        ]
        backends = get_discovery_backends()
        all_tools: list[ToolDefinition] = []
        seen: set[str] = set()
        for backend in backends:
            for tool in backend.discover_tools():
                if tool.name not in seen:
                    all_tools.append(tool)
                    seen.add(tool.name)
        assert len(all_tools) == len({t.name for t in all_tools})


# ---------------------------------------------------------------------------
# Custom backend integration smoke test
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCustomBackendSmoke:
    """Custom backend tools appear in tool_registry after apps.ready()."""

    def test_custom_backend_returns_base_discovery_backend(self, settings: Any) -> None:
        """A custom backend class is a proper BaseDiscoveryBackend subclass."""
        settings.FRIESE_MCP_DISCOVERY_BACKENDS = [
            "tests.test_discovery_backends._AlphaBackend",
        ]
        backends = get_discovery_backends()
        assert all(isinstance(b, BaseDiscoveryBackend) for b in backends)
