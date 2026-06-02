"""Tests for FRISIAN_MCP_TOOLS_LIST_CACHE_TTL — optional tools/list caching."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp import invalidate_tools_list_cache
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import _TOOLS_LIST_CACHE_KEY, McpView

_rf = RequestFactory()
_view = McpView.as_view()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _anon_user() -> AnonymousUser:
    return AnonymousUser()


def _tools_list_request() -> Any:
    req = _rf.post(
        "/mcp/",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        content_type="application/json",
    )
    req.user = _anon_user()
    return req


def _isolated_registry(*tool_names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(name, lambda a, r: {"ok": True}, f"Tool {name}", {})
    return reg


# ---------------------------------------------------------------------------
# No-cache default behaviour (FRISIAN_MCP_TOOLS_LIST_CACHE_TTL absent)
# ---------------------------------------------------------------------------


class TestToolsListNoCacheDefault:
    """Without FRISIAN_MCP_TOOLS_LIST_CACHE_TTL, cache is never used."""

    def test_no_cache_setting_does_not_call_cache_get(self) -> None:
        """list_tools() is called directly when FRISIAN_MCP_TOOLS_LIST_CACHE_TTL is absent."""
        reg = _isolated_registry("alpha.ping")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            _view(_tools_list_request())
        mock_cache.get.assert_not_called()

    def test_no_cache_returns_tools(self) -> None:
        """Without caching, tools are still returned correctly."""
        reg = _isolated_registry("alpha.ping")
        with patch("frisian_mcp.views.tool_registry", reg):
            resp = _view(_tools_list_request())
        data = json.loads(resp.content)
        names = [t["name"] for t in data["result"]["tools"]]
        assert "alpha.ping" in names


# ---------------------------------------------------------------------------
# Cache enabled — FRISIAN_MCP_TOOLS_LIST_CACHE_TTL set
# ---------------------------------------------------------------------------


class TestToolsListCacheEnabled:
    """FRISIAN_MCP_TOOLS_LIST_CACHE_TTL enables cache read/write."""

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_cache_miss_calls_list_tools_and_populates_cache(self) -> None:
        """On a cache miss, list_tools() is called and result stored with correct TTL."""
        reg = _isolated_registry("beta.call")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _view(_tools_list_request())
        mock_cache.get.assert_called_once_with(f"{_TOOLS_LIST_CACHE_KEY}:read")
        mock_cache.set.assert_called_once()
        args = mock_cache.set.call_args
        assert args[0][0] == f"{_TOOLS_LIST_CACHE_KEY}:read"
        assert args[0][2] == 60

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_cache_hit_skips_list_tools(self) -> None:
        """On a cache hit, list_tools() is not called (no registry read)."""
        reg = _isolated_registry("gamma.go")
        cached_tools = [{"name": "cached.tool", "description": "From cache", "inputSchema": {}}]
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
            patch.object(reg, "list_tools") as mock_list,
        ):
            mock_cache.get.return_value = cached_tools
            resp = _view(_tools_list_request())
        mock_list.assert_not_called()
        data = json.loads(resp.content)
        names = [t["name"] for t in data["result"]["tools"]]
        assert "cached.tool" in names

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_cache_key_is_correct(self) -> None:
        """Cache reads use the _TOOLS_LIST_CACHE_KEY constant."""
        reg = _isolated_registry()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _view(_tools_list_request())
        mock_cache.get.assert_called_once_with(f"{_TOOLS_LIST_CACHE_KEY}:read")

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_cache_ttl_matches_setting(self) -> None:
        """Cache is set with TTL equal to FRISIAN_MCP_TOOLS_LIST_CACHE_TTL."""
        reg = _isolated_registry("tool.a")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _view(_tools_list_request())
        _, _, ttl = mock_cache.set.call_args[0]
        assert ttl == 60

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=300)
    def test_different_ttl_is_forwarded(self) -> None:
        """A different FRISIAN_MCP_TOOLS_LIST_CACHE_TTL value is forwarded to cache.set."""
        reg = _isolated_registry("tool.b")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _view(_tools_list_request())
        _, _, ttl = mock_cache.set.call_args[0]
        assert ttl == 300

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_cache_not_set_on_cache_hit(self) -> None:
        """cache.set is not called when cache.get returns a hit."""
        reg = _isolated_registry("tool.c")
        cached_tools = [{"name": "cached.tool", "description": "", "inputSchema": {}}]
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = cached_tools
            _view(_tools_list_request())
        mock_cache.set.assert_not_called()


# ---------------------------------------------------------------------------
# Per-agent filtering bypasses cache
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestToolsListCacheBypassForAgentFilter:
    """Cache is bypassed when per-agent allowed_tools filtering is active."""

    @override_settings(
        FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60,
        FRISIAN_MCP_AUTHENTICATION_CLASSES=[
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication"
        ],
        FRISIAN_MCP_PERMISSION_CLASSES=[],
    )
    def test_filtered_response_does_not_read_cache(self) -> None:
        """When an AgentConnection has allowed_tools, cache.get is not called."""
        from frisian_mcp.contrib.agents.models import (  # pylint: disable=import-outside-toplevel
            AgentConnection,
        )
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        token = FrisianMcpToken.objects.create(name="tok")
        AgentConnection.objects.create(
            name="filtered-agent",
            token=token,
            allowed_tools=["alpha.ping"],
        )
        reg = _isolated_registry("alpha.ping", "beta.pong")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            req = _rf.post(
                "/mcp/",
                data=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token.plaintext_token}",
            )
            _view(req)
        mock_cache.get.assert_not_called()

    @override_settings(FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=60)
    def test_null_allowed_tools_uses_cache(self) -> None:
        """An AgentConnection with allowed_tools=None does not bypass the cache."""
        from frisian_mcp.contrib.agents.models import (  # pylint: disable=import-outside-toplevel
            AgentConnection,
        )
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        token = FrisianMcpToken.objects.create(name="tok2")
        AgentConnection.objects.create(
            name="unrestricted-agent",
            token=token,
            allowed_tools=None,
        )
        reg = _isolated_registry("alpha.ping")
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
            override_settings(
                FRISIAN_MCP_AUTHENTICATION_CLASSES=[
                    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication"
                ],
                FRISIAN_MCP_PERMISSION_CLASSES=[],
            ),
        ):
            mock_cache.get.return_value = None
            req = _rf.post(
                "/mcp/",
                data=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token.plaintext_token}",
            )
            _view(req)
        mock_cache.get.assert_called_once_with(f"{_TOOLS_LIST_CACHE_KEY}:read_write")


# ---------------------------------------------------------------------------
# Public invalidation helper
# ---------------------------------------------------------------------------


class TestInvalidateToolsListCache:
    """invalidate_tools_list_cache() deletes the tools/list cache entry."""

    def test_calls_cache_delete_many_with_all_tier_keys(self) -> None:
        """cache.delete_many is called with all per-tier keys."""
        with patch("frisian_mcp.views.django_cache") as mock_cache:
            invalidate_tools_list_cache()
        mock_cache.delete_many.assert_called_once()
        deleted_keys = mock_cache.delete_many.call_args[0][0]
        assert f"{_TOOLS_LIST_CACHE_KEY}:all" in deleted_keys
        assert f"{_TOOLS_LIST_CACHE_KEY}:read" in deleted_keys
        assert f"{_TOOLS_LIST_CACHE_KEY}:read_write" in deleted_keys
        assert f"{_TOOLS_LIST_CACHE_KEY}:admin" in deleted_keys

    def test_importable_from_package_root(self) -> None:
        """invalidate_tools_list_cache is exported from the frisian_mcp package."""
        import frisian_mcp  # pylint: disable=import-outside-toplevel

        assert callable(frisian_mcp.invalidate_tools_list_cache)
