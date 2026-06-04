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
:class:`McpView` (formerly ``McpEndpointView``) extends DRF's
:class:`~rest_framework.views.APIView`, which
means host projects can gate the MCP surface using standard DRF mechanisms:

* ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` — list of dotted-path strings *or*
  class objects; falls back to DRF's ``DEFAULT_AUTHENTICATION_CLASSES``.
* ``FRISIAN_MCP_PERMISSION_CLASSES``    — list of dotted-path strings *or*
  class objects; defaults to ``[]`` (no gateway-level permission check) for
  backwards compatibility.  Tool-level ``permission_classes`` are enforced
  separately by :data:`~frisian_mcp.registry.tool_registry`.
"""
# pylint: disable=too-many-lines

import asyncio
import base64
import difflib
import hashlib
import importlib.metadata
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

from django.conf import settings
from django.core.cache import cache as django_cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request as DRFRequest
from rest_framework.views import APIView

from frisian_mcp.backends.base import ToolResult
from frisian_mcp.middleware import build_middleware_chain, get_middleware_instances
from frisian_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    JsonDict,
    JsonRpcId,
)
from frisian_mcp.registry import (
    ToolInputError,
    ToolInvocationError,
    ToolNotFoundError,
    tool_registry,
)
from frisian_mcp.resources import ResourceNotFoundError, resource_registry

logger = logging.getLogger(__name__)

_TOOLS_LIST_CACHE_KEY = "frisian_mcp:tools_list"
_HEAVY_CACHE_PREFIX = "frisian_mcp:heavy:"
_HEAVY_CACHE_TTL: int = 300  # seconds; tokens expire after 5 minutes

_REFRESH_HINT = (
    " Call tools/list to refresh your available tools"
    " — the server manifest may have changed."
)

#: Substrings used to detect instructional scaffolding text that the
#: ``lite: true`` per-call flag should strip from dispatcher help responses.
#: Matched case-insensitively against the *value* of any string field on the
#: dispatcher help payload (e.g. a ``hints`` map keyed by tool name).  A field
#: whose value contains any of these substrings is removed from the lite
#: response so the agent receives the action list without re-teaching text.
_LITE_SCAFFOLDING_SUBSTRINGS: tuple[str, ...] = (
    "use action=",
    "use action ",
    "call tools/list",
    "tools/list to refresh",
)


def _strip_lite_scaffolding(payload: Any) -> Any:
    """
    Return a copy of *payload* with instructional scaffolding removed.

    Applied on every successful ``tools/call`` response when the caller passes
    ``lite: true``.  The lite contract is:

    * Drop the ``hints`` map (operator-supplied navigation hints).
    * Remove any top-level string field whose value contains
      instructional scaffolding (``"use action='help'"`` and similar) —
      detected by :data:`_LITE_SCAFFOLDING_SUBSTRINGS`.
    * On dispatcher help responses (``payload["help"] is True``): also
      reduce each entry in the ``actions`` list to its ``name`` string
      (drop ``description``, ``input_schema``, ``params``).
    * Leave every other field untouched — data is never stripped.

    Args:
        payload: The dispatch result returned by the tool's ``invoke``.

    Returns:
        A new dict (or the original *payload* unchanged when no stripping
        applies).  Never mutates *payload* in place.

    """
    if not isinstance(payload, dict):
        return payload

    is_help = payload.get("help") is True
    stripped: dict[str, Any] = {}
    for key, value in payload.items():
        if is_help and key == "actions" and isinstance(value, list):
            # Reduce action entries to plain name strings (help responses only).
            # Each entry may be a dict (single dispatcher) or a string (group
            # dispatcher: action names only) — handle both shapes uniformly.
            names: list[str] = []
            for entry in value:
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str):
                        names.append(name)
                elif isinstance(entry, str):
                    names.append(entry)
            stripped[key] = names
            continue
        if key == "hints":
            # Operator-supplied navigation hints are scaffolding by definition.
            continue
        if isinstance(value, str) and any(
            marker in value.lower() for marker in _LITE_SCAFFOLDING_SUBSTRINGS
        ):
            continue
        stripped[key] = value
    return stripped


def _get_token_permission(request: Any) -> str:
    """
    Return the effective permission tier for this request.

    Delegates to :func:`frisian_mcp.registry._resolve_request_tier` so that the
    full resolution chain (``FRISIAN_MCP_RESOLVE_TIER`` callable hook,
    ``request.auth.permission``, ``FRISIAN_MCP_TOKEN_TIER_MAP`` role map, and
    fallback) is applied in one canonical place.  Retained as a thin shim for
    backwards compatibility with code (and tests) that imports
    ``views._get_token_permission`` directly.

    If ``request._mcp_max_tier`` is set (stamped by :meth:`McpView.post` from
    the view's :meth:`~McpView._effective_max_tier`), the resolved tier is
    clamped to that maximum.  This is the ``FRISIAN_MCP_MAX_TIER`` mechanism:
    even a superuser hitting an open endpoint receives at most the declared cap.
    """
    from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
        _resolve_request_tier,
    )

    return _resolve_request_tier(request)


def invalidate_tools_list_cache() -> None:
    """
    Delete the cached tools/list manifest so the next request rebuilds it.

    Call this after registering tools at runtime when
    ``FRISIAN_MCP_TOOLS_LIST_CACHE_TTL`` is set, rather than waiting for the
    TTL to expire naturally.
    """
    from frisian_mcp.registry import _TIER_RANK  # pylint: disable=import-outside-toplevel

    # Delete per-tier keys + the legacy :all key (written by any custom code using
    # max_tier=None → cache_key={key}:all in older deployments).
    keys = [f"{_TOOLS_LIST_CACHE_KEY}:all"] + [
        f"{_TOOLS_LIST_CACHE_KEY}:{tier}" for tier in _TIER_RANK
    ]
    django_cache.delete_many(keys)


# ---------------------------------------------------------------------------
# Heavy response-negotiation helpers
# ---------------------------------------------------------------------------


def _heavy_owner_key(request: Any, tool_name: str) -> str:
    """
    Return a stable identifier for the caller of a heavy tool invocation.

    SEC-3: heavy-response continuation tokens cache the result under a
    server-issued opaque token.  Without binding, anyone who learns the
    token (a leaked log, a compromised middlebox, a different agent on
    the same gateway) could replay it and read another caller's data.
    The owner key composes:

    * the originating tool name — refuses replay against a different tool
    * the auth backend type + primary key — refuses cross-credential replay
    * the effective permission tier — refuses replay after a downgrade
    * the user PK if any — refuses cross-user replay
    * the agent connection PK if the request is per-agent scoped (PKG-6)
    * the MCP session ID if the client supplied one

    The shape is intentionally a single string so the comparison is a
    simple equality check; the exact field set need not be stable across
    releases because the owner key never leaves the server.
    """
    auth_obj = getattr(request, "auth", None)
    if auth_obj is None:
        auth_id = "anon"
    else:
        pk = getattr(auth_obj, "pk", None)
        if pk is not None:
            auth_id = f"{type(auth_obj).__name__}:{pk}"
        else:
            # Static API keys (_ApiKeyAuth) have no PK; fall back to the
            # type + permission tier so two distinct tiers don't collide.
            auth_id = (
                f"{type(auth_obj).__name__}:tier="
                f"{getattr(auth_obj, 'permission', 'unknown')}"
            )

    user = getattr(request, "user", None)
    user_pk = getattr(user, "pk", None) if user is not None else None
    user_part = f":user={user_pk}" if user_pk is not None else ""

    # Tier resolution flows through registry._resolve_request_tier (PKG-15)
    # so SEC-3 inherits the same hook/role-map chain — no duplicated logic.
    tier = _get_token_permission(request)

    conn = getattr(request, "_mcp_agent_connection", None)
    conn_pk = getattr(conn, "pk", None) if conn is not None else None
    conn_part = f":conn={conn_pk}" if conn_pk is not None else ""

    session_id = (request.META or {}).get("HTTP_MCP_SESSION_ID", "")
    session_part = f":session={session_id}" if session_id else ""

    return (
        f"tool={tool_name}:auth={auth_id}:tier={tier}"
        f"{user_part}{conn_part}{session_part}"
    )


def _build_heavy_cache_entry(
    result: Any, request: Any, tool_name: str
) -> dict[str, Any]:
    """Wrap *result* with the SEC-3 owner-binding metadata for the cache."""
    return {
        "result": result,
        "owner_key": _heavy_owner_key(request, tool_name),
        "tool_name": tool_name,
    }


def _build_probe_envelope(result: Any, token: str) -> dict[str, Any]:
    """Build the call-1 probe envelope for the two-call response-negotiation protocol."""
    serialized = json.dumps(result)
    if isinstance(result, dict):
        preview = json.dumps({k: str(v)[:80] for k, v in list(result.items())[:5]})
    elif isinstance(result, list):
        preview = json.dumps(result[:3])
    else:
        preview = serialized[:200]
    return {
        "preview": preview[:200],
        "total_size": len(serialized.encode()),
        "available_modes": ["summary", "paginated", "filtered", "full"],
        "continuation_token": token,
    }


def _serve_heavy_mode(result: Any, mode: str, arguments: dict[str, Any]) -> Any:
    """
    Serve a cached heavy result in the requested response mode.

    Modes:
    * ``summary``   — first 10 dict keys / 5 list items; values truncated to 100 chars
    * ``paginated`` — one page of a list or JSON-chunk of a scalar; honours ``page``
      and ``page_size`` arguments (default page=1, page_size=FRISIAN_MCP_HEAVY_PAGE_SIZE|20)
    * ``filtered``  — result filtered to the keys listed in ``filter_keys`` argument
    * ``full``      — complete cached result (default when mode is absent or unknown)
    """
    if mode == "full":
        return result

    if mode == "summary":
        if isinstance(result, dict):
            return {k: str(v)[:100] for k, v in list(result.items())[:10]}
        if isinstance(result, list):
            return result[:5]
        return {"summary": str(result)[:500]}

    if mode == "paginated":
        page: int = max(1, int(arguments.get("page", 1)))
        page_size: int = max(
            1,
            int(arguments.get("page_size", getattr(settings, "FRISIAN_MCP_HEAVY_PAGE_SIZE", 20))),
        )
        if isinstance(result, list):
            start = (page - 1) * page_size
            end = start + page_size
            return {
                "items": result[start:end],
                "page": page,
                "page_size": page_size,
                "total": len(result),
                "has_more": end < len(result),
            }
        serialized = json.dumps(result)
        chunk_size = page_size * 100
        start = (page - 1) * chunk_size
        end = start + chunk_size
        return {"chunk": serialized[start:end], "page": page, "has_more": end < len(serialized)}

    if mode == "filtered":
        filter_keys: list[str] = list(arguments.get("filter_keys") or [])
        if isinstance(result, dict) and filter_keys:
            return {k: v for k, v in result.items() if k in filter_keys}
        if isinstance(result, list) and filter_keys:
            return [
                {k: item[k] for k in filter_keys if k in item}
                if isinstance(item, dict)
                else item
                for item in result
            ]
        return result

    return result  # unknown mode → full


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
    data: Any = None,
) -> JsonResponse:
    """
    Return a JSON-RPC 2.0 error response.

    *data* may be any JSON-serialisable value.  Strings remain the common
    case (e.g. close-match suggestions for an unknown tool); dicts are used
    by the lite-mode escape hatch (see :func:`_lite_enrich_error`) to attach
    the failing tool's ``inputSchema`` so the agent can self-teach without
    a separate ``tools/list`` round-trip.
    """
    error: JsonDict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


def _lite_enrich_error_content(
    content: dict[str, Any], tool_name: str, lite: bool
) -> dict[str, Any]:
    """
    Attach the failing tool's ``inputSchema`` to an ``isError`` content dict.

    ``tools/call`` failures that surface as ``isError=true`` content blocks
    (rather than JSON-RPC ``error`` responses) carry their detail in
    ``content[0].text`` as JSON.  This helper mirrors :func:`_lite_enrich_error`
    for that path: when *lite* is ``True`` and the tool exists, attach the
    tool's ``inputSchema`` to the content dict so the agent can self-correct.

    Args:
        content: The dict that will be JSON-serialised into
            ``content[0].text`` of an ``isError=true`` response.
        tool_name: The tool name the caller invoked.
        lite: The per-call ``lite`` flag extracted from arguments.

    Returns:
        Either *content* unchanged, or a new dict with ``"inputSchema"``
        added.  Never mutates *content* in place.

    """
    if not lite or not tool_name:
        return content
    entry = tool_registry.get_entry(tool_name)
    if entry is None:
        return content
    return {**content, "inputSchema": entry.input_schema}


def _lite_enrich_error(
    response: JsonResponse, tool_name: str, lite: bool
) -> JsonResponse:
    """
    Attach the failing tool's ``inputSchema`` to a JSON-RPC error response.

    Implements the lite-mode escape hatch: when a caller passes
    ``lite: true`` on a ``tools/call`` and the call fails (bad params,
    unknown action, validation error, etc.), the response includes the
    tool's input schema so the agent can self-correct without re-fetching
    ``tools/list``.  Lite mode normally suppresses scaffolding; a failure
    re-includes it.

    When *lite* is ``False`` or the tool is not registered, *response* is
    returned unchanged.  Otherwise the response body is rewritten so that
    ``error.data`` is a structured dict containing ``"detail"`` (the original
    string data, when present) and ``"inputSchema"`` (the tool's schema).

    Args:
        response: The JSON-RPC error response built by :func:`_jsonrpc_error`.
        tool_name: The tool name the caller invoked.
        lite: The per-call ``lite`` flag extracted from arguments.

    Returns:
        Either *response* unchanged, or a new ``JsonResponse`` with the
        ``inputSchema`` attached to ``error.data``.

    """
    if not lite or not tool_name:
        return response
    entry = tool_registry.get_entry(tool_name)
    if entry is None:
        return response
    body = json.loads(response.content)
    err = body.get("error")
    if not isinstance(err, dict):
        return response
    existing = err.get("data")
    enriched: dict[str, Any]
    if isinstance(existing, dict):
        enriched = {**existing}
    elif existing is None:
        enriched = {}
    else:
        enriched = {"detail": existing}
    enriched["inputSchema"] = entry.input_schema
    err["data"] = enriched
    return JsonResponse(body)


# ---------------------------------------------------------------------------
# Error content builders
# ---------------------------------------------------------------------------


def _build_drf_error_content(exc: DRFValidationError) -> dict[str, Any]:
    """
    Convert a DRF ``ValidationError`` into a structured tool-error dict.

    * Field errors (dict detail) →
      ``{"error": "Validation failed", "detail": {field: [msgs]}, "status_code": 422}``
    * Bulk errors (list of dicts) →
      ``{"error": "Validation failed", "detail": ["row 0: …", …], "status_code": 422}``
    * Non-field errors (flat list) → ``{"error": "<joined messages>", "status_code": 422}``
    * Scalar detail → ``{"error": "<string>", "status_code": 422}``

    The result is safe to JSON-serialise and return to the MCP caller inside
    an ``isError=True`` content block.
    """
    from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
        _flatten_error_detail,
    )

    detail = exc.detail
    if isinstance(detail, dict):
        return {
            "error": "Validation failed",
            "detail": {
                field: [str(e) for e in (errors if isinstance(errors, list) else [errors])]
                for field, errors in detail.items()
            },
            "status_code": 422,
        }
    if isinstance(detail, list):
        if detail and isinstance(detail[0], (dict, list)):
            # Bulk validation: detail is a list of per-row error dicts.
            # Flatten each row using _flatten_error_detail to avoid Python repr()
            # of ErrorDetail objects (e.g. "{'field': [ErrorDetail(...)]}").
            rows: list[str] = []
            for i, row_err in enumerate(detail):
                msg = _flatten_error_detail(row_err)
                if msg:
                    rows.append(f"row {i}: {msg}")
            return {
                "error": "Validation failed",
                "detail": rows if rows else ["unknown error"],
                "status_code": 422,
            }
        return {"error": "; ".join(str(e) for e in detail), "status_code": 422}
    return {"error": str(detail), "status_code": 422}


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


def _resolve_agent_connection_state(request: Any) -> tuple[Any | None, bool]:
    """
    Look up the AgentConnection state for ``request.auth``.

    Returns ``(active_connection_or_None, has_inactive_match)``:

    * ``(conn, False)`` — at least one active AgentConnection links the
      credential; ``conn`` is the most-recent.  Apply per-agent filtering.
    * ``(None, True)`` — the credential IS linked to at least one
      AgentConnection but all of them are inactive.  SEC-5: fail closed.
      Operators who deactivate an agent expect access to stop, not for
      filtering to silently disappear.
    * ``(None, False)`` — no AgentConnection links this credential.  Pass
      through with no filtering (existing default-allow contract).

    Resolution order:

    1. ``frisian_mcp.contrib.agents`` not installed → ``(None, False)``.
    2. ``request.auth`` is a
       :class:`~frisian_mcp.contrib.tokens.models.FrisianMcpToken` → look up
       AgentConnections linked via ``token``.
    3. ``request.auth`` is an
       :class:`~frisian_mcp.contrib.oauth.models.OAuthAccessToken` → look up
       AgentConnections linked via the parent ``OAuthClient``.
    4. Otherwise → ``(None, False)``.
    """
    from django.apps import apps as django_apps  # pylint: disable=import-outside-toplevel

    if not django_apps.is_installed("frisian_mcp.contrib.agents"):
        return None, False

    auth = getattr(request, "auth", None)
    if auth is None:
        return None, False

    queryset = None

    try:
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        if isinstance(auth, FrisianMcpToken):
            queryset = auth.agent_connections.select_related("token", "oauth_client")
    except ImportError:
        pass

    if queryset is None:
        try:
            from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
                OAuthAccessToken,
            )

            if isinstance(auth, OAuthAccessToken):
                queryset = auth.client.agent_connections.select_related(
                    "token", "oauth_client"
                )
        except ImportError:
            pass

    if queryset is None:
        return None, False

    active = queryset.filter(is_active=True).order_by("-created_at").first()
    if active is not None:
        return active, False

    # No active match — but maybe the credential is linked to an inactive one.
    # SEC-5: an admin who deactivated the agent expects the credential to stop
    # working, not for filtering to silently disappear.
    has_inactive = queryset.exists()
    return None, has_inactive


def _get_agent_connection(request: Any) -> Any | None:
    """
    Return the active AgentConnection for ``request.auth``, or ``None``.

    Backwards-compatible thin wrapper around
    :func:`_resolve_agent_connection_state` that drops the
    ``has_inactive_match`` signal.  Callers that need the SEC-5 fail-closed
    behaviour should use the resolver directly.
    """
    conn, _ = _resolve_agent_connection_state(request)
    return conn


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------


def _maybe_sse(response: HttpResponse, request: Any) -> HttpResponse | StreamingHttpResponse:
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
    if "application/json" in accept:
        return response

    body: str = response.content.decode("utf-8")

    def _stream() -> Generator[str, None, None]:
        yield f"data: {body}\n\n"

    sse: StreamingHttpResponse = StreamingHttpResponse(
        _stream(), content_type="text/event-stream"
    )
    sse["Cache-Control"] = "no-cache"
    for header, value in response.items():
        if header.lower() not in ("content-type", "content-length"):
            sse[header] = value
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


def _server_version() -> str:
    """Return the installed frisian-mcp package version, or ``'unknown'`` as fallback."""
    try:
        return importlib.metadata.version("frisian-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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

    server_name: str = getattr(settings, "FRISIAN_MCP_SERVER_NAME", "frisian-mcp")
    tool_names = sorted(t["name"] for t in tool_registry.list_tools())
    tools_version = hashlib.sha256(",".join(tool_names).encode()).hexdigest()[:8]
    response = _jsonrpc_success(
        request_id,
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": server_name,
                "version": _server_version(),
            },
            "capabilities": {"tools": {}, "resources": {}},
            "toolsVersion": tools_version,
        },
    )
    if getattr(settings, "FRISIAN_MCP_SESSION_ID_HEADER", True):
        response["Mcp-Session-Id"] = str(uuid.uuid4())
    return response


def _handle_initialized(request_id: JsonRpcId) -> JsonResponse:
    """Handle ``initialized`` — client confirms handshake; acknowledgement only."""
    logger.info("mcp_initialized")
    return _jsonrpc_success(request_id, {})


def _decode_cursor(cursor: str) -> int:
    """
    Decode a base64url cursor string to an integer offset.

    Raises :exc:`ValueError` when *cursor* is not a valid base64url-encoded
    integer so the caller can surface INVALID_PARAMS to the client.
    """
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {cursor!r}") from exc


def _encode_cursor(offset: int) -> str:
    """Encode an integer offset as a base64url cursor string."""
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _handle_tools_list(  # pylint: disable=too-many-locals
    request_id: JsonRpcId, request: Any, params: JsonDict
) -> JsonResponse:
    """
    Handle ``tools/list`` — return the tool manifest from the registry.

    When ``frisian_mcp.contrib.agents`` is installed and ``request.auth`` maps to
    an active :class:`~frisian_mcp.contrib.agents.models.AgentConnection` with a
    non-null ``allowed_tools`` list, only those tools are included in the
    response.  All other callers receive the full manifest.

    When ``FRISIAN_MCP_TOOLS_PAGE_SIZE`` is set, results are paginated using an
    opaque base64url cursor that encodes a simple integer offset.  Clients pass
    the returned ``nextCursor`` in subsequent requests to advance through pages.
    When the setting is absent, all tools are returned in a single response with
    no ``nextCursor`` key (default, zero-behavior-change).

    **Auth note:** Beyond per-agent filtering, this handler does not perform
    additional authentication or permission checks.  The host application is
    responsible for gateway-level auth-gating via
    ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` / ``FRISIAN_MCP_PERMISSION_CLASSES`` or
    upstream infrastructure.
    """
    conn, has_inactive_match = _resolve_agent_connection_state(request)
    # SEC-5: when the credential is bound to AgentConnection(s) but every
    # one is inactive, fail closed — the operator deactivated the agent
    # and expects access to stop, not for filtering to silently disappear.
    if conn is None and has_inactive_match:
        return _jsonrpc_success(request_id, {"tools": []})
    max_tier = _get_token_permission(request)
    cache_ttl: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_LIST_CACHE_TTL", None)
    # Use a per-tier cache key so authenticated requests benefit from caching too.
    cache_key = f"{_TOOLS_LIST_CACHE_KEY}:{max_tier or 'all'}"
    use_cache = cache_ttl is not None and (conn is None or conn.allowed_tools is None)

    if use_cache:
        tools: list[dict[str, Any]] | None = django_cache.get(cache_key)
        if tools is None:
            tools = tool_registry.list_tools(max_tier=max_tier)
            django_cache.set(cache_key, tools, cache_ttl)
    else:
        tools = tool_registry.list_tools(max_tier=max_tier)

    if conn is not None and conn.allowed_tools is not None:
        allowed: frozenset[str] = frozenset(conn.allowed_tools)
        tools = [t for t in tools if t["name"] in allowed]

    page_size: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_PAGE_SIZE", None)
    if page_size is None:
        return _jsonrpc_success(request_id, {"tools": tools})

    cursor_str: Any = params.get("cursor")
    offset = 0
    if cursor_str is not None:
        try:
            offset = _decode_cursor(str(cursor_str))
        except ValueError:
            return _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid cursor")

    page = tools[offset : offset + page_size]
    result: dict[str, Any] = {"tools": page}
    next_offset = offset + page_size
    if next_offset < len(tools):
        result["nextCursor"] = _encode_cursor(next_offset)
    return _jsonrpc_success(request_id, result)


def _handle_tools_call(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
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

    # Per-call ``lite`` opt-in (Issue 4 / Repeated-Path Token Reduction).  When
    # ``lite: true`` is present we (a) suppress dispatcher help scaffolding on
    # success and (b) re-include the tool's ``inputSchema`` on failure as a
    # self-teaching escape hatch.  Strip it from ``arguments`` here so the
    # underlying tool implementation never sees the protocol flag.  Pattern
    # matches how ``verify`` is stripped on the write-path below.
    arguments = dict(arguments)
    _lite: bool = bool(arguments.pop("lite", False))
    # Fallback: agents that cached an older schema (before ``lite`` was added as
    # a top-level dispatcher property) pass ``lite`` inside the ``params`` bag.
    # Extract it there too so lite works regardless of which schema the agent saw.
    if not _lite and isinstance(arguments.get("params"), dict):
        arguments["params"] = dict(arguments["params"])
        _lite = bool(arguments["params"].pop("lite", False))

    # Per-agent tool allowlist: when an active AgentConnection with a non-null
    # allowed_tools list is linked to the caller's credential, reject any tool
    # name not in that list before reaching the registry.
    conn, has_inactive_match = _resolve_agent_connection_state(request)
    # SEC-5: fail closed when the credential is bound to AgentConnection(s)
    # but every one is inactive.  Returning isError=true (not a JSON-RPC
    # protocol error) so MCP clients render it as a normal tool denial and
    # the JSON-RPC session stays alive for the caller to inspect.
    if conn is None and has_inactive_match:
        return _jsonrpc_success(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "error": (
                                    "Agent connection is inactive; this credential "
                                    "is not currently authorised to call MCP tools."
                                )
                            }
                        ),
                    }
                ],
                "isError": True,
            },
        )
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

    # Heavy response negotiation: if continuation_token is present, serve the cached
    # result without dispatching to the tool again.  This short-circuits schema
    # validation, which is intentional — call-2 arguments only need the token + mode.
    cont_token: str | None = arguments.get("continuation_token")
    if cont_token is not None:
        cached = django_cache.get(f"{_HEAVY_CACHE_PREFIX}{cont_token}")
        # SEC-3: legacy raw-result entries (pre-fix deploys) lack the owner
        # binding and are treated as expired — better a brief disruption
        # during cutover than serving cross-caller data.
        is_bound = isinstance(cached, dict) and "owner_key" in cached and "result" in cached
        if cached is None or not is_bound:
            return _jsonrpc_success(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": (
                                        "Continuation token expired or not found."
                                        " Re-invoke without continuation_token"
                                        " to start a new negotiation."
                                    )
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        # SEC-3: refuse to serve when the current caller does not match the
        # caller that issued the continuation.  Owner key composes auth
        # identity, tier, user, agent connection, and tool name; any drift
        # (different token, different tool, downgraded tier, different
        # agent connection) terminates the negotiation safely.
        expected_owner: str = cached.get("owner_key", "")
        actual_owner = _heavy_owner_key(request, tool_name)
        if expected_owner != actual_owner:
            logger.warning(
                "heavy_continuation_owner_mismatch",
                extra={"tool": tool_name},
            )
            return _jsonrpc_success(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": (
                                        "Continuation token does not belong to this"
                                        " caller / tool / session.  Re-invoke without"
                                        " continuation_token to start a new negotiation."
                                    )
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        _mode: str = arguments.get("mode", "full")
        served = _serve_heavy_mode(cached["result"], _mode, arguments)
        return _jsonrpc_success(
            request_id,
            {"content": [{"type": "text", "text": json.dumps(served)}], "isError": False},
        )

    # Record last_seen_at for this agent connection (fire-and-forget UPDATE).
    if conn is not None:
        from frisian_mcp.contrib.agents.models import (  # pylint: disable=import-outside-toplevel
            AgentConnection,
        )

        AgentConnection.objects.filter(pk=conn.pk).update(last_seen_at=timezone.now())

    # Write-path filtering: strip the `verify` negotiation flag before dispatching
    # to the invocation backend.  The ViewSet serializer must never see it — it
    # is a frisian-mcp protocol param, not a model field.
    _write_entry = tool_registry.get_entry(tool_name)
    _verify = False
    _dispatch_arguments = arguments
    if _write_entry is not None and _write_entry.is_write:
        _verify = bool(arguments.get("verify", False))
        if "verify" in arguments:
            _dispatch_arguments = {k: v for k, v in arguments.items() if k != "verify"}

    try:
        result = build_middleware_chain(
            _tool_registry_dispatch, get_middleware_instances()
        )(request, tool_name, _dispatch_arguments)
    except ToolNotFoundError as exc:
        # JSON-RPC 2.0: -32601 METHOD_NOT_FOUND is the correct code for an unknown
        # tool name.  -32602 INVALID_PARAMS is reserved for structural argument
        # errors; using it for a missing tool misleads clients into thinking their
        # call format is wrong rather than the tool name.
        #
        # Append close-match suggestions so agents can self-correct without an
        # extra tools/list round-trip.
        known_names = [
            t["name"] for t in tool_registry.list_tools(max_tier=_get_token_permission(request))
        ]
        suggestions = difflib.get_close_matches(tool_name, known_names, n=3, cutoff=0.6)
        data = str(exc)
        if suggestions:
            data += f". Did you mean: {', '.join(suggestions)}?"
        data += f" Available tools: {', '.join(sorted(known_names))}."
        data += _REFRESH_HINT
        # Lite-mode escape hatch is a no-op here: the tool is unknown so the
        # registry has no inputSchema to re-include.  The close-match
        # suggestions in ``data`` are already the agent's recovery path.
        return _lite_enrich_error(
            _jsonrpc_error(request_id, METHOD_NOT_FOUND, "Unknown tool", data),
            tool_name,
            _lite,
        )
    except LookupError as exc:
        # LookupError from inside a registered tool (e.g. group dispatcher raises
        # LookupError for an unknown resource/action pair).  Distinct from the
        # tool-not-found case above: the MCP tool exists, but the sub-action does
        # not.  Surface as isError:true so the agent can self-correct.
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 404}, tool_name, _lite
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except ToolInputError as exc:
        # Lite-mode escape hatch: when the call fails because arguments did not
        # validate against ``inputSchema``, re-include the schema so the agent
        # can self-correct without a separate ``tools/list`` round-trip.
        return _lite_enrich_error(
            _jsonrpc_error(request_id, INVALID_PARAMS, "Invalid arguments", str(exc)),
            tool_name,
            _lite,
        )
    except ToolInvocationError as exc:
        # Backend returned ToolResult.is_error=True (non-DRF exception in the ViewSet).
        # Forward the actual error content so the agent receives actionable feedback
        # instead of the generic "Internal tool error" fallback.
        content = exc.content if isinstance(exc.content, dict) else {"error": str(exc.content)}
        if "status_code" not in content:
            content = {**content, "status_code": 500}
        content = _lite_enrich_error_content(content, tool_name, _lite)
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except PermissionError as exc:
        # Return as isError=True tool-level content, not a JSON-RPC protocol error.
        # INVALID_PARAMS (-32602) is reserved for argument structure failures; using
        # it for auth denial misleads agents into thinking their call format is wrong.
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 403}, tool_name, _lite
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except DRFValidationError as exc:
        # IT-8: Surface DRF field-level validation errors with structured detail so
        # the caller can display per-field messages without parsing a flat string.
        content = _build_drf_error_content(exc)
        content = _lite_enrich_error_content(content, tool_name, _lite)
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
        content = _lite_enrich_error_content(
            {"error": msg, "status_code": 400}, tool_name, _lite
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except ValueError as exc:
        # IT-3: Surface ValueError raised by @mcp_tool handlers — the convention is to
        # raise ValueError for user-correctable input problems (e.g. invalid UUID, bad
        # enum value).  Return as a tool-level isError response so the caller gets
        # actionable feedback without a full JSON-RPC error.
        content = _lite_enrich_error_content(
            {"error": str(exc), "status_code": 400}, tool_name, _lite
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("tool_execution_error", extra={"tool": tool_name, "error": str(exc)})
        content = _lite_enrich_error_content(
            {
                "error": (
                    str(exc)
                    if getattr(settings, "FRISIAN_MCP_EXPOSE_ERRORS", settings.DEBUG)
                    else "Internal tool error"
                ),
                "status_code": 500,
            },
            tool_name,
            _lite,
        )
        return _jsonrpc_success(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": True,
            },
        )

    # Unwrap ToolResult from DRF-backed tools so the write-path lean envelope
    # can access the actual HTTP status (201 for creates, 204 for deletes, etc.).
    # Custom @mcp_tool callables return plain Python objects, not ToolResult, so
    # the isinstance check is a no-op for those paths.
    _http_status: int = 200
    if isinstance(result, ToolResult):
        _http_status = result.http_status
        result = result.content

    # Lite-mode success post-processing: when the caller passed ``lite: true``
    # and the result is a dispatcher help payload (``{"help": True, ...}``),
    # strip the instructional scaffolding (action descriptions, input
    # schemas, navigation hints) so the agent receives action names only.
    # Non-help dispatcher results, write/heavy envelopes, and plain tool
    # results are left unchanged — lite never strips operation data, only
    # the re-teaching wrapper.  See :func:`_strip_lite_scaffolding`.
    if _lite and _write_entry is not None and _write_entry.is_dispatcher:
        result = _strip_lite_scaffolding(result)

    # Write-path lean default: cache the full result and return a compact
    # confirmation envelope unless the caller passed verify=True.  @mcp_heavy
    # takes precedence when a tool is decorated with both.
    if _write_entry is not None and _write_entry.is_write and not (
        _write_entry is not None and _write_entry.is_heavy
    ):
        if _verify:
            return _jsonrpc_success(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False},
            )
        from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
            _extract_lean_envelope,
        )
        _w_token = secrets.token_urlsafe(16)
        _lean = _extract_lean_envelope(result, _w_token, _http_status)
        if "continuation_token" in _lean:
            django_cache.set(
                f"{_HEAVY_CACHE_PREFIX}{_w_token}",
                _build_heavy_cache_entry(result, request, tool_name),
                _HEAVY_CACHE_TTL,
            )
        elif _lean.get("deleted") is True:
            # Delete: enrich with the pk from original arguments.
            pk_val = arguments.get("pk") or arguments.get("id")
            if pk_val is not None:
                _lean["id"] = pk_val
        return _jsonrpc_success(
            request_id,
            {"content": [{"type": "text", "text": json.dumps(_lean)}], "isError": False},
        )

    # Dispatcher-routed write: a group dispatcher routed to a write-tier
    # underlying tool.  Apply the same lean/verify logic as the flat write
    # path.  `verify` was stripped from params by make_group_invoke before
    # the underlying tool ran, so we read it from the original arguments here.
    if _write_entry is not None and _write_entry.is_dispatcher:
        _d_resource = arguments.get("resource", "")
        _d_action = arguments.get("action", "")
        _d_params: dict[str, Any] = arguments.get("params") or {}
        _d_sep: str = getattr(settings, "FRISIAN_MCP_TOOL_NAME_SEPARATOR", "_")
        _d_target = f"{_d_resource}{_d_sep}{_d_action}" if _d_resource and _d_action else ""
        _d_entry = tool_registry.get_entry(_d_target) if _d_target else None
        if _d_entry is not None and _d_entry.is_write and not _d_entry.is_heavy:
            _d_verify = bool(_d_params.get("verify", False))
            if _d_verify:
                return _jsonrpc_success(
                    request_id,
                    {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False},
                )
            from frisian_mcp.backends.invocation import (  # pylint: disable=import-outside-toplevel
                _extract_lean_envelope,
            )
            _w_token = secrets.token_urlsafe(16)
            _d_lean = _extract_lean_envelope(result, _w_token, _http_status)
            if "continuation_token" in _d_lean:
                django_cache.set(
                    f"{_HEAVY_CACHE_PREFIX}{_w_token}",
                    _build_heavy_cache_entry(result, request, tool_name),
                    _HEAVY_CACHE_TTL,
                )
            elif _d_lean.get("deleted") is True:
                pk_val = _d_params.get("pk") or _d_params.get("id")
                if pk_val is not None:
                    _d_lean["id"] = pk_val
            return _jsonrpc_success(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(_d_lean)}], "isError": False},
            )

    # @mcp_heavy tools: cache the result and return a probe envelope so the agent
    # can choose how much of the response to retrieve on the follow-up call.
    _entry = tool_registry.get_entry(tool_name)
    if _entry is not None and _entry.is_heavy:
        _token = secrets.token_urlsafe(16)
        # SEC-3: bind the cache entry to the current caller so a leaked
        # continuation_token cannot be replayed by a different agent.
        django_cache.set(
            f"{_HEAVY_CACHE_PREFIX}{_token}",
            _build_heavy_cache_entry(result, request, tool_name),
            _HEAVY_CACHE_TTL,
        )
        probe = _build_probe_envelope(result, _token)
        return _jsonrpc_success(
            request_id,
            {"content": [{"type": "text", "text": json.dumps(probe)}], "isError": False},
        )

    # Threshold backstop (secondary, v2): auto-negotiate any tool response that exceeds
    # FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD bytes.  Prefer @mcp_heavy for explicit control.
    _threshold: int | None = getattr(settings, "FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD", None)
    if _threshold is not None:
        _serialized = json.dumps(result)
        if len(_serialized.encode()) > _threshold:
            _token = secrets.token_urlsafe(16)
            django_cache.set(
                f"{_HEAVY_CACHE_PREFIX}{_token}",
                _build_heavy_cache_entry(result, request, tool_name),
                _HEAVY_CACHE_TTL,
            )
            probe = _build_probe_envelope(result, _token)
            return _jsonrpc_success(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(probe)}], "isError": False},
            )

    return _jsonrpc_success(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        },
    )


def _handle_resources_list(request_id: JsonRpcId, request: Any) -> JsonResponse:
    """Handle ``resources/list`` — return all registered resources."""
    return _jsonrpc_success(request_id, {"resources": resource_registry.list_resources(request)})


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
    server_name: str = getattr(settings, "FRISIAN_MCP_SERVER_NAME", "frisian-mcp")
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
        return _handle_tools_list(request_id, request, params)
    if method == "tools/call":
        return _handle_tools_call(request, request_id, params)
    if method == "resources/list":
        return _handle_resources_list(request_id, request)
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


class McpView(APIView):
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
    ``FRISIAN_MCP_AUTHENTICATION_CLASSES``
        List of dotted-path strings or class objects.  When absent, DRF's
        ``DEFAULT_AUTHENTICATION_CLASSES`` are used.

    ``FRISIAN_MCP_PERMISSION_CLASSES``
        List of dotted-path strings or class objects.  Defaults to ``[]``
        (no gateway-level permission check) to preserve backwards
        compatibility.  Individual tools still enforce their own
        ``permission_classes`` via :data:`~frisian_mcp.registry.tool_registry`.

    Example (JWT-gated MCP surface)::

        # settings.py
        FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
        ]
        FRISIAN_MCP_PERMISSION_CLASSES = [
            "rest_framework.permissions.IsAuthenticated",
        ]

    """

    renderer_classes = [JSONRenderer, _EventStreamRenderer]

    def _effective_max_tier(self) -> str | None:
        """
        Return the tier cap for this endpoint, or ``None`` for no cap.

        Reads ``FRISIAN_MCP_MAX_TIER`` from settings.  Override in a subclass
        to pin a different cap (or ``None`` to disable it) without touching
        global settings — the auto-registered protected endpoint does exactly
        this so that authenticated callers receive their full tier there.
        """
        return getattr(settings, "FRISIAN_MCP_MAX_TIER", None)

    def get_authenticators(self) -> list[Any]:
        """
        Return authenticator instances for this view.

        Reads ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` from settings.  When the
        setting is absent, delegates to the DRF default.
        """
        classes = _resolve_classes("FRISIAN_MCP_AUTHENTICATION_CLASSES")
        if classes is None:
            return super().get_authenticators()
        return [cls() for cls in classes]

    def get_permissions(self) -> list[Any]:
        """
        Return permission instances for this view.

        Reads ``FRISIAN_MCP_PERMISSION_CLASSES`` from settings.  Defaults to
        ``[]`` when the setting is absent (backward compatible — no gateway
        permission check; tool-level permissions still apply).
        """
        classes = _resolve_classes("FRISIAN_MCP_PERMISSION_CLASSES")
        if classes is None:
            return []
        return [cls() for cls in classes]

    def get(
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> StreamingHttpResponse | HttpResponse:
        """
        Handle GET — open an SSE keepalive channel per MCP Streamable HTTP spec.

        When ``FRISIAN_MCP_SSE_CHANNEL`` is ``False``, the server does not
        support server-initiated messages and returns HTTP 405 so MCP clients
        fall back to receiving responses in the POST response body.  Use this
        for stateless deployments (e.g. multi-pod Kubernetes) that cannot route
        POST responses through a long-lived per-client SSE stream.

        When ``FRISIAN_MCP_SSE_CHANNEL`` is ``True`` (default), a keepalive
        comment is sent every 15 seconds to prevent proxy and client timeouts.
        """
        if not getattr(settings, "FRISIAN_MCP_SSE_CHANNEL", True):
            return HttpResponse(status=405)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # WSGI context — async generators cannot be iterated synchronously,
            # but a sync generator works fine: the worker thread blocks on
            # time.sleep() between keepalives while the HTTP chunked stream stays
            # open. This is preferable to returning 405 because some clients
            # (e.g. Cursor) do not fall back to POST-only on 405 and instead
            # treat it as a hard connection failure even though POST calls work.
            def _wsgi_keepalive() -> Generator[str, None, None]:
                while True:
                    yield ": keepalive\n\n"
                    time.sleep(15)

            resp = StreamingHttpResponse(_wsgi_keepalive(), content_type="text/event-stream")
            resp["Cache-Control"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            return resp

        async def _keepalive_stream() -> AsyncGenerator[str, None]:
            while True:
                yield ": keepalive\n\n"
                await asyncio.sleep(3)

        resp = StreamingHttpResponse(_keepalive_stream(), content_type="text/event-stream")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

    def post(
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse | HttpResponse | StreamingHttpResponse:
        """Handle POST — dispatch JSON-RPC 2.0 requests."""
        # Stamp the endpoint-level tier cap so _get_token_permission can apply
        # it throughout the request without re-reading settings on each call.
        request._mcp_max_tier = self._effective_max_tier()  # type: ignore[attr-defined]  # pylint: disable=protected-access
        if not getattr(settings, "FRISIAN_MCP_ENABLED", True):
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
        # Enforce a request-body size limit.  Django's DATA_UPLOAD_MAX_MEMORY_SIZE
        # only applies to multipart/form-encoded bodies; raw JSON POST bodies are
        # unbounded by default.  FRISIAN_MCP_REQUEST_BODY_MAX_SIZE (bytes, default
        # 1 MiB) protects against oversized payloads being loaded into memory by
        # json.loads() in _parse_and_dispatch.
        max_body: int = getattr(settings, "FRISIAN_MCP_REQUEST_BODY_MAX_SIZE", 1 * 1024 * 1024)
        if len(request.body) > max_body:
            return _maybe_sse(
                _jsonrpc_error(
                    None,
                    INVALID_REQUEST,
                    "Request body too large",
                    f"Maximum allowed size is {max_body} bytes.",
                ),
                request,
            )
        return _maybe_sse(_parse_and_dispatch(request), request)

    def delete(self, request: DRFRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Handle DELETE — stateless no-op for agent session-cleanup calls."""
        return JsonResponse({}, status=200)

    def http_method_not_allowed(  # type: ignore[override]
        self, request: DRFRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        """Return a JSON-RPC 2.0 error for non-POST methods."""
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": METHOD_NOT_FOUND, "message": "Method Not Allowed — POST only"},
            },
            status=405,
        )
