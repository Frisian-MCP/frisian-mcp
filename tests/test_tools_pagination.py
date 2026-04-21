"""Tests for cursor-based pagination in tools/list."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from friese_mcp.protocol import INVALID_PARAMS
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView, _decode_cursor, _encode_cursor

_view = McpEndpointView.as_view()
_rf = RequestFactory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(n: int) -> ToolRegistry:
    """Return a fresh registry with *n* dummy tools named tool_0 .. tool_{n-1}."""
    reg = ToolRegistry()
    for i in range(n):
        reg.register(
            name=f"tool_{i}",
            fn=lambda arguments, request: {},
            description=f"Tool {i}",
            input_schema={"type": "object", "properties": {}},
        )
    return reg


def _call(params: dict[str, Any] | None, registry: ToolRegistry) -> Any:
    req = _rf.post(
        "/mcp/",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params or {}}
        ),
        content_type="application/json",
    )
    req.user = AnonymousUser()
    with patch("friese_mcp.views.tool_registry", registry):
        resp = _view(req)
    return json.loads(resp.content)


# ---------------------------------------------------------------------------
# TestCursorHelpers
# ---------------------------------------------------------------------------


class TestCursorHelpers:
    """Unit tests for _encode_cursor and _decode_cursor."""

    def test_encode_decode_roundtrip(self) -> None:
        """Encoding then decoding an offset returns the original value."""
        for offset in (0, 1, 20, 999):
            assert _decode_cursor(_encode_cursor(offset)) == offset

    def test_encoded_is_base64url(self) -> None:
        """Encoded cursor is valid base64url."""
        encoded = _encode_cursor(42)
        base64.urlsafe_b64decode(encoded.encode())  # must not raise

    def test_invalid_cursor_raises_value_error(self) -> None:
        """_decode_cursor raises ValueError for non-integer payloads."""
        import pytest

        with pytest.raises(ValueError, match="Invalid cursor"):
            _decode_cursor("not-valid-base64!!!")


# ---------------------------------------------------------------------------
# TestToolsListNoPagination
# ---------------------------------------------------------------------------


class TestToolsListNoPagination:
    """Without FRIESE_MCP_TOOLS_PAGE_SIZE, existing behaviour is unchanged."""

    def test_no_page_size_returns_all_tools(self) -> None:
        """Without page size, all tools are returned."""
        reg = _make_registry(5)
        data = _call({}, reg)
        assert len(data["result"]["tools"]) == 5

    def test_no_page_size_no_next_cursor(self) -> None:
        """Without page size, nextCursor is absent."""
        reg = _make_registry(5)
        data = _call({}, reg)
        assert "nextCursor" not in data["result"]

    def test_empty_registry_no_pagination(self) -> None:
        """Empty registry with no page size returns empty tools list."""
        data = _call({}, _make_registry(0))
        assert data["result"]["tools"] == []
        assert "nextCursor" not in data["result"]


# ---------------------------------------------------------------------------
# TestToolsListWithPagination
# ---------------------------------------------------------------------------


class TestToolsListWithPagination:
    """With FRIESE_MCP_TOOLS_PAGE_SIZE set, pagination is applied."""

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_first_page_returns_n_tools(self) -> None:
        """First page returns exactly page_size tools."""
        reg = _make_registry(7)
        data = _call({}, reg)
        assert len(data["result"]["tools"]) == 3

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_first_page_includes_next_cursor(self) -> None:
        """First page includes nextCursor when more tools exist."""
        reg = _make_registry(7)
        data = _call({}, reg)
        assert "nextCursor" in data["result"]

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_second_page_using_cursor(self) -> None:
        """Using nextCursor from page 1 returns the next page."""
        reg = _make_registry(7)
        page1 = _call({}, reg)
        cursor = page1["result"]["nextCursor"]
        page2 = _call({"cursor": cursor}, reg)
        assert len(page2["result"]["tools"]) == 3
        # Names should not overlap with page 1
        names1 = {t["name"] for t in page1["result"]["tools"]}
        names2 = {t["name"] for t in page2["result"]["tools"]}
        assert names1.isdisjoint(names2)

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_last_page_has_no_next_cursor(self) -> None:
        """The final page does not include nextCursor."""
        reg = _make_registry(6)
        page1 = _call({}, reg)
        cursor = page1["result"]["nextCursor"]
        page2 = _call({"cursor": cursor}, reg)
        assert "nextCursor" not in page2["result"]

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_all_tools_covered_across_pages(self) -> None:
        """Iterating all pages returns every tool exactly once."""
        reg = _make_registry(7)
        all_names: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            data = _call(params, reg)
            all_names.extend(t["name"] for t in data["result"]["tools"])
            cursor = data["result"].get("nextCursor")
            if cursor is None:
                break
        assert len(all_names) == 7
        assert len(set(all_names)) == 7

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=10)
    def test_page_size_larger_than_registry(self) -> None:
        """Page size larger than tool count returns all tools, no nextCursor."""
        reg = _make_registry(3)
        data = _call({}, reg)
        assert len(data["result"]["tools"]) == 3
        assert "nextCursor" not in data["result"]

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=5)
    def test_empty_registry_with_pagination(self) -> None:
        """Empty registry with pagination returns empty list, no nextCursor."""
        data = _call({}, _make_registry(0))
        assert data["result"]["tools"] == []
        assert "nextCursor" not in data["result"]

    @override_settings(FRIESE_MCP_TOOLS_PAGE_SIZE=3)
    def test_invalid_cursor_returns_invalid_params(self) -> None:
        """An invalid cursor string returns INVALID_PARAMS error."""
        reg = _make_registry(5)
        data = _call({"cursor": "!!not-valid!!"}, reg)
        assert "error" in data
        assert data["error"]["code"] == INVALID_PARAMS
