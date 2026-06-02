"""Tests for SSE (Server-Sent Events) support in the MCP gateway."""

from __future__ import annotations

import json
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse, StreamingHttpResponse
from django.test import RequestFactory

from frisian_mcp.views import McpView, _maybe_sse

_view = McpView.as_view()
_rf = RequestFactory()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(accept: str | None = None) -> Any:
    """Build a POST request for a ping JSON-RPC call."""
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if accept is not None:
        kwargs["HTTP_ACCEPT"] = accept
    req = _rf.post(
        "/mcp/",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        **kwargs,
    )
    req.user = AnonymousUser()
    return req


def _stream_body(response: StreamingHttpResponse) -> str:
    """Consume the streaming response and return the full body string."""
    return b"".join(response.streaming_content).decode("utf-8")


# ---------------------------------------------------------------------------
# TestMaybeSse — unit tests for the helper
# ---------------------------------------------------------------------------


class TestMaybeSse:
    """Unit tests for _maybe_sse."""

    def test_non_json_response_returned_unchanged(self) -> None:
        """Non-JsonResponse (e.g. HttpResponse) is returned as-is."""
        from django.http import HttpResponse

        plain = HttpResponse(status=202)
        req = _post()
        result = _maybe_sse(plain, req)
        assert result is plain

    def test_no_accept_header_returns_json_response(self) -> None:
        """Without Accept: text/event-stream, JsonResponse is returned unchanged."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post()
        result = _maybe_sse(resp, req)
        assert result is resp
        assert isinstance(result, JsonResponse)

    def test_wrong_accept_returns_json_response(self) -> None:
        """Accept: application/json does not trigger SSE."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="application/json")
        result = _maybe_sse(resp, req)
        assert result is resp

    def test_sse_accept_returns_streaming_response(self) -> None:
        """Accept: text/event-stream returns StreamingHttpResponse."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        assert isinstance(result, StreamingHttpResponse)

    def test_sse_content_type_header(self) -> None:
        """SSE response has Content-Type: text/event-stream."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        assert "text/event-stream" in result["Content-Type"]

    def test_sse_cache_control_header(self) -> None:
        """SSE response has Cache-Control: no-cache."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        assert result["Cache-Control"] == "no-cache"

    def test_sse_body_has_data_prefix(self) -> None:
        """SSE body line starts with 'data: '."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        body = _stream_body(result)  # type: ignore[arg-type]
        assert body.startswith("data: ")

    def test_sse_body_ends_with_double_newline(self) -> None:
        """SSE body ends with the double-newline event delimiter."""
        resp = JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        body = _stream_body(result)  # type: ignore[arg-type]
        assert body.endswith("\n\n")

    def test_sse_body_contains_valid_json(self) -> None:
        """The data payload is valid JSON."""
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        resp = JsonResponse(payload)
        req = _post(accept="text/event-stream")
        result = _maybe_sse(resp, req)
        body = _stream_body(result)  # type: ignore[arg-type]
        # Strip 'data: ' prefix and trailing '\n\n'
        json_str = body.removeprefix("data: ").rstrip("\n")
        parsed = json.loads(json_str)
        assert parsed == payload

    def test_sse_json_matches_original_response(self) -> None:
        """SSE payload matches what the JSON response would have contained."""
        original_data = {"jsonrpc": "2.0", "id": 42, "result": {"ping": "pong"}}
        resp = JsonResponse(original_data)
        original_content = json.loads(resp.content)

        req = _post(accept="text/event-stream")
        sse_result = _maybe_sse(resp, req)
        body = _stream_body(sse_result)  # type: ignore[arg-type]
        json_str = body.removeprefix("data: ").rstrip("\n")
        assert json.loads(json_str) == original_content


# ---------------------------------------------------------------------------
# TestSseIntegration — end-to-end through McpView
# ---------------------------------------------------------------------------


class TestSseIntegration:
    """End-to-end SSE tests through the view."""

    def test_ping_without_sse_accept_returns_json(self) -> None:
        """Ping without SSE Accept header returns a normal JsonResponse."""
        resp = _view(_post())
        assert isinstance(resp, JsonResponse)

    def test_ping_with_sse_accept_returns_streaming(self) -> None:
        """Ping with Accept: text/event-stream returns StreamingHttpResponse."""
        resp = _view(_post(accept="text/event-stream"))
        assert isinstance(resp, StreamingHttpResponse)

    def test_sse_ping_result_is_correct(self) -> None:
        """Ping SSE response contains the correct JSON-RPC result."""
        resp = _view(_post(accept="text/event-stream"))
        body = _stream_body(resp)  # type: ignore[arg-type]
        json_str = body.removeprefix("data: ").rstrip("\n")
        data = json.loads(json_str)
        assert data["result"] == {}
        assert data["id"] == 1

    def test_notification_202_not_wrapped_as_sse(self) -> None:
        """HTTP 202 notification responses are never SSE-wrapped."""
        req = _rf.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "method": "initialized"}),
            content_type="application/json",
            HTTP_ACCEPT="text/event-stream",
        )
        req.user = AnonymousUser()
        resp = _view(req)
        assert resp.status_code == 202
        assert not isinstance(resp, StreamingHttpResponse)
