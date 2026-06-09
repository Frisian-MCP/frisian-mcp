"""Tests for ``_install_mcp_url()`` and ``_install_healthcheck_urls()``."""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import URLResolver, clear_url_caches, get_resolver

from frisian_mcp.apps import (
    _DEFAULT_HEALTHCHECK_PATHS,
    _HEALTHCHECK_AUTO_URL_ATTR,
    _MCP_AUTO_URL_ATTR,
    _install_healthcheck_urls,
    _install_mcp_url,
)

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
    def test_skips_when_frisian_mcp_urls_already_included(self, empty_urlconf: str) -> None:
        """Returns False when frisian_mcp.urls is already included (app_name match)."""
        from django.urls import include, re_path  # pylint: disable=import-outside-toplevel

        resolver = get_resolver()
        resolver.url_patterns.clear()
        explicit = re_path(r"^mcp/?", include("frisian_mcp.urls"))
        resolver.url_patterns.append(explicit)
        try:
            result = _install_mcp_url()
            assert result is False
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(
        ROOT_URLCONF="_test_empty_urlconf_fixture",
        FRISIAN_MCP_PATH="/api/mcp/",
    )
    def test_custom_path_strips_slashes(self, empty_urlconf: str) -> None:
        """FRISIAN_MCP_PATH is stripped of leading/trailing slashes before use."""
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


# ---------------------------------------------------------------------------
# _install_healthcheck_urls — healthcheck injection
# ---------------------------------------------------------------------------


class TestInstallHealthcheckUrls:
    """Tests for _install_healthcheck_urls()."""

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_returns_count_when_injected(self, empty_urlconf: str) -> None:
        """Returns the number of paths injected (1 for the default path)."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            result = _install_healthcheck_urls()
            assert result == 1
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_idempotent_second_call_returns_zero(self, empty_urlconf: str) -> None:
        """A second call returns 0 when the path is already registered."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_healthcheck_urls()
            result = _install_healthcheck_urls()
            assert result == 0
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_sentinel_attribute_set(self, empty_urlconf: str) -> None:
        """The injected pattern carries _HEALTHCHECK_AUTO_URL_ATTR with the clean path."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_healthcheck_urls()
            sentinels = [
                getattr(p, _HEALTHCHECK_AUTO_URL_ATTR, None) for p in resolver.url_patterns
            ]
            assert "backend/healthcheck" in sentinels
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    def test_returns_zero_when_no_root_urlconf(self) -> None:
        """Returns 0 without touching the resolver when ROOT_URLCONF is absent."""
        with override_settings():
            from django.conf import settings  # pylint: disable=import-outside-toplevel

            if hasattr(settings, "ROOT_URLCONF"):
                del settings.ROOT_URLCONF
            result = _install_healthcheck_urls()
        assert result == 0

    @override_settings(
        ROOT_URLCONF="_test_empty_urlconf_fixture",
        FRISIAN_MCP_HEALTHCHECK_PATHS=[],
    )
    def test_returns_zero_for_empty_paths(self, empty_urlconf: str) -> None:
        """Returns 0 when FRISIAN_MCP_HEALTHCHECK_PATHS is an empty list."""
        result = _install_healthcheck_urls()
        assert result == 0

    @override_settings(
        ROOT_URLCONF="_test_empty_urlconf_fixture",
        FRISIAN_MCP_HEALTHCHECK_PATHS=["custom/health", "api/ping"],
    )
    def test_custom_paths_all_injected(self, empty_urlconf: str) -> None:
        """Each path in FRISIAN_MCP_HEALTHCHECK_PATHS gets its own URL pattern."""
        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            result = _install_healthcheck_urls()
            assert result == 2
            sentinels = {
                getattr(p, _HEALTHCHECK_AUTO_URL_ATTR, None) for p in resolver.url_patterns
            }
            assert "custom/health" in sentinels
            assert "api/ping" in sentinels
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    @override_settings(ROOT_URLCONF="_test_empty_urlconf_fixture")
    def test_healthcheck_returns_200_ok(self, empty_urlconf: str) -> None:
        """GET /backend/healthcheck/ returns 200 with {status: ok}."""
        import json

        resolver = get_resolver()
        resolver.url_patterns.clear()
        try:
            _install_healthcheck_urls()
            client = Client()
            response = client.get("/backend/healthcheck/")
            assert response.status_code == 200
            body = json.loads(response.content)
            assert body == {"status": "ok"}
        finally:
            resolver.url_patterns.clear()
            clear_url_caches()

    def test_default_healthcheck_paths(self) -> None:
        """The default paths list contains the Grok-expected path."""
        assert "backend/healthcheck" in _DEFAULT_HEALTHCHECK_PATHS
