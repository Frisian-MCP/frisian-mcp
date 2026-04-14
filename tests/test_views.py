"""Tests for the MCP gateway endpoint (views.py)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from rest_framework.permissions import BasePermission

from friese_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
)
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import mcp_endpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(rf: RequestFactory, payload: Any, content_type: str = "application/json") -> Any:
    """Build a POST request with a JSON payload."""
    return rf.post(
        "/mcp/",
        data=json.dumps(payload),
        content_type=content_type,
    )


def _jsonrpc(method: str, params: dict[str, Any] | None = None, req_id: Any = 1) -> dict[str, Any]:
    """Build a minimal JSON-RPC 2.0 request dict."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _call(rf: RequestFactory, method: str, params: dict[str, Any] | None = None) -> Any:
    """Helper: POST a well-formed JSON-RPC request and call the view."""
    request = _post(rf, _jsonrpc(method, params))
    request.user = _anon_user()
    return mcp_endpoint(request)


def _anon_user() -> AnonymousUser:
    """Return a minimal anonymous-user stand-in."""
    return AnonymousUser()


def _response_data(response: Any) -> dict[str, Any]:
    """Parse the JSON body of a JsonResponse."""
    return json.loads(response.content)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# HTTP-level guards
# ---------------------------------------------------------------------------


class TestHttpGuards:
    """Tests for HTTP-method and feature-flag guards in mcp_endpoint."""

    def test_get_returns_405(self, rf: RequestFactory) -> None:
        """GET requests to the MCP endpoint return 405 Method Not Allowed."""
        request = rf.get("/mcp/")
        request.user = _anon_user()
        response = mcp_endpoint(request)
        assert response.status_code == 405

    def test_get_body_is_json_rpc_error(self, rf: RequestFactory) -> None:
        """A 405 response has a JSON-RPC error body."""
        request = rf.get("/mcp/")
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_disabled_returns_503(self, rf: RequestFactory, settings: Any) -> None:
        """When FRIESE_MCP_ENABLED=False the endpoint returns 503."""
        settings.FRIESE_MCP_ENABLED = False
        request = _post(rf, _jsonrpc("ping"))
        request.user = _anon_user()
        response = mcp_endpoint(request)
        assert response.status_code == 503

    def test_disabled_body_is_json_rpc_error(self, rf: RequestFactory, settings: Any) -> None:
        """A 503 response has a JSON-RPC error body with INTERNAL_ERROR code."""
        settings.FRIESE_MCP_ENABLED = False
        request = _post(rf, _jsonrpc("ping"))
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# JSON-RPC parsing
# ---------------------------------------------------------------------------


class TestJsonRpcParsing:
    """Tests for JSON-RPC 2.0 parsing and validation."""

    def test_invalid_json_returns_parse_error(self, rf: RequestFactory) -> None:
        """Malformed JSON produces a JSON-RPC parse error."""
        request = rf.post("/mcp/", data=b"not json", content_type="application/json")
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == -32700

    def test_non_object_json_returns_invalid_request(self, rf: RequestFactory) -> None:
        """A JSON array (not object) produces INVALID_REQUEST."""
        request = _post(rf, [1, 2, 3])
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_wrong_jsonrpc_version_returns_invalid_request(self, rf: RequestFactory) -> None:
        """Jsonrpc != '2.0' produces INVALID_REQUEST."""
        request = _post(rf, {"jsonrpc": "1.0", "id": 1, "method": "ping"})
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_non_string_method_returns_invalid_request(self, rf: RequestFactory) -> None:
        """A non-string method field produces INVALID_REQUEST."""
        request = _post(rf, {"jsonrpc": "2.0", "id": 1, "method": 42})
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_non_object_params_returns_invalid_params(self, rf: RequestFactory) -> None:
        """A non-object params field produces INVALID_PARAMS."""
        request = _post(rf, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]})
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


class TestMethodHandlers:
    """Tests for each supported JSON-RPC method handler."""

    def test_ping_returns_empty_result(self, rf: RequestFactory) -> None:
        """Ping returns a success response with an empty result."""
        data = _response_data(_call(rf, "ping"))
        assert data["result"] == {}
        assert data["jsonrpc"] == "2.0"

    def test_initialize_returns_server_info(self, rf: RequestFactory) -> None:
        """Initialize returns serverInfo and capabilities."""
        data = _response_data(_call(rf, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION}))
        result = data["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert "serverInfo" in result
        assert "capabilities" in result

    def test_initialize_server_name_from_settings(self, rf: RequestFactory, settings: Any) -> None:
        """Initialize uses FRIESE_MCP_SERVER_NAME when set."""
        settings.FRIESE_MCP_SERVER_NAME = "my-gateway"
        data = _response_data(_call(rf, "initialize", {}))
        assert data["result"]["serverInfo"]["name"] == "my-gateway"

    def test_initialized_returns_empty_result(self, rf: RequestFactory) -> None:
        """Initialized returns a success response with an empty result."""
        data = _response_data(_call(rf, "initialized"))
        assert data["result"] == {}

    def test_resources_list_returns_empty(self, rf: RequestFactory) -> None:
        """resources/list returns an empty resources list in v1."""
        data = _response_data(_call(rf, "resources/list"))
        assert data["result"]["resources"] == []

    def test_resources_read_returns_method_not_found(self, rf: RequestFactory) -> None:
        """resources/read returns METHOD_NOT_FOUND in v1."""
        data = _response_data(_call(rf, "resources/read", {"uri": "mcp://test"}))
        assert data["error"]["code"] == METHOD_NOT_FOUND

    def test_unknown_method_returns_method_not_found(self, rf: RequestFactory) -> None:
        """An unrecognised method name returns METHOD_NOT_FOUND."""
        data = _response_data(_call(rf, "no.such.method"))
        assert data["error"]["code"] == METHOD_NOT_FOUND

    def test_request_id_echoed_in_response(self, rf: RequestFactory) -> None:
        """The response id field matches the request id."""
        request = _post(rf, _jsonrpc("ping", req_id=42))
        request.user = _anon_user()
        data = _response_data(mcp_endpoint(request))
        assert data["id"] == 42


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class TestToolsList:
    """Tests for the tools/list handler."""

    def test_tools_list_returns_registered_tools(self, rf: RequestFactory) -> None:
        """tools/list returns all tools currently in the registry."""
        isolated = ToolRegistry()
        isolated.register("test.tool", lambda a, r: None, "Test", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/list"))

        assert len(data["result"]["tools"]) == 1
        assert data["result"]["tools"][0]["name"] == "test.tool"

    def test_tools_list_empty_registry(self, rf: RequestFactory) -> None:
        """tools/list returns an empty list when no tools are registered."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/list"))

        assert data["result"]["tools"] == []


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


class TestToolsCall:
    """Tests for the tools/call handler."""

    def test_tools_call_success(self, rf: RequestFactory) -> None:
        """tools/call returns the tool result wrapped in content[]."""
        isolated = ToolRegistry()
        isolated.register("echo", lambda a, r: {"ok": True}, "Echo", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "echo", "arguments": {}}))

        assert data["result"]["isError"] is False
        content = json.loads(data["result"]["content"][0]["text"])
        assert content == {"ok": True}

    def test_tools_call_unknown_tool(self, rf: RequestFactory) -> None:
        """tools/call with an unknown tool name returns INVALID_PARAMS."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert data["error"]["code"] == INVALID_PARAMS

    def test_tools_call_missing_name(self, rf: RequestFactory) -> None:
        """tools/call without a 'name' field returns INVALID_PARAMS."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"arguments": {}}))

        assert data["error"]["code"] == INVALID_PARAMS

    def test_tools_call_non_dict_arguments(self, rf: RequestFactory) -> None:
        """tools/call with non-object 'arguments' returns INVALID_PARAMS."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "x", "arguments": "bad"}))

        assert data["error"]["code"] == INVALID_PARAMS

    def test_tools_call_permission_denied(self, rf: RequestFactory) -> None:
        """tools/call returns INVALID_PARAMS when a permission class denies access."""

        class _DenyAll(BasePermission):
            def has_permission(self, request: Any, view: Any) -> bool:
                """Deny all."""
                return False

        isolated = ToolRegistry()
        isolated.register("locked", lambda a, r: None, "Locked", {}, [_DenyAll])

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "locked", "arguments": {}}))

        assert data["error"]["code"] == INVALID_PARAMS

    def test_tools_call_exception_returns_is_error_true(self, rf: RequestFactory) -> None:
        """tools/call wraps an unexpected tool exception in isError=True response."""

        def _raiser(arguments: dict[str, Any], request: Any) -> None:
            """Always raises."""
            raise ValueError("boom")

        isolated = ToolRegistry()
        isolated.register("bad", _raiser, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad", "arguments": {}}))

        assert data["result"]["isError"] is True

    def test_tools_call_null_arguments_defaults_to_empty(self, rf: RequestFactory) -> None:
        """tools/call with null 'arguments' defaults to an empty dict."""
        isolated = ToolRegistry()
        captured: list[dict[str, Any]] = []

        def _capture(arguments: dict[str, Any], request: Any) -> None:
            """Capture arguments."""
            captured.append(arguments)

        isolated.register("cap", _capture, "Cap", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            _call(rf, "tools/call", {"name": "cap", "arguments": None})

        assert captured == [{}]
