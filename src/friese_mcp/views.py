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

Authentication & permissions
-----------------------------
``McpEndpointView`` extends DRF's :class:`~rest_framework.views.APIView`, which
means host projects can gate the MCP surface using standard DRF mechanisms:

* ``FRIESE_MCP_AUTHENTICATION_CLASSES`` — list of dotted-path strings *or*
  class objects; falls back to DRF's ``DEFAULT_AUTHENTICATION_CLASSES``.
* ``FRIESE_MCP_PERMISSION_CLASSES``    — list of dotted-path strings *or*
  class objects; defaults to ``[]`` (no gateway-level permission check) for
  backwards compatibility.  Tool-level ``permission_classes`` are enforced
  separately by :data:`~friese_mcp.registry.tool_registry`.
"""

import json
import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest, JsonResponse
from django.utils.module_loading import import_string
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.request import Request as DRFRequest
from rest_framework.views import APIView

from friese_mcp.backends.invocation import _format_drf_validation_error
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
# Settings helpers
# ---------------------------------------------------------------------------


def _resolve_classes(setting_name: str) -> list[Any] | None:
    """
    Resolve a settings list of class paths or class objects.

    Returns ``None`` when the setting is absent (caller should fall back to
    DRF defaults).  Returns an empty list when the setting is explicitly ``[]``.

    Each element may be:

    * A dotted-path string (e.g. ``"rest_framework.authentication.SessionAuthentication"``).
    * A class object already imported by the host project.

    """
    raw = getattr(settings, setting_name, None)
    if raw is None:
        return None
    return [import_string(cls) if isinstance(cls, str) else cls for cls in raw]


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
    except DRFValidationError as exc:
        # IT-3: Surface DRF validation errors raised by @mcp_tool functions —
        # they describe invalid input and are safe to return to the caller.
        msg = _format_drf_validation_error(exc)
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid arguments", msg)
    except DjangoValidationError as exc:
        # IT-3: Surface Django model/form validation errors raised by @mcp_tool functions.
        msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid arguments", msg)
    except ValueError as exc:
        # IT-3: Surface ValueError raised by @mcp_tool handlers — the convention is to
        # raise ValueError for user-correctable input problems (e.g. invalid UUID, bad
        # enum value).  Return as a tool-level isError response so the caller gets
        # actionable feedback without a full JSON-RPC error.
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            },
        )
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
    # DRF wraps the Django HttpRequest in a rest_framework.request.Request.
    # _parse_and_dispatch only needs request.body and request.user — both are
    # proxied transparently by the DRF wrapper, so no unwrapping is needed.
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
# Main endpoint — DRF APIView
# ---------------------------------------------------------------------------


class McpEndpointView(APIView):
    """
    MCP gateway — single HTTP POST endpoint for all JSON-RPC 2.0 traffic.

    Extends DRF :class:`~rest_framework.views.APIView` so that host projects
    can apply standard DRF authentication and permission classes to the MCP
    surface without requiring custom middleware.

    Configuration (all optional)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``FRIESE_MCP_AUTHENTICATION_CLASSES``
        List of dotted-path strings or class objects.  When absent, DRF's
        ``DEFAULT_AUTHENTICATION_CLASSES`` are used.

    ``FRIESE_MCP_PERMISSION_CLASSES``
        List of dotted-path strings or class objects.  Defaults to ``[]``
        (no gateway-level permission check) to preserve backwards
        compatibility.  Individual tools still enforce their own
        ``permission_classes`` via :data:`~friese_mcp.registry.tool_registry`.

    Example (JWT-gated MCP surface)::

        # settings.py
        FRIESE_MCP_AUTHENTICATION_CLASSES = [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
        ]
        FRIESE_MCP_PERMISSION_CLASSES = [
            "rest_framework.permissions.IsAuthenticated",
        ]

    """

    def get_authenticators(self) -> list[Any]:
        """
        Return authenticator instances for this view.

        Reads ``FRIESE_MCP_AUTHENTICATION_CLASSES`` from settings.  When the
        setting is absent, delegates to the DRF default.
        """
        classes = _resolve_classes("FRIESE_MCP_AUTHENTICATION_CLASSES")
        if classes is None:
            return super().get_authenticators()
        return [cls() for cls in classes]

    def get_permissions(self) -> list[Any]:
        """
        Return permission instances for this view.

        Reads ``FRIESE_MCP_PERMISSION_CLASSES`` from settings.  Defaults to
        ``[]`` when the setting is absent (backward compatible — no gateway
        permission check; tool-level permissions still apply).
        """
        classes = _resolve_classes("FRIESE_MCP_PERMISSION_CLASSES")
        if classes is None:
            return []
        return [cls() for cls in classes]

    def post(self, request: DRFRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Handle POST — the only allowed HTTP method."""
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

    def http_method_not_allowed(  # type: ignore[override]
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        """Return a JSON-RPC 2.0 error for non-POST methods."""
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": INVALID_REQUEST, "message": "Method Not Allowed — POST only"},
            },
            status=405,
        )
