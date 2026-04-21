"""Tests for Mcp-Session-Id header on initialize responses."""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.http import StreamingHttpResponse
from django.test import RequestFactory, override_settings

from friese_mcp.views import McpEndpointView

_view = McpEndpointView.as_view()
_rf = RequestFactory()


def _call(method: str, params: dict[str, Any] | None = None, accept: str | None = None) -> Any:
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if accept is not None:
        kwargs["HTTP_ACCEPT"] = accept
    req = _rf.post(
        "/mcp/",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}),
        **kwargs,
    )
    req.user = AnonymousUser()
    return _view(req)


class TestMcpSessionIdHeader:
    """Tests for Mcp-Session-Id on the initialize response."""

    def test_initialize_includes_session_id_header_by_default(self) -> None:
        """Initialize response includes Mcp-Session-Id by default."""
        resp = _call("initialize")
        assert "Mcp-Session-Id" in resp

    def test_session_id_is_valid_uuid(self) -> None:
        """The Mcp-Session-Id value is a valid UUID v4."""
        resp = _call("initialize")
        value = resp["Mcp-Session-Id"]
        parsed = uuid.UUID(value)
        assert parsed.version == 4

    def test_each_initialize_returns_different_session_id(self) -> None:
        """Each initialize call generates a unique session ID."""
        ids = {_call("initialize")["Mcp-Session-Id"] for _ in range(5)}
        assert len(ids) == 5

    @override_settings(FRIESE_MCP_SESSION_ID_HEADER=False)
    def test_session_id_suppressed_when_setting_false(self) -> None:
        """Setting FRIESE_MCP_SESSION_ID_HEADER=False suppresses the header."""
        resp = _call("initialize")
        assert "Mcp-Session-Id" not in resp

    def test_non_initialize_methods_do_not_include_header(self) -> None:
        """Non-initialize methods do not include Mcp-Session-Id."""
        for method in ("ping", "tools/list"):
            resp = _call(method)
            assert "Mcp-Session-Id" not in resp, f"Unexpected header on {method}"

    def test_session_id_preserved_in_sse_response(self) -> None:
        """Mcp-Session-Id is present even when the response is SSE-wrapped."""
        resp = _call("initialize", accept="text/event-stream")
        assert isinstance(resp, StreamingHttpResponse)
        assert "Mcp-Session-Id" in resp
        assert uuid.UUID(resp["Mcp-Session-Id"]).version == 4
