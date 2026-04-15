"""
MCP gateway endpoint — JSON-RPC 2.0 over HTTP POST.

Single entry-point for all MCP traffic.  Clients POST a JSON-RPC 2.0 message
and receive a JSON-RPC 2.0 response.  Server-Sent Events (SSE) are out of scope
for v1.

Supported methods
-----------------
* ``initialize``       — protocol handshake
* ``initialized``      — client confirmation notification
* ``tools/list``       — enumerate registered tools
* ``tools/call``       — invoke a registered tool
* ``resources/list``   — stub (returns empty list in v1)
* ``resources/read``   — stub (returns METHOD_NOT_FOUND in v1)
* ``ping``             — liveness check
"""

import json
import logging
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from friese_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    JsonDict,
    JsonRpcId,
)
from friese_mcp.registry import ToolInputError, tool_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _jsonrpc_success(request_id: JsonRpcId, result: JsonDict) -> JsonResponse:
    """Return a JSON-RPC 2.0 success response."""
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(
    request_id: JsonRpcId,
    code: int,
    message: str,
    data: str | None = None,
) -> JsonResponse:
    """Return a JSON-RPC 2.0 error response."""
    error: JsonDict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


def _handle_initialize(request_id: JsonRpcId, params: JsonDict) -> JsonResponse:
    """Handle ``initialize`` — MCP protocol handshake."""
    client_info: Any = params.get("clientInfo", {})
    protocol_version: Any = params.get("protocolVersion", MCP_PROTOCOL_VERSION)

    logger.info(
        "mcp_initialize",
        extra={
            "client_name": client_info.get("name") if isinstance(client_info, dict) else None,
            "client_version": (
                client_info.get("version") if isinstance(client_info, dict) else None
            ),
            "protocol_version": protocol_version,
        },
    )

    server_name: str = getattr(settings, "FRIESE_MCP_SERVER_NAME", "friese-mcp")
    return _jsonrpc_success(
        request_id,
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {"name": server_name, "version": "0.1.0"},
            "capabilities": {"tools": {}, "resources": {}},
        },
    )


def _handle_initialized(request_id: JsonRpcId) -> JsonResponse:
    """Handle ``initialized`` — client confirms handshake; acknowledgement only."""
    logger.info("mcp_initialized")
    return _jsonrpc_success(request_id, {})


def _handle_tools_list(request_id: JsonRpcId) -> JsonResponse:
    """
    Handle ``tools/list`` — return the tool manifest from the registry.

    **Auth note:** This handler returns the full tool manifest to any caller without
    performing any authentication or permission checks.  This is intentional: friese-mcp
    does not own authentication or authorisation.  The host application is responsible
    for placing auth-gating in front of the MCP endpoint at the infrastructure level
    (e.g. API gateway, reverse proxy, Django middleware, DRF authentication classes on
    the URL include).  Refer to the friese-mcp documentation for recommended patterns.
    """
    return _jsonrpc_success(request_id, {"tools": tool_registry.list_tools()})


def _handle_tools_call(
    request: HttpRequest,
    request_id: JsonRpcId,
    params: JsonDict,
) -> JsonResponse:
    """Handle ``tools/call`` — validate and dispatch to the tool registry."""
    tool_name: Any = params.get("name")
    arguments: Any = params.get("arguments") or {}

    if not tool_name or not isinstance(tool_name, str):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid params", "'name' is required")
    if not isinstance(arguments, dict):
        return _jsonrpc_error(
            request_id, INVALID_PARAMS, "Invalid params", "'arguments' must be an object"
        )

    try:
        result = tool_registry.dispatch(request, tool_name, arguments)
    except LookupError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Unknown tool", str(exc))
    except ToolInputError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid arguments", str(exc))
    except PermissionError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Permission denied", str(exc))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("tool_execution_error", extra={"tool": tool_name, "error": str(exc)})
        # Do NOT surface the raw exception message to the caller — it may contain
        # internal details (DB column names, file paths, stack frames).  The full
        # error is already captured in the server log above.
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"error": "Internal tool error"})}],
                "isError": True,
            },
        )

    return _jsonrpc_success(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        },
    )


def _handle_resources_list(request_id: JsonRpcId) -> JsonResponse:
    """Handle ``resources/list`` — returns empty list in v1."""
    return _jsonrpc_success(request_id, {"resources": []})


def _handle_resources_read(request_id: JsonRpcId, params: JsonDict) -> JsonResponse:
    """Handle ``resources/read`` — not implemented in v1."""
    uri: Any = params.get("uri", "<unknown>")
    return _jsonrpc_error(
        request_id,
        METHOD_NOT_FOUND,
        "resources/read is not supported in v1",
        str(uri),
    )


def _parse_and_dispatch(request: HttpRequest) -> JsonResponse:
    """Parse the POST body and dispatch to the appropriate method handler."""
    try:
        body: Any = json.loads(request.body)
    except json.JSONDecodeError as exc:
        return _jsonrpc_error(None, -32700, "Parse error", str(exc))

    if not isinstance(body, dict):
        return _jsonrpc_error(None, INVALID_REQUEST, "Invalid Request", "expected a JSON object")
    if body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(None, INVALID_REQUEST, "jsonrpc must be '2.0'")

    request_id: JsonRpcId = body.get("id")
    method: Any = body.get("method", "")
    params: Any = body.get("params") or {}

    if not isinstance(method, str):
        return _jsonrpc_error(request_id, INVALID_REQUEST, "'method' must be a string")
    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "'params' must be an object")

    logger.debug("mcp_request", extra={"method": method, "request_id": request_id})

    if method == "ping":
        return _jsonrpc_success(request_id, {})
    if method == "initialize":
        return _handle_initialize(request_id, params)
    if method == "initialized":
        return _handle_initialized(request_id)
    if method == "tools/list":
        return _handle_tools_list(request_id)
    if method == "tools/call":
        return _handle_tools_call(request, request_id, params)
    if method == "resources/list":
        return _handle_resources_list(request_id)
    if method == "resources/read":
        return _handle_resources_read(request_id, params)
    return _jsonrpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------


@csrf_exempt
def mcp_endpoint(request: HttpRequest) -> JsonResponse:
    """MCP gateway — single HTTP POST endpoint for all JSON-RPC 2.0 traffic."""
    if request.method != "POST":
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": INVALID_REQUEST, "message": "Method Not Allowed — POST only"},
            },
            status=405,
        )
    if not getattr(settings, "FRIESE_MCP_ENABLED", True):
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": INTERNAL_ERROR, "message": "MCP gateway is disabled"},
            },
            status=503,
        )
    return _parse_and_dispatch(request)
