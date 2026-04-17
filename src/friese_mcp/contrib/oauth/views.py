"""
OAuth 2.0 HTTP views for friese_mcp.contrib.oauth.

Endpoints
---------
``POST /oauth/token/``
    Client credentials token endpoint (RFC 6749 §4.4).  Accepts
    ``application/x-www-form-urlencoded`` *or* ``application/json``.

``POST /oauth/register/``
    Dynamic client registration (RFC 7591).  Disabled by default;
    enable by setting ``FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True``.

``GET /.well-known/oauth-authorization-server``
    Authorization server metadata (RFC 8414).

``GET /.well-known/oauth-protected-resource``
    Protected resource metadata (MCP spec §Authorization).

URL configuration example::

    # urls.py
    from django.urls import include, path

    urlpatterns = [
        path("oauth/", include("friese_mcp.contrib.oauth.urls")),
        path(".well-known/", include("friese_mcp.contrib.oauth.wellknown_urls")),
        path("mcp/", include("friese_mcp.urls")),
    ]

"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import OAuthAccessToken, OAuthClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_body(request: HttpRequest) -> dict[str, Any]:
    """Parse request body from either form-encoded or JSON content."""
    ct = request.content_type or ""
    if "json" in ct:
        try:
            body = json.loads(request.body)
            return body if isinstance(body, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    # Fall back to form-encoded (standard for OAuth token endpoint, RFC 6749)
    return dict(request.POST)


def _str_field(data: dict[str, Any], key: str) -> str:
    """Extract a single string value from a parsed request body."""
    val = data.get(key, "")
    if isinstance(val, list):
        return val[0] if val else ""
    return str(val) if val else ""


def _get_base_url(request: HttpRequest) -> str:
    """Return the server base URL for building OAuth metadata URLs."""
    issuer: str = getattr(settings, "FRIESE_MCP_OAUTH_ISSUER", "")
    if issuer:
        return issuer.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class TokenView(View):
    """
    OAuth 2.0 token endpoint — ``client_credentials`` grant only.

    Accepts ``application/x-www-form-urlencoded`` (RFC 6749 §4.4) or
    ``application/json``.

    Required parameters:
        * ``grant_type`` — must be ``"client_credentials"``
        * ``client_id``
        * ``client_secret``

    Returns a JSON response with ``access_token``, ``token_type``,
    ``expires_in``, and ``scope``.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Issue a new access token for valid client credentials."""
        data = _parse_body(request)
        grant_type = _str_field(data, "grant_type")
        client_id = _str_field(data, "client_id")
        client_secret = _str_field(data, "client_secret")

        if grant_type != "client_credentials":
            return JsonResponse(
                {
                    "error": "unsupported_grant_type",
                    "error_description": "Only client_credentials grant is supported.",
                },
                status=400,
            )

        if not client_id or not client_secret:
            return JsonResponse(
                {
                    "error": "invalid_request",
                    "error_description": "client_id and client_secret are required.",
                },
                status=400,
            )

        try:
            client = OAuthClient.objects.get(
                client_id=client_id,
                client_secret=client_secret,
            )
        except OAuthClient.DoesNotExist:
            logger.warning("oauth_token_invalid_credentials", extra={"client_id": client_id})
            return JsonResponse(
                {
                    "error": "invalid_client",
                    "error_description": "Invalid client credentials.",
                },
                status=401,
            )

        if not client.is_active:
            return JsonResponse(
                {
                    "error": "invalid_client",
                    "error_description": "Client is inactive.",
                },
                status=401,
            )

        access_token = OAuthAccessToken.objects.create(client=client, scope=client.scope)
        expiry: int = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)

        logger.info("oauth_token_issued", extra={"client_name": client.name})
        return JsonResponse(
            {
                "access_token": access_token.token,
                "token_type": "Bearer",
                "expires_in": expiry,
                "scope": access_token.scope,
            }
        )

    def http_method_not_allowed(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        """Return 405 for non-POST methods."""
        return JsonResponse({"error": "method_not_allowed"}, status=405)


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class RegistrationView(View):
    """
    RFC 7591 dynamic client registration endpoint.

    Disabled by default.  Enable by setting
    ``FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True`` in Django settings.

    Accepts ``application/json`` with at minimum ``{"client_name": "<name>"}``.
    Returns 201 with ``client_id``, ``client_secret``, and ``client_name``.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Register a new OAuth client dynamically."""
        if not getattr(settings, "FRIESE_MCP_OAUTH_REGISTRATION_OPEN", False):
            return JsonResponse(
                {
                    "error": "registration_not_supported",
                    "error_description": (
                        "Dynamic client registration is disabled on this server."
                    ),
                },
                status=403,
            )

        try:
            body: Any = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse(
                {"error": "invalid_request", "error_description": "Request body must be JSON."},
                status=400,
            )

        if not isinstance(body, dict):
            return JsonResponse(
                {
                    "error": "invalid_request",
                    "error_description": "Request body must be a JSON object.",
                },
                status=400,
            )

        client_name: Any = body.get("client_name", "")
        if not client_name or not isinstance(client_name, str):
            return JsonResponse(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "client_name is required.",
                },
                status=400,
            )

        scope: str = body.get("scope", "mcp") if isinstance(body.get("scope"), str) else "mcp"
        client = OAuthClient.objects.create(name=client_name.strip(), scope=scope)

        logger.info("oauth_client_registered", extra={"client_name": client.name})
        return JsonResponse(
            {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "client_name": client.name,
                "scope": client.scope,
            },
            status=201,
        )

    def http_method_not_allowed(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        """Return 405 for non-POST methods."""
        return JsonResponse({"error": "method_not_allowed"}, status=405)


# ---------------------------------------------------------------------------
# Well-known metadata endpoints
# ---------------------------------------------------------------------------


class OAuthAuthorizationServerView(View):
    """
    RFC 8414 OAuth Authorization Server Metadata.

    Returns a JSON document describing the token endpoint, supported grant
    types, and (if enabled) the registration endpoint.

    Canonical path: ``GET /.well-known/oauth-authorization-server``
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
        base = _get_base_url(request)
        token_path: str = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_PATH", "/oauth/token/")
        token_endpoint = f"{base}{token_path}"

        metadata: dict[str, Any] = {
            "issuer": base,
            "token_endpoint": token_endpoint,
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "response_types_supported": ["token"],
            "scopes_supported": ["mcp"],
        }

        if getattr(settings, "FRIESE_MCP_OAUTH_REGISTRATION_OPEN", False):
            reg_path: str = getattr(settings, "FRIESE_MCP_OAUTH_REGISTER_PATH", "/oauth/register/")
            metadata["registration_endpoint"] = f"{base}{reg_path}"

        return JsonResponse(metadata)


class OAuthProtectedResourceView(View):
    """
    MCP-spec OAuth Protected Resource Metadata.

    Returns a JSON document describing the MCP resource server so that
    MCP clients can discover the authorization server.

    Canonical path: ``GET /.well-known/oauth-protected-resource``
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return MCP OAuth Protected Resource Metadata."""
        base = _get_base_url(request)
        mcp_path: str = getattr(settings, "FRIESE_MCP_PATH", "/mcp/")
        resource_url = f"{base}{mcp_path}"

        return JsonResponse(
            {
                "resource": resource_url,
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp"],
            }
        )
