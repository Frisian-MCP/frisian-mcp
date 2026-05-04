"""Tests for ``_install_mcp_url()`` — auto-registration of McpView in the resolver."""

from __future__ import annotations

import pytest
from django.test import override_settings
from django.urls import URLResolver, clear_url_caches, get_resolver

from friese_mcp.apps import _MCP_AUTO_URL_ATTR, _install_mcp_url

# ---------------------------------------------------------------------------
# _install_mcp_url — basic injection
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_urlconf() -> str:
    """Register a minimal in-process URLconf with no patterns and return its name."""
    import sys
    import types

    mod = types.ModuleType("_test_empty_urlconf_fixture")
    mod.urlpatterns = []  # type: ignore[attr-defined]
    sys.modules["_test_empty_urlconf_fixture"] = mod
    return "_test_empty_urlconf_fixture"


class TestInstallMcpUrl:
    """Tests for _install_mcp_url()."""

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_returns_true_when_url_inserted(self, empty_urlconf: str) -> None:
        """_install_mcp_url() returns True when it injects the pattern."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            result = _install_mcp_url()
            assert result is True
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_pattern_inserted_at_position_zero(self, empty_urlconf: str) -> None:
        """The auto-registered resolver is inserted at index 0."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_mcp_url()
            assert len(resolver.url_patterns) >= 1
            assert isinstance(resolver.url_patterns[0], URLResolver)
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_sentinel_attribute_set_on_injected_resolver(self, empty_urlconf: str) -> None:
        """The injected URLResolver carries the sentinel attribute for idempotency."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_mcp_url()
            assert getattr(resolver.url_patterns[0], _MCP_AUTO_URL_ATTR, False) is True
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_idempotent_second_call_returns_false(self, empty_urlconf: str) -> None:
        """A second call returns False when the sentinel is already present."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_mcp_url()
            result = _install_mcp_url()
            assert result is False
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_idempotent_does_not_duplicate_pattern(self, empty_urlconf: str) -> None:
        """Calling _install_mcp_url() twice leaves exactly one auto-registered entry."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_mcp_url()
            _install_mcp_url()
            auto_count = sum(
                1 for p in resolver.url_patterns if getattr(p, _MCP_AUTO_URL_ATTR, False)
            )
            assert auto_count == 1
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    def test_returns_false_when_no_root_urlconf(self) -> None:
        """Returns False without touching the resolver when ROOT_URLCONF is absent."""
        with override_settings():
            from django.conf import settings  # pylint: disable=import-outside-toplevel

            if hasattr(settings, "ROOT_URLCONF"):
                del settings.ROOT_URLCONF
            result = _install_mcp_url()
        assert result is False

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_skips_when_friese_mcp_urls_already_included(self, empty_urlconf: str) -> None:
        """Returns False when friese_mcp.urls is already included (app_name match)."""
        from django.urls import include, re_path  # pylint: disable=import-outside-toplevel

        resolver = get_resolver()
        resolver.url_patterns.clear()
        explicit = re_path(r"^mcp/?", include("friese_mcp.urls"))
        resolver.url_patterns.append(explicit)
        try:
            result = _install_mcp_url()
            assert result is False
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(
        ROOT_URLCONF="_test_empty_urlconf_fixture",
        FRIESE_MCP_PATH="/api/mcp/",
    )
    def test_custom_path_strips_slashes(self, empty_urlconf: str) -> None:
        """FRIESE_MCP_PATH is stripped of leading/trailing slashes before use."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_mcp_url()
            pattern_str = str(resolver.url_patterns[0].pattern)
            assert "api" in pattern_str
            assert "mcp" in pattern_str
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()
