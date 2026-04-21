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
* ``help``             — server metadata and usage hints for AI agents

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

import difflib
import json
import logging
from collections.abc import Generator
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request as DRFRequest
from rest_framework.views import APIView

from friese_mcp.middleware import build_middleware_chain, get_middleware_instances
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
from friese_mcp.resources import ResourceNotFoundError, resource_registry

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
# Error content builders
# ---------------------------------------------------------------------------


def _build_drf_error_content(exc: DRFValidationError) -> dict[str, Any]:
    """
    Convert a DRF ``ValidationError`` into a structured tool-error dict.

    * Field errors (dict detail) → ``{"error": "Validation failed", "detail": {field: [msgs]}}``
    * Non-field errors (list detail) → ``{"error": "<joined messages>"}``
    * Scalar detail → ``{"error": "<string>"}``

    The result is safe to JSON-serialise and return to the MCP caller inside
    an ``isError=True`` content block.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        return {
            "error": "Validation failed",
            "detail": {
                field: [str(e) for e in (errors if isinstance(errors, list) else [errors])]
                for field, errors in detail.items()
            },
        }
    if isinstance(detail, list):
        return {"error": "; ".join(str(e) for e in detail)}
    return {"error": str(detail)}


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


def _get_agent_connection(request: Any) -> Any | None:
    """
    Return the active AgentConnection for ``request.auth``, or ``None``.

    Looks up :class:`~friese_mcp.contrib.agents.models.AgentConnection`
    for ``request.auth``.

    Resolution order:

    1. If ``friese_mcp.contrib.agents`` is not installed → ``None``.
    2. If ``request.auth`` is a
       :class:`~friese_mcp.contrib.tokens.models.FrieseMcpToken` → look up the
       first active ``AgentConnection`` linked via ``token``.
    3. If ``request.auth`` is an
       :class:`~friese_mcp.contrib.oauth.models.OAuthAccessToken` → look up the
       first active ``AgentConnection`` linked via ``oauth_client``.
    4. Otherwise → ``None``.

    Returns ``None`` when no matching ``AgentConnection`` exists, allowing all
    registered tools to be accessible.
    """
    from django.apps import apps as django_apps  # pylint: disable=import-outside-toplevel

    if not django_apps.is_installed("friese_mcp.contrib.agents"):
        return None

    auth = getattr(request, "auth", None)
    if auth is None:
        return None

    try:
        from friese_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrieseMcpToken,
        )

        if isinstance(auth, FrieseMcpToken):
            return auth.agent_connections.filter(is_active=True).first()
    except ImportError:
        pass

    try:
        from friese_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            OAuthAccessToken,
        )

        if isinstance(auth, OAuthAccessToken):
            return auth.client.agent_connections.filter(is_active=True).first()
    except ImportError:
        pass

    return None


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------


def _maybe_sse(response: HttpResponse, request: Any) -> HttpResponse:
    """
    Wrap *response* as a single-message SSE stream when the caller accepts it.

    Returns *response* unchanged when:

    * The request ``Accept`` header does not include ``text/event-stream``, or
    * *response* is not a :class:`~django.http.JsonResponse` (e.g. HTTP 202
      notifications have no body to stream).

    When SSE is accepted, returns a :class:`~django.http.StreamingHttpResponse`
    with ``Content-Type: text/event-stream`` and ``Cache-Control: no-cache``
    containing a single ``data:`` event followed by the double-newline delimiter.

    """
    if not isinstance(response, JsonResponse):
        return response
    accept: str = request.META.get("HTTP_ACCEPT", "")
    if "text/event-stream" not in accept:
        return response

    body: str = response.content.decode("utf-8")

    def _stream() -> Generator[str, None, None]:
        yield f"data: {body}\n\n"

    sse: StreamingHttpResponse = StreamingHttpResponse(
        _stream(), content_type="text/event-stream"
    )
    sse["Cache-Control"] = "no-cache"
    return sse


# ---------------------------------------------------------------------------
# Middleware dispatch helper
# ---------------------------------------------------------------------------


def _tool_registry_dispatch(
    request: HttpRequest, tool_name: str, arguments: dict[str, Any]
) -> Any:
    """Inner dispatch callable passed to the middleware chain."""
    return tool_registry.dispatch(request, tool_name, arguments)


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


def _handle_tools_list(request_id: JsonRpcId, request: Any) -> JsonResponse:
    """
    Handle ``tools/list`` — return the tool manifest from the registry.

    When ``friese_mcp.contrib.agents`` is installed and ``request.auth`` maps to
    an active :class:`~friese_mcp.contrib.agents.models.AgentConnection` with a
    non-null ``allowed_tools`` list, only those tools are included in the
    response.  All other callers receive the full manifest.

    **Auth note:** Beyond per-agent filtering, this handler does not perform
    additional authentication or permission checks.  The host application is
    responsible for gateway-level auth-gating via
    ``FRIESE_MCP_AUTHENTICATION_CLASSES`` / ``FRIESE_MCP_PERMISSION_CLASSES`` or
    upstream infrastructure.
    """
    tools = tool_registry.list_tools()
    conn = _get_agent_connection(request)
    if conn is not None and conn.allowed_tools is not None:
        allowed: frozenset[str] = frozenset(conn.allowed_tools)
        tools = [t for t in tools if t["name"] in allowed]
    return _jsonrpc_success(request_id, {"tools": tools})


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

    # Per-agent tool allowlist: when an active AgentConnection with a non-null
    # allowed_tools list is linked to the caller's credential, reject any tool
    # name not in that list before reaching the registry.
    conn = _get_agent_connection(request)
    if conn is not None and conn.allowed_tools is not None:
        if tool_name not in frozenset(conn.allowed_tools):
            return _jsonrpc_success(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": (
                                        f"Tool {tool_name!r} is not permitted "
                                        "for this agent connection"
                                    )
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )

    # Record last_seen_at for this agent connection (fire-and-forget UPDATE).
    if conn is not None:
        from friese_mcp.contrib.agents.models import (  # pylint: disable=import-outside-toplevel
            AgentConnection,
        )

        AgentConnection.objects.filter(pk=conn.pk).update(last_seen_at=timezone.now())

    try:
        result = build_middleware_chain(
            _tool_registry_dispatch, get_middleware_instances()
        )(request, tool_name, arguments)
    except LookupError as exc:
        # JSON-RPC 2.0: -32601 METHOD_NOT_FOUND is the correct code for an unknown
        # tool name.  -32602 INVALID_PARAMS is reserved for structural argument
        # errors; using it for a missing tool misleads clients into thinking their
        # call format is wrong rather than the tool name.
        #
        # Append close-match suggestions so agents can self-correct without an
        # extra tools/list round-trip.
        known_names = [t["name"] for t in tool_registry.list_tools()]
        suggestions = difflib.get_close_matches(tool_name, known_names, n=3, cutoff=0.6)
        data = str(exc)
        if suggestions:
            data += f". Did you mean: {', '.join(suggestions)}?"
        return _jsonrpc_error(request_id, METHOD_NOT_FOUND, "Unknown tool", data)
    except ToolInputError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid arguments", str(exc))
    except PermissionError as exc:
        # Return as isError=True tool-level content, not a JSON-RPC protocol error.
        # INVALID_PARAMS (-32602) is reserved for argument structure failures; using
        # it for auth denial misleads agents into thinking their call format is wrong.
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            },
        )
    except DRFValidationError as exc:
        # IT-8: Surface DRF field-level validation errors with structured detail so
        # the caller can display per-field messages without parsing a flat string.
        content = _build_drf_error_content(exc)
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except DjangoValidationError as exc:
        # Surface Django model/form validation errors as structured isError=True content
        # so agents receive actionable feedback in the same format as DRFValidationError.
        msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"error": msg})}],
                "isError": True,
            },
        )
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
    """Handle ``resources/list`` — return all registered resources."""
    return _jsonrpc_success(request_id, {"resources": resource_registry.list_resources()})


def _handle_resources_read(request_id: JsonRpcId, params: JsonDict, request: Any) -> JsonResponse:
    """Handle ``resources/read`` — dispatch to a registered resource handler."""
    uri: Any = params.get("uri")
    if not uri or not isinstance(uri, str):
        return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid params", "'uri' is required")

    try:
        text = resource_registry.read_resource(uri, request)
    except ResourceNotFoundError as exc:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Resource not found: {uri}", str(exc))

    defn = resource_registry.get_definition(uri)
    mime_type = defn.mime_type if defn is not None else "text/plain"

    return _jsonrpc_success(
        request_id,
        {"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]},
    )


def _handle_help(request_id: JsonRpcId) -> JsonResponse:
    """
    Handle ``help`` — return server metadata and usage hints for AI agents.

    Returns a structured summary of available methods, error formats, and
    navigation tips so that agents can self-orient without out-of-band
    documentation.
    """
    server_name: str = getattr(settings, "FRIESE_MCP_SERVER_NAME", "friese-mcp")
    return _jsonrpc_success(
        request_id,
        {
            "server": server_name,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "methods": [
                "initialize",
                "initialized",
                "tools/list",
                "tools/call",
                "resources/list",
                "ping",
                "help",
            ],
            "hints": {
                "discovery": (
                    "Call tools/list to enumerate available tools and their inputSchema."
                ),
                "invocation": ("Call tools/call with {name, arguments} to invoke a tool."),
                "errors": (
                    "Tool errors return isError=true with content[0].text as JSON. "
                    "Check the 'error' key for the message and 'detail' for field-level hints."
                ),
                "unknown_tool": (
                    "If tools/call returns -32601, the tool name is unrecognised. "
                    "Re-run tools/list for the correct name — suggestions are included in "
                    "the error data field."
                ),
            },
        },
    )


def _parse_and_dispatch(  # pylint: disable=too-many-branches
    request: HttpRequest | DRFRequest,
) -> JsonResponse | HttpResponse:
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

    # MCP Streamable HTTP (2025-03-26) §transport: when a POST body contains only
    # JSON-RPC *notifications* (messages without an ``id`` field), the server MUST
    # return HTTP 202 Accepted with no body.  Notifications have no ``id`` key at all
    # (distinct from an explicit ``"id": null`` on a request).
    is_notification = "id" not in body
    if is_notification:
        if method == "initialized":
            logger.info("mcp_initialized")
        else:
            logger.debug("mcp_notification", extra={"method": method})
        return HttpResponse(status=202)

    logger.debug("mcp_request", extra={"method": method, "request_id": request_id})

    if method == "ping":
        return _jsonrpc_success(request_id, {})
    if method == "initialize":
        return _handle_initialize(request_id, params)
    if method == "initialized":
        return _handle_initialized(request_id)
    if method == "tools/list":
        return _handle_tools_list(request_id, request)
    if method == "tools/call":
        return _handle_tools_call(request, request_id, params)
    if method == "resources/list":
        return _handle_resources_list(request_id)
    if method == "resources/read":
        return _handle_resources_read(request_id, params, request)
    if method == "help":
        return _handle_help(request_id)
    return _jsonrpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# SSE renderer — lets DRF content negotiation accept text/event-stream
# ---------------------------------------------------------------------------


class _EventStreamRenderer(BaseRenderer):
    """Passthrough renderer that satisfies DRF content negotiation for SSE."""

    media_type = "text/event-stream"
    format = "event-stream"

    def render(
        self, data: Any, accepted_media_type: str | None = None, renderer_context: Any = None
    ) -> Any:
        return data


# ---------------------------------------------------------------------------
# Main endpoint — DRF APIView
# ---------------------------------------------------------------------------


class McpEndpointView(APIView):
    """
    MCP gateway — single HTTP POST endpoint for all JSON-RPC 2.0 traffic.

    ``renderer_classes`` includes :class:`_EventStreamRenderer` so that DRF
    content negotiation accepts ``Accept: text/event-stream`` requests without
    raising HTTP 406.  The actual SSE wrapping is handled by :func:`_maybe_sse`;
    the renderer's ``render`` method is never invoked because ``post`` returns
    a raw :class:`~django.http.StreamingHttpResponse` that bypasses DRF rendering.

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

    renderer_classes = [JSONRenderer, _EventStreamRenderer]

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

    def post(self, request: DRFRequest, *args: Any, **kwargs: Any) -> JsonResponse | HttpResponse:
        """Handle POST — the only allowed HTTP method."""
        if not getattr(settings, "FRIESE_MCP_ENABLED", True):
            return _maybe_sse(
                JsonResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": INTERNAL_ERROR, "message": "MCP gateway is disabled"},
                    },
                    status=503,
                ),
                request,
            )
        return _maybe_sse(_parse_and_dispatch(request), request)

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
