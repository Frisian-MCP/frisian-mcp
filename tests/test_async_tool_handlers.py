"""Tests for async tool handler support in ToolRegistry.dispatch()."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from friese_mcp.registry import ToolInputError, ToolNotFoundError, ToolRegistry
from friese_mcp.views import McpView

_rf = RequestFactory()
_view = McpView.as_view()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _anon_request() -> Any:
    req = MagicMock()
    req.user = AnonymousUser()
    req.auth = None
    return req


def _tools_call_request(tool_name: str, arguments: dict[str, Any]) -> Any:
    req = _rf.post(
        "/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        ),
        content_type="application/json",
    )
    req.user = AnonymousUser()
    return req


# ---------------------------------------------------------------------------
# ToolRegistry.dispatch() — async handler detection and bridging
# ---------------------------------------------------------------------------


class TestAsyncToolHandlerDispatch:
    """ToolRegistry.dispatch() bridges async def tool handlers via async_to_sync."""

    def test_sync_handler_still_works(self) -> None:
        """Existing sync handlers are unaffected by the async detection path."""
        reg = ToolRegistry()
        reg.register("sync.tool", lambda args, _req: {"result": "sync"}, "Sync", {})
        result = reg.dispatch(_anon_request(), "sync.tool", {})
        assert result == {"result": "sync"}

    def test_async_handler_returns_result(self) -> None:
        """An async def handler is called and its return value is returned synchronously."""
        reg = ToolRegistry()

        async def _handler(_args: dict, _req: Any) -> dict:
            return {"result": "async"}

        reg.register("async.tool", _handler, "Async", {})
        result = reg.dispatch(_anon_request(), "async.tool", {})
        assert result == {"result": "async"}

    def test_async_handler_can_await_coroutine(self) -> None:
        """An async handler that awaits an internal coroutine returns its result."""
        reg = ToolRegistry()

        async def _inner() -> str:
            return "awaited"

        async def _handler(_args: dict, _req: Any) -> dict:
            return {"value": await _inner()}

        reg.register("async.await", _handler, "Await", {})
        result = reg.dispatch(_anon_request(), "async.await", {})
        assert result == {"value": "awaited"}

    def test_async_handler_receives_arguments_and_request(self) -> None:
        """Arguments and request are forwarded correctly to async handlers."""
        reg = ToolRegistry()
        received: list[tuple[Any, Any]] = []

        async def _capture(args: dict, req: Any) -> dict:
            received.append((args, req))
            return {}

        expected_req = _anon_request()
        reg.register(
            "async.capture",
            _capture,
            "Capture",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        reg.dispatch(expected_req, "async.capture", {"x": 42})
        assert len(received) == 1
        assert received[0][0] == {"x": 42}
        assert received[0][1] is expected_req

    def test_async_handler_is_detected_as_coroutine_function(self) -> None:
        """asyncio.iscoroutinefunction correctly identifies async handlers."""

        async def _handler(_args: dict, _req: Any) -> dict:
            return {}

        assert asyncio.iscoroutinefunction(_handler) is True

    def test_sync_handler_is_not_coroutine_function(self) -> None:
        """Regular sync handlers are not identified as coroutine functions."""

        def _handler(_args: dict, _req: Any) -> dict:
            return {}

        assert asyncio.iscoroutinefunction(_handler) is False

    def test_async_tool_not_found_still_raises(self) -> None:
        """ToolNotFoundError is still raised for unknown tool names with async handlers."""
        reg = ToolRegistry()

        async def _handler(_args: dict, _req: Any) -> dict:
            return {}

        reg.register("async.registered", _handler, "Registered", {})
        with pytest.raises(ToolNotFoundError):
            reg.dispatch(_anon_request(), "async.unregistered", {})

    def test_async_handler_schema_validated_before_call(self) -> None:
        """JSON Schema validation still runs before an async handler is called."""
        reg = ToolRegistry()

        async def _handler(_args: dict, _req: Any) -> dict:
            return {}

        reg.register(
            "async.validated",
            _handler,
            "Validated",
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        )
        with pytest.raises(ToolInputError):
            reg.dispatch(_anon_request(), "async.validated", {})


# ---------------------------------------------------------------------------
# Integration: async handler called via McpView tools/call
# ---------------------------------------------------------------------------


class TestAsyncHandlerViaView:
    """Async tool handlers are callable end-to-end through McpView tools/call."""

    def test_async_handler_response_returned_in_jsonrpc(self) -> None:
        """An async tool handler's return value appears in the tools/call JSON-RPC result."""
        reg = ToolRegistry()

        async def _greet(args: dict, _req: Any) -> dict:
            return {"greeting": f"hello {args.get('name', 'world')}"}

        reg.register(
            "greet.async",
            _greet,
            "Async greeter",
            {"type": "object", "properties": {"name": {"type": "string"}}},
        )

        with patch("friese_mcp.views.tool_registry", reg):
            resp = _view(_tools_call_request("greet.async", {"name": "pytest"}))

        data = json.loads(resp.content)
        assert data["result"]["content"][0]["text"] == '{"greeting": "hello pytest"}'

    def test_async_handler_error_propagates_as_is_error(self) -> None:
        """An async handler that raises PermissionError returns an isError response."""
        reg = ToolRegistry()

        async def _fail(_args: dict, _req: Any) -> None:
            raise PermissionError("async denied")

        reg.register("fail.async", _fail, "Fail", {})

        with patch("friese_mcp.views.tool_registry", reg):
            resp = _view(_tools_call_request("fail.async", {}))

        data = json.loads(resp.content)
        assert data["result"]["isError"] is True
