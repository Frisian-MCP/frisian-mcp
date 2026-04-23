"""Tests for the MCP gateway endpoint (views.py)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import RequestFactory, override_settings
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import BasePermission

from friese_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
)
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView

_view = McpEndpointView.as_view()

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
    return _view(request)


def _notify(rf: RequestFactory, method: str, params: dict[str, Any] | None = None) -> Any:
    """Helper: POST a JSON-RPC 2.0 notification (no 'id' field) and call the view."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    request = _post(rf, msg)
    request.user = _anon_user()
    return _view(request)


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
    """Tests for HTTP-method and feature-flag guards in McpEndpointView."""

    def test_delete_returns_200(self, rf: RequestFactory) -> None:
        """DELETE requests return HTTP 200 (stateless no-op for session cleanup)."""
        request = rf.delete("/mcp/")
        request.user = _anon_user()
        response = _view(request)
        assert response.status_code == 200

    def test_delete_body_is_empty_object(self, rf: RequestFactory) -> None:
        """DELETE response body is an empty JSON object."""
        request = rf.delete("/mcp/")
        request.user = _anon_user()
        response = _view(request)
        assert json.loads(response.content) == {}

    def test_get_returns_405(self, rf: RequestFactory) -> None:
        """GET requests to the MCP endpoint return 405 Method Not Allowed."""
        request = rf.get("/mcp/")
        request.user = _anon_user()
        response = _view(request)
        assert response.status_code == 405

    def test_get_body_is_json_rpc_error(self, rf: RequestFactory) -> None:
        """A 405 response has a JSON-RPC error body."""
        request = rf.get("/mcp/")
        request.user = _anon_user()
        data = _response_data(_view(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_disabled_returns_503(self, rf: RequestFactory, settings: Any) -> None:
        """When FRIESE_MCP_ENABLED=False the endpoint returns 503."""
        settings.FRIESE_MCP_ENABLED = False
        request = _post(rf, _jsonrpc("ping"))
        request.user = _anon_user()
        response = _view(request)
        assert response.status_code == 503

    def test_disabled_body_is_json_rpc_error(self, rf: RequestFactory, settings: Any) -> None:
        """A 503 response has a JSON-RPC error body with INTERNAL_ERROR code."""
        settings.FRIESE_MCP_ENABLED = False
        request = _post(rf, _jsonrpc("ping"))
        request.user = _anon_user()
        data = _response_data(_view(request))
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
        data = _response_data(_view(request))
        assert data["error"]["code"] == -32700

    def test_non_object_json_returns_invalid_request(self, rf: RequestFactory) -> None:
        """A JSON array (not object) produces INVALID_REQUEST."""
        request = _post(rf, [1, 2, 3])
        request.user = _anon_user()
        data = _response_data(_view(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_wrong_jsonrpc_version_returns_invalid_request(self, rf: RequestFactory) -> None:
        """Jsonrpc != '2.0' produces INVALID_REQUEST."""
        request = _post(rf, {"jsonrpc": "1.0", "id": 1, "method": "ping"})
        request.user = _anon_user()
        data = _response_data(_view(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_non_string_method_returns_invalid_request(self, rf: RequestFactory) -> None:
        """A non-string method field produces INVALID_REQUEST."""
        request = _post(rf, {"jsonrpc": "2.0", "id": 1, "method": 42})
        request.user = _anon_user()
        data = _response_data(_view(request))
        assert data["error"]["code"] == INVALID_REQUEST

    def test_non_object_params_returns_invalid_params(self, rf: RequestFactory) -> None:
        """A non-object params field produces INVALID_PARAMS."""
        request = _post(rf, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]})
        request.user = _anon_user()
        data = _response_data(_view(request))
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

    def test_initialize_server_name_default(self, rf: RequestFactory) -> None:
        """Initialize serverInfo.name defaults to 'friese-mcp'."""
        data = _response_data(_call(rf, "initialize", {}))
        assert data["result"]["serverInfo"]["name"] == "friese-mcp"

    def test_initialize_server_version_present(self, rf: RequestFactory) -> None:
        """Initialize serverInfo.version is present and non-empty."""
        data = _response_data(_call(rf, "initialize", {}))
        version = data["result"]["serverInfo"]["version"]
        assert isinstance(version, str) and version

    def test_initialize_tools_version_present(self, rf: RequestFactory) -> None:
        """Initialize response contains toolsVersion key."""
        data = _response_data(_call(rf, "initialize", {}))
        assert "toolsVersion" in data["result"]

    def test_initialize_tools_version_is_eight_hex_chars(self, rf: RequestFactory) -> None:
        """Value of toolsVersion is exactly 8 hexadecimal characters."""
        data = _response_data(_call(rf, "initialize", {}))
        tv = data["result"]["toolsVersion"]
        assert isinstance(tv, str)
        assert len(tv) == 8
        assert all(c in "0123456789abcdef" for c in tv)

    def test_initialize_tools_version_stable(self, rf: RequestFactory) -> None:
        """Same tool set produces the same toolsVersion on repeated calls."""
        isolated = ToolRegistry()
        isolated.register(
            name="a_tool",
            fn=lambda a, r: {},
            description="",
            input_schema={"type": "object", "properties": {}},
        )
        with patch("friese_mcp.views.tool_registry", isolated):
            v1 = _response_data(_call(rf, "initialize", {}))["result"]["toolsVersion"]
            v2 = _response_data(_call(rf, "initialize", {}))["result"]["toolsVersion"]
        assert v1 == v2

    def test_initialize_tools_version_changes_with_registry(self, rf: RequestFactory) -> None:
        """Different tool sets produce different toolsVersion values."""
        reg_a = ToolRegistry()
        reg_a.register(
            name="tool_a",
            fn=lambda a, r: {},
            description="",
            input_schema={"type": "object", "properties": {}},
        )
        reg_b = ToolRegistry()
        reg_b.register(
            name="tool_b",
            fn=lambda a, r: {},
            description="",
            input_schema={"type": "object", "properties": {}},
        )
        with patch("friese_mcp.views.tool_registry", reg_a):
            v_a = _response_data(_call(rf, "initialize", {}))["result"]["toolsVersion"]
        with patch("friese_mcp.views.tool_registry", reg_b):
            v_b = _response_data(_call(rf, "initialize", {}))["result"]["toolsVersion"]
        assert v_a != v_b

    def test_initialized_returns_empty_result(self, rf: RequestFactory) -> None:
        """Initialized sent as a request (with id) returns a success response."""
        data = _response_data(_call(rf, "initialized"))
        assert data["result"] == {}

    def test_initialized_notification_returns_202(self, rf: RequestFactory) -> None:
        """Initialized sent as a notification (no id) returns HTTP 202 per Streamable HTTP spec."""
        response = _notify(rf, "initialized")
        assert response.status_code == 202
        assert not response.content

    def test_unknown_notification_returns_202(self, rf: RequestFactory) -> None:
        """Any JSON-RPC notification (no id) returns HTTP 202 Accepted."""
        response = _notify(rf, "notifications/cancelled", {"requestId": 1, "reason": "user"})
        assert response.status_code == 202
        assert not response.content

    def test_resources_list_returns_empty(self, rf: RequestFactory) -> None:
        """resources/list returns an empty resources list in v1."""
        data = _response_data(_call(rf, "resources/list"))
        assert data["result"]["resources"] == []

    def test_resources_read_unknown_uri_returns_invalid_params(self, rf: RequestFactory) -> None:
        """resources/read with an unknown URI returns INVALID_PARAMS."""
        data = _response_data(_call(rf, "resources/read", {"uri": "mcp://unknown"}))
        assert data["error"]["code"] == INVALID_PARAMS

    def test_unknown_method_returns_method_not_found(self, rf: RequestFactory) -> None:
        """An unrecognised method name returns METHOD_NOT_FOUND."""
        data = _response_data(_call(rf, "no.such.method"))
        assert data["error"]["code"] == METHOD_NOT_FOUND

    def test_request_id_echoed_in_response(self, rf: RequestFactory) -> None:
        """The response id field matches the request id."""
        request = _post(rf, _jsonrpc("ping", req_id=42))
        request.user = _anon_user()
        data = _response_data(_view(request))
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
        """tools/call with an unknown tool name returns METHOD_NOT_FOUND."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert data["error"]["code"] == METHOD_NOT_FOUND

    def test_tools_call_unknown_tool_includes_refresh_hint(self, rf: RequestFactory) -> None:
        """tools/call -32601 error data includes the tools/list refresh hint."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert "tools/list" in data["error"]["data"]
        assert "manifest" in data["error"]["data"]

    def test_tools_call_unknown_tool_includes_available_tools(self, rf: RequestFactory) -> None:
        """tools/call -32601 error data lists available tool names."""
        isolated = ToolRegistry()
        for name in ("beta", "alpha"):
            isolated.register(
                name=name,
                fn=lambda a, r: {},
                description="",
                input_schema={"type": "object", "properties": {}},
            )

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert "Available tools: alpha, beta." in data["error"]["data"]

    def test_tools_call_available_tools_sorted(self, rf: RequestFactory) -> None:
        """Available tools in -32601 data appear in sorted order."""
        isolated = ToolRegistry()
        for name in ("zebra", "apple", "mango"):
            isolated.register(
                name=name,
                fn=lambda a, r: {},
                description="",
                input_schema={"type": "object", "properties": {}},
            )

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert "Available tools: apple, mango, zebra." in data["error"]["data"]

    def test_tools_call_available_tools_empty_registry(self, rf: RequestFactory) -> None:
        """Available tools section is present even when registry is empty."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert "Available tools: ." in data["error"]["data"]

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
        """Permission denial returns isError=True content block, not a JSON-RPC protocol error."""

        class _DenyAll(BasePermission):
            def has_permission(self, request: Any, view: Any) -> bool:
                """Deny all."""
                return False

        isolated = ToolRegistry()
        isolated.register("locked", lambda a, r: None, "Locked", {}, [_DenyAll])

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "locked", "arguments": {}}))

        # Permission denial is a tool-level error, not a protocol-level error.
        assert "error" not in data
        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert "error" in content

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

    def test_tools_call_schema_validation_failure_returns_invalid_params(
        self, rf: RequestFactory
    ) -> None:
        """tools/call with arguments failing JSON Schema returns INVALID_PARAMS error."""
        isolated = ToolRegistry()
        isolated.register(
            "typed",
            lambda a, r: None,
            "Typed",
            {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
        )

        with patch("friese_mcp.views.tool_registry", isolated):
            # Pass a string where an integer is required — should fail schema validation.
            data = _response_data(
                _call(
                    rf,
                    "tools/call",
                    {"name": "typed", "arguments": {"count": "not-an-int"}},
                )
            )

        assert "error" in data
        assert data["error"]["code"] == INVALID_PARAMS
        assert data["error"]["message"] == "Invalid arguments"

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


# ---------------------------------------------------------------------------
# IT-3: ValueError from @mcp_tool handlers is surfaced to the caller
# ---------------------------------------------------------------------------


class TestValueErrorSurfacing:
    """IT-3: ValueError raised by tool handlers surfaces the message, not 'Internal tool error'."""

    def test_value_error_returns_is_error_true(self, rf: RequestFactory) -> None:
        """ValueError from a tool handler produces isError=True in the result."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises ValueError."""
            raise ValueError("Invalid UUID for 'id': 'not-a-uuid'")

        isolated = ToolRegistry()
        isolated.register("bad.uuid", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.uuid", "arguments": {}}))

        assert data["result"]["isError"] is True

    def test_value_error_message_is_surfaced(self, rf: RequestFactory) -> None:
        """ValueError message from the handler reaches the caller (not 'Internal tool error')."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises ValueError."""
            raise ValueError("Invalid UUID for 'id': 'not-a-uuid'")

        isolated = ToolRegistry()
        isolated.register("bad.uuid", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.uuid", "arguments": {}}))

        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Invalid UUID for 'id': 'not-a-uuid'"

    def test_unexpected_exception_still_returns_generic_message(self, rf: RequestFactory) -> None:
        """Unexpected exceptions (non-ValueError) still return the safe generic message."""

        def _boom(arguments: dict[str, Any], request: Any) -> None:
            """Raises unexpected internal error."""
            raise RuntimeError("DB column 'secret_internal_field' missing")

        isolated = ToolRegistry()
        isolated.register("boom", _boom, "Boom", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "boom", "arguments": {}}))

        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Internal tool error"


# ---------------------------------------------------------------------------
# FRIESE_MCP_EXPOSE_ERRORS — configurable exception verbosity
# ---------------------------------------------------------------------------


class TestExposeErrors:
    """FRIESE_MCP_EXPOSE_ERRORS: configurable exception verbosity."""

    def _register_boom(self, msg: str) -> ToolRegistry:
        def _boom(arguments: dict[str, Any], request: Any) -> None:
            raise RuntimeError(msg)

        reg = ToolRegistry()
        reg.register("boom", _boom, "Boom", {})
        return reg

    def test_default_returns_generic_message(self, rf: RequestFactory) -> None:
        """Without the setting (and DEBUG=False), returns 'Internal tool error'."""
        reg = self._register_boom("secret detail")
        with patch("friese_mcp.views.tool_registry", reg):
            data = _response_data(_call(rf, "tools/call", {"name": "boom", "arguments": {}}))
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Internal tool error"

    @override_settings(FRIESE_MCP_EXPOSE_ERRORS=True)
    def test_expose_true_returns_exception_message(self, rf: RequestFactory) -> None:
        """FRIESE_MCP_EXPOSE_ERRORS=True surfaces str(exc)."""
        reg = self._register_boom("very secret detail")
        with patch("friese_mcp.views.tool_registry", reg):
            data = _response_data(_call(rf, "tools/call", {"name": "boom", "arguments": {}}))
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "very secret detail"

    @override_settings(FRIESE_MCP_EXPOSE_ERRORS=False, DEBUG=True)
    def test_expose_false_overrides_debug(self, rf: RequestFactory) -> None:
        """FRIESE_MCP_EXPOSE_ERRORS=False suppresses detail even when DEBUG=True."""
        reg = self._register_boom("secret detail")
        with patch("friese_mcp.views.tool_registry", reg):
            data = _response_data(_call(rf, "tools/call", {"name": "boom", "arguments": {}}))
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Internal tool error"

    @override_settings(DEBUG=True)
    def test_debug_true_without_setting_exposes_detail(self, rf: RequestFactory) -> None:
        """DEBUG=True without explicit setting surfaces str(exc)."""
        reg = self._register_boom("debug detail")
        with patch("friese_mcp.views.tool_registry", reg):
            data = _response_data(_call(rf, "tools/call", {"name": "boom", "arguments": {}}))
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "debug detail"


# ---------------------------------------------------------------------------
# IT-8: DRF ValidationError surfacing with structured field-level detail
# ---------------------------------------------------------------------------


class TestDRFValidationErrorSurfacing:
    """IT-8: DRFValidationError from tool handlers returns isError=True with structured detail."""

    def test_drf_field_error_returns_is_error_true(self, rf: RequestFactory) -> None:
        """DRFValidationError from a tool handler produces isError=True in the result."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DRFValidationError with field detail."""
            raise DRFValidationError({"name": ["This field is required."]})

        isolated = ToolRegistry()
        isolated.register("bad.drf", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.drf", "arguments": {}}))

        assert data["result"]["isError"] is True

    def test_drf_field_error_structured_detail(self, rf: RequestFactory) -> None:
        """IT-8: Field-level DRFValidationError detail is returned as a structured dict."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DRFValidationError with field detail."""
            raise DRFValidationError({"email": ["Enter a valid email address."]})

        isolated = ToolRegistry()
        isolated.register("bad.email", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.email", "arguments": {}}))

        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Validation failed"
        assert "email" in content["detail"]
        assert "Enter a valid email address." in content["detail"]["email"]

    def test_drf_non_field_error_returns_error_string(self, rf: RequestFactory) -> None:
        """IT-8: Non-field DRFValidationError (list detail) returns a plain error string."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DRFValidationError with non-field error."""
            raise DRFValidationError(["Invalid input."])

        isolated = ToolRegistry()
        isolated.register("bad.nonfield", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "bad.nonfield", "arguments": {}})
            )

        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert "Invalid input." in content["error"]

    def test_drf_validation_error_not_json_rpc_error(self, rf: RequestFactory) -> None:
        """IT-8: DRFValidationError produces a result (not a JSON-RPC error code)."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DRFValidationError."""
            raise DRFValidationError({"title": ["Too short."]})

        isolated = ToolRegistry()
        isolated.register("bad.title", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.title", "arguments": {}}))

        # Must be a result (isError=True), NOT a JSON-RPC error response.
        assert "result" in data
        assert "error" not in data


# ---------------------------------------------------------------------------
# Finding 1: DRFValidationError from SyncInvocation-backed tools surfaces correctly
# ---------------------------------------------------------------------------


class TestSyncInvocationValidationSurfacing:
    """Finding 1: Validation errors from auto-discovered tools reach the caller."""

    def test_drf_validation_bubbles_through_invocation_fn(self, rf: RequestFactory) -> None:
        """Finding 1: DRFValidationError raised inside a tool fn surfaces as isError=True."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Simulates a DRFValidationError from a ViewSet write action."""
            raise DRFValidationError({"name": ["This field is required."]})

        isolated = ToolRegistry()
        isolated.register("auto.create", _bad, "Create", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "auto.create", "arguments": {}}))

        # Must NOT be "Internal tool error" — must be the structured validation detail.
        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["error"] == "Validation failed"
        assert "name" in content["detail"]


# ---------------------------------------------------------------------------
# Finding 3: Unknown tool returns METHOD_NOT_FOUND, not INVALID_PARAMS
# ---------------------------------------------------------------------------


class TestUnknownToolCode:
    """Finding 3: LookupError for an unknown tool name uses METHOD_NOT_FOUND (-32601)."""

    def test_unknown_tool_returns_method_not_found(self, rf: RequestFactory) -> None:
        """Finding 3: An unknown tool name returns -32601 METHOD_NOT_FOUND."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert data["error"]["code"] == METHOD_NOT_FOUND

    def test_unknown_tool_not_invalid_params(self, rf: RequestFactory) -> None:
        """Finding 3: An unknown tool name does NOT use INVALID_PARAMS (-32602)."""
        isolated = ToolRegistry()

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(
                _call(rf, "tools/call", {"name": "no.such.tool", "arguments": {}})
            )

        assert data["error"]["code"] != INVALID_PARAMS


# ---------------------------------------------------------------------------
# Agent-friendly help method
# ---------------------------------------------------------------------------


class TestHelpMethod:
    """Tests for the ``help`` JSON-RPC method."""

    def test_help_returns_success(self, rf: RequestFactory) -> None:
        """Help method returns a success response."""
        data = _response_data(_call(rf, "help"))
        assert "result" in data
        assert "error" not in data

    def test_help_result_has_server_field(self, rf: RequestFactory) -> None:
        """Help result includes a server name."""
        data = _response_data(_call(rf, "help"))
        assert "server" in data["result"]

    def test_help_result_has_methods_list(self, rf: RequestFactory) -> None:
        """Help result includes a list of supported methods."""
        data = _response_data(_call(rf, "help"))
        methods = data["result"]["methods"]
        assert "tools/list" in methods
        assert "tools/call" in methods
        assert "help" in methods

    def test_help_result_has_hints(self, rf: RequestFactory) -> None:
        """Help result includes usage hints for agents."""
        data = _response_data(_call(rf, "help"))
        assert "hints" in data["result"]
        hints = data["result"]["hints"]
        assert "discovery" in hints
        assert "errors" in hints

    def test_help_server_name_from_settings(self, rf: RequestFactory, settings: Any) -> None:
        """Help uses FRIESE_MCP_SERVER_NAME when configured."""
        settings.FRIESE_MCP_SERVER_NAME = "my-server"
        data = _response_data(_call(rf, "help"))
        assert data["result"]["server"] == "my-server"


# ---------------------------------------------------------------------------
# Tool-not-found suggestions
# ---------------------------------------------------------------------------


class TestToolNotFoundSuggestions:
    """Tool-not-found errors include close-match suggestions."""

    def test_typo_in_tool_name_includes_suggestion(self, rf: RequestFactory) -> None:
        """A near-typo in the tool name produces a suggestion in error data."""
        isolated = ToolRegistry()
        isolated.register("users.create", lambda a, r: None, "Create", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "users.creat", "arguments": {}}))

        assert data["error"]["code"] == METHOD_NOT_FOUND
        assert "users.create" in data["error"]["data"]

    def test_no_suggestion_for_unrelated_name(self, rf: RequestFactory) -> None:
        """A completely unrelated tool name produces no suggestion."""
        isolated = ToolRegistry()
        isolated.register("users.create", lambda a, r: None, "Create", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "zzz.qqq.xxx", "arguments": {}}))

        assert data["error"]["code"] == METHOD_NOT_FOUND
        # No suggestion appended when nothing is close enough.
        assert "Did you mean" not in data["error"]["data"]


# ---------------------------------------------------------------------------
# DjangoValidationError as isError=True
# ---------------------------------------------------------------------------


class TestDjangoValidationErrorSurfacing:
    """DjangoValidationError from tool handlers returns isError=True, not a JSON-RPC error."""

    def test_django_validation_error_is_error_true(self, rf: RequestFactory) -> None:
        """DjangoValidationError returns isError=True in the content block."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DjangoValidationError."""
            raise DjangoValidationError("Value must be positive.")

        isolated = ToolRegistry()
        isolated.register("bad.django", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.django", "arguments": {}}))

        assert data["result"]["isError"] is True

    def test_django_validation_error_message_surfaced(self, rf: RequestFactory) -> None:
        """DjangoValidationError message reaches the caller via content block."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DjangoValidationError."""
            raise DjangoValidationError("Value must be positive.")

        isolated = ToolRegistry()
        isolated.register("bad.django2", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.django2", "arguments": {}}))

        content = json.loads(data["result"]["content"][0]["text"])
        assert "Value must be positive" in content["error"]

    def test_django_validation_error_not_json_rpc_error(self, rf: RequestFactory) -> None:
        """DjangoValidationError does NOT return a JSON-RPC error code."""

        def _bad(arguments: dict[str, Any], request: Any) -> None:
            """Raises DjangoValidationError."""
            raise DjangoValidationError("Bad value.")

        isolated = ToolRegistry()
        isolated.register("bad.django3", _bad, "Bad", {})

        with patch("friese_mcp.views.tool_registry", isolated):
            data = _response_data(_call(rf, "tools/call", {"name": "bad.django3", "arguments": {}}))

        assert "result" in data
        assert "error" not in data
