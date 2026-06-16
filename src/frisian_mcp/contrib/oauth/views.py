"""
OAuth 2.0 HTTP views for frisian_mcp.contrib.oauth.

Endpoints
---------
``POST /oauth/token/``
    Client credentials token endpoint (RFC 6749 §4.4).  Accepts
    ``application/x-www-form-urlencoded`` *or* ``application/json``.

``POST /oauth/register/``
    Dynamic client registration (RFC 7591).  Disabled by default;
    enable by setting ``FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True``.

``GET /.well-known/oauth-authorization-server``
    Authorization server metadata (RFC 8414).

``GET /.well-known/oauth-protected-resource``
    Protected resource metadata (MCP spec §Authorization).

URL configuration example::

    # urls.py
    from django.urls import include, path

    urlpatterns = [
        path("oauth/", include("frisian_mcp.contrib.oauth.urls")),
        path(".well-known/", include("frisian_mcp.contrib.oauth.wellknown_urls")),
        path("mcp/", include("frisian_mcp.urls")),
    ]

"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.core.cache import cache as django_cache
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import OAuthAccessToken, OAuthClient, _hmac_secret

_AUTH_CODE_CACHE_PREFIX = "frisian_mcp:oauth_code:"
_AUTH_CODE_TTL = 300  # 5 minutes

_TOKEN_RL_PREFIX = "frisian_mcp:oauth_token_rl:"  # noqa: S105  # cache key prefix, not a password
_RATE_LIMIT_PERIODS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}

logger = logging.getLogger(__name__)


def _get_client_ip(request: HttpRequest) -> str:
    """
    Return the best-guess client IP address.

    Respects ``FRISIAN_MCP_TRUSTED_PROXY_COUNT``: when set, reads the
    ``X-Forwarded-For`` header and returns the entry just before the
    rightmost *N* proxy-added entries (which are attacker-injectable
    upstream of the trust boundary).  Falls back to ``REMOTE_ADDR`` when
    no proxy count is configured.
    """
    proxy_count: int = getattr(settings, "FRISIAN_MCP_TRUSTED_PROXY_COUNT", 0)
    if proxy_count > 0:
        xff = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
        if xff:
            parts = [p.strip() for p in xff.split(",")]
            # The rightmost proxy_count entries are set by trusted proxies;
            # the entry just before them is the real originating client.
            index = max(0, len(parts) - proxy_count)
            return parts[index]
    return str(request.META.get("REMOTE_ADDR", ""))


def _token_rate_limit_exceeded(request: HttpRequest) -> bool:
    """
    Return ``True`` when the token endpoint rate limit is exceeded for this IP.

    Reads ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` (format ``"N/period"``,
    e.g. ``"10/minute"``).  Supported periods: ``second``, ``minute``,
    ``hour``, ``day``.

    Returns ``False`` (not exceeded) when the setting is absent, ``None``,
    or malformed — fail-open to avoid breaking token issuance on cache
    failure or misconfiguration.

    **Deployment note:** enable this in production to mitigate brute-force
    and credential-stuffing against client secrets.  A value of
    ``"20/minute"`` is a reasonable starting point for most deployments;
    tighten based on observed legitimate traffic.  Nginx / load-balancer
    rate limiting is a complementary layer and does not replace this.
    """
    rate_limit: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT", None)
    if not rate_limit:
        return False
    try:
        count_str, period = rate_limit.split("/", 1)
        max_count = int(count_str.strip())
        period_seconds = _RATE_LIMIT_PERIODS[period.strip().lower()]
    except (ValueError, KeyError):
        return False  # Misconfigured — fail open

    ip = _get_client_ip(request)
    cache_key = f"{_TOKEN_RL_PREFIX}{ip}"
    try:
        # add() is a no-op when the key already exists — sets counter to 0
        # with TTL only on the first request in the window.
        django_cache.add(cache_key, 0, period_seconds)
        count = django_cache.incr(cache_key)
    except Exception:  # pylint: disable=broad-except  # cache backend unavailable
        return False  # Fail open — do not block token issuance on cache errors
    return count > max_count


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """
    Return True if the PKCE S256 code_verifier matches code_challenge.

    Uses hmac.compare_digest() for constant-time comparison to prevent
    timing attacks.  Strips base64url padding per RFC 7636.
    """
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return _hmac.compare_digest(digest, code_challenge)


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
    # Fall back to form-encoded (standard for OAuth token endpoint, RFC 6749).
    # QueryDict.dict() flattens multi-valued keys to their last value, matching
    # the OAuth spec expectation that each parameter appears at most once.
    # Using dict(request.POST) instead would expose the raw MultiValueDict
    # internals ({key: [values]}), breaking callers that do direct str access.
    return request.POST.dict()


def _str_field(data: dict[str, Any], key: str) -> str:
    """Extract a single string value from a parsed request body."""
    val = data.get(key, "")
    if isinstance(val, list):
        return val[0] if val else ""
    return str(val) if val else ""


def _pkce_permission_for_uri(redirect_uri: str) -> str:
    """
    Return the OAuth permission tier for a PKCE client by its redirect_uri.

    Checks ``FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP`` (a ``dict[str, str]``
    mapping redirect_uri prefix → tier) and returns the first matching tier.
    Falls back to ``FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION`` (default
    ``"read"``) when no prefix matches.

    Example::

        FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP = {
            "https://public-client.example.com/": "read",
            "https://internal.corp.example.com/": "read_write",
            "com.example.admin:/": "admin",
        }
    """
    tier_map: dict[str, str] = getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP", {})
    for prefix, tier in tier_map.items():
        if redirect_uri.startswith(prefix):
            return tier
    return getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION", "read")


_SCOPE_TO_TIER: dict[str, str] = {
    "mcp:read": "read",
    "mcp:read mcp:write": "read_write",
    "mcp:read mcp:write mcp:admin": "admin",
}
_TIER_RANK: list[str] = ["read", "read_write", "admin"]


def _resolve_scope_permission(requested_scope: str, client_permission: str) -> str | None:
    """
    Map an optional RFC 6749 scope string to a permission tier.

    Returns the effective tier to use for token issuance, or ``None`` when the
    requested scope exceeds the client's permitted tier.

    * No scope requested → use the client's full tier (no change).
    * Valid scope within client tier → downscope or match (allowed).
    * Valid scope exceeding client tier → ``None`` (reject).
    * Unrecognised scope string → ``None`` (reject).
    """
    if not requested_scope:
        return client_permission
    tier = _SCOPE_TO_TIER.get(requested_scope.strip())
    if tier is None:
        return None
    client_rank = _TIER_RANK.index(client_permission) if client_permission in _TIER_RANK else 0
    requested_rank = _TIER_RANK.index(tier)
    if requested_rank > client_rank:
        return None
    return tier


def _get_base_url(request: HttpRequest) -> str:
    """Return the server base URL for building OAuth metadata URLs."""
    issuer: str = getattr(settings, "FRISIAN_MCP_OAUTH_ISSUER", "")
    if issuer:
        return issuer.rstrip("/")

    proxy_count: int = getattr(settings, "FRISIAN_MCP_TRUSTED_PROXY_COUNT", 0)
    if proxy_count > 0:
        xff_proto = request.META.get("HTTP_X_FORWARDED_PROTO", "").strip()
        # Use the LAST value — rightmost is set by the nearest trusted proxy.
        # The first value is attacker-injectable before the proxy chain.
        scheme = xff_proto.split(",")[-1].strip() or request.scheme
        xff_host = request.META.get("HTTP_X_FORWARDED_HOST", "").strip()
        host = xff_host.split(",")[-1].strip() if xff_host else request.get_host()
        return f"{scheme}://{host}"

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
        if _token_rate_limit_exceeded(request):
            return JsonResponse(
                {
                    "error": "rate_limit_exceeded",
                    "error_description": (
                        "Too many token requests from this client. Please retry later."
                    ),
                },
                status=429,
            )
        data = _parse_body(request)
        grant_type = _str_field(data, "grant_type")
        client_id = _str_field(data, "client_id")
        client_secret = _str_field(data, "client_secret")

        if grant_type == "authorization_code":
            return self._handle_authorization_code(data)

        if grant_type != "client_credentials":
            return JsonResponse(
                {
                    "error": "unsupported_grant_type",
                    "error_description": (
                        "Supported grant types: client_credentials, authorization_code."
                    ),
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
                client_secret=_hmac_secret(client_secret),
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

        # RFC 7591 §2: enforce per-client grant_types when specified.
        allowed_grants: list[str] = list(client.grant_types or [])
        if allowed_grants and "client_credentials" not in allowed_grants:
            return JsonResponse(
                {
                    "error": "unauthorized_client",
                    "error_description": (
                        "This client is not authorized for the client_credentials grant."
                    ),
                },
                status=400,
            )

        # RFC 6749 §4.4.2: optional scope parameter — honour downscoping.
        # If the caller requests a scope that is within the client's permitted
        # tier, issue a token at that (possibly lower) tier.  Requesting a
        # scope that exceeds the client's tier is rejected.
        requested_scope = _str_field(data, "scope")
        effective_permission = _resolve_scope_permission(requested_scope, client.permission)
        if effective_permission is None:
            return JsonResponse(
                {
                    "error": "invalid_scope",
                    "error_description": (
                        f"Requested scope '{requested_scope}' exceeds this client's "
                        f"permitted tier ('{client.permission}').  "
                        "Valid scopes: mcp:read, mcp:read mcp:write, "
                        "mcp:read mcp:write mcp:admin."
                    ),
                },
                status=400,
            )

        access_token = OAuthAccessToken.objects.create(
            client=client, permission=effective_permission
        )
        expiry: int = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)

        logger.info("oauth_token_issued", extra={"client_name": client.name})
        return JsonResponse(
            {
                # SEC-1: access_token.token is the HMAC digest; the raw Bearer
                # value is exposed once via plaintext_token on the freshly-saved
                # instance.  This is the only place the raw value leaves the
                # server.
                "access_token": access_token.plaintext_token,
                "token_type": "Bearer",
                "expires_in": expiry,
                "scope": access_token.scope_string,
            }
        )

    def _handle_authorization_code(self, data: dict[str, Any]) -> JsonResponse:
        """Exchange an authorization code (PKCE) for a Bearer token."""
        code = _str_field(data, "code")
        redirect_uri = _str_field(data, "redirect_uri")
        client_id = _str_field(data, "client_id")
        code_verifier = _str_field(data, "code_verifier")

        if not code or not redirect_uri or not client_id or not code_verifier:
            return JsonResponse(
                {
                    "error": "invalid_request",
                    "error_description": (
                        "code, redirect_uri, client_id, and code_verifier are required."
                    ),
                },
                status=400,
            )

        cached = django_cache.get(f"{_AUTH_CODE_CACHE_PREFIX}{code}")
        if cached is None:
            return JsonResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "Authorization code expired or not found.",
                },
                status=400,
            )

        if cached["client_id"] != client_id:
            return JsonResponse(
                {"error": "invalid_grant", "error_description": "client_id mismatch."},
                status=400,
            )
        if cached["redirect_uri"] != redirect_uri:
            return JsonResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch."},
                status=400,
            )

        # PKCE S256: constant-time comparison via _verify_pkce (RFC 7636)
        if not _verify_pkce(code_verifier, cached["code_challenge"]):
            return JsonResponse(
                {"error": "invalid_grant", "error_description": "PKCE code_verifier mismatch."},
                status=400,
            )

        # One-time use: delete the code
        django_cache.delete(f"{_AUTH_CODE_CACHE_PREFIX}{code}")

        pkce_auto: bool = getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER", False)
        # Resolve the permission tier for this redirect_uri.  When
        # FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP is configured, different
        # clients (keyed by their redirect_uri prefix) can receive different
        # default tiers so that public-facing clients (e.g. Claude.ai) and
        # internal privileged clients are not forced to share the same grant.
        effective_permission: str = _pkce_permission_for_uri(redirect_uri)
        try:
            client = OAuthClient.objects.get(client_id=client_id, is_active=True)
            # When the resolved permission for this redirect_uri has been raised
            # (e.g. the tier map now grants "read_write" where "read" was issued
            # previously), promote the existing PKCE client so reconnections pick
            # up the new grant without requiring a DB reset.
            if pkce_auto and client.permission != effective_permission:
                _tier_rank = ["read", "read_write", "admin"]
                existing_rank = (
                    _tier_rank.index(client.permission) if client.permission in _tier_rank else 0
                )
                effective_rank = (
                    _tier_rank.index(effective_permission)
                    if effective_permission in _tier_rank
                    else 0
                )
                if effective_rank > existing_rank:
                    client.permission = effective_permission
                    client.save(update_fields=["permission"])
                    logger.info(
                        "oauth_pkce_client_permission_promoted",
                        extra={"client_id": client_id, "new_permission": effective_permission},
                    )
        except OAuthClient.DoesNotExist:
            # PKCE clients (e.g. Claude.ai, Cursor) generate their own client_id and
            # never pre-register.  When FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER is True the
            # server creates a new OAuthClient on first use rather than rejecting the
            # exchange.
            if not pkce_auto:
                return JsonResponse(
                    {"error": "invalid_client", "error_description": "Unknown or inactive client."},
                    status=401,
                )
            if len(client_id) > 255:
                return JsonResponse(
                    {
                        "error": "invalid_client",
                        "error_description": "client_id exceeds maximum length of 255 characters.",
                    },
                    status=400,
                )
            client = OAuthClient.objects.create(
                client_id=client_id,
                name=f"pkce-{client_id[:24]}",
                permission=effective_permission,
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code"],
            )
            logger.info("oauth_pkce_client_auto_registered", extra={"client_id": client_id})
        else:
            # RFC 7591 §2: enforce per-client grant_types for pre-registered clients.
            if client.grant_types and "authorization_code" not in client.grant_types:
                return JsonResponse(
                    {
                        "error": "unauthorized_client",
                        "error_description": (
                            "This client is not authorized for the authorization_code grant."
                        ),
                    },
                    status=400,
                )

        access_token = OAuthAccessToken.objects.create(client=client, permission=client.permission)
        expiry: int = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)

        logger.info("oauth_token_issued_code_flow", extra={"client_name": client.name})
        return JsonResponse(
            {
                # SEC-1: see _handle_client_credentials for the rationale.
                "access_token": access_token.plaintext_token,
                "token_type": "Bearer",
                "expires_in": expiry,
                "scope": access_token.scope_string,
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
    ``FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True`` in Django settings.

    Accepts ``application/json`` with at minimum ``{"client_name": "<name>"}``.
    Returns 201 with ``client_id``, ``client_secret``, and ``client_name``.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Register a new OAuth client dynamically."""
        if not getattr(settings, "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN", False):
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

        # SEC-2: accept and validate the registered redirect_uris list.
        # RFC 7591 §2.0 lets clients register multiple redirect URIs at
        # creation time; the authorize endpoint will require an exact
        # match against this list before issuing codes.
        raw_uris: Any = body.get("redirect_uris", [])
        if not isinstance(raw_uris, list) or not all(isinstance(u, str) for u in raw_uris):
            return JsonResponse(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "redirect_uris must be a list of strings.",
                },
                status=400,
            )
        for candidate in raw_uris:
            if not _redirect_uri_is_safe(candidate):
                return JsonResponse(
                    {
                        "error": "invalid_redirect_uri",
                        "error_description": (
                            f"redirect_uri {candidate!r} must be HTTPS, a "
                            "loopback http:// URI, or a custom-scheme native-app URI."
                        ),
                    },
                    status=400,
                )

        # Resolve tier by the first registered redirect_uri so DCR clients also
        # benefit from FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP.  Falls back to
        # FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION when no URIs are supplied or
        # none match the map.
        first_uri = raw_uris[0] if raw_uris else ""
        reg_permission: str = _pkce_permission_for_uri(first_uri)
        client = OAuthClient.objects.create(
            name=client_name.strip(),
            redirect_uris=list(raw_uris),
            permission=reg_permission,
        )

        logger.info(
            "oauth_client_registered",
            extra={"client_name": client.name, "redirect_uri_count": len(raw_uris)},
        )
        return JsonResponse(
            {
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
                "client_name": client.name,
                "redirect_uris": client.redirect_uris,
                "scope": client.scope_string,
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

    Hidden behind ``FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY`` (default ``True``):
    when ``False``, returns a JSON 404 so discovery-first MCP clients do not
    receive metadata advertising a closed authorization server.  Pre-shared
    OAuth clients continue to work with hard-coded endpoint URLs.
    """

    def get(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """
        Return OAuth 2.0 Authorization Server Metadata (RFC 8414).

        ``**kwargs`` absorbs the optional ``<path:resource>`` URL capture from
        the RFC 8414 §3 path-scoped variant.  All resources behind the same
        issuer share one authorization server, so the response is identical
        whether or not a resource suffix was supplied.
        """
        if not getattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY", True):
            return JsonResponse({"error": "not_found"}, status=404)
        base = _get_base_url(request)
        token_path: str = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_PATH", "/oauth/token/")
        token_endpoint = f"{base}{token_path}"

        # authorization_endpoint: prefer the override setting, fall back to package default.
        authorize_url_override: str = getattr(settings, "FRISIAN_MCP_OAUTH_AUTHORIZE_URL", "")
        if authorize_url_override:
            authorization_endpoint = authorize_url_override
        else:
            authorize_path: str = getattr(
                settings, "FRISIAN_MCP_OAUTH_AUTHORIZE_PATH", "/oauth/authorize/"
            )
            authorization_endpoint = f"{base}{authorize_path}"

        metadata: dict[str, Any] = {
            "issuer": base,
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "grant_types_supported": ["client_credentials", "authorization_code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
            # SEC-2: only the authorization-code flow is implemented; do not
            # advertise the implicit-flow ``token`` response type.
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp:read", "mcp:write", "mcp:admin"],
        }

        # Only advertise registration_endpoint when dynamic client registration
        # is actually open (RFC 8414 §2).  Advertising the endpoint when
        # FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False is misleading — clients that
        # discover the URL will receive a 403, violating the principle that
        # metadata describes real server capabilities.
        if getattr(settings, "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN", False):
            reg_path: str = getattr(settings, "FRISIAN_MCP_OAUTH_REGISTER_PATH", "/oauth/register/")
            metadata["registration_endpoint"] = f"{base}{reg_path}"

        return JsonResponse(metadata)


class OAuthProtectedResourceView(View):
    """
    MCP-spec OAuth Protected Resource Metadata.

    Returns a JSON document describing the MCP resource server so that
    MCP clients can discover the authorization server.

    Canonical path: ``GET /.well-known/oauth-protected-resource``

    Hidden behind ``FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY`` (default ``True``):
    when ``False``, returns a JSON 404 so the same setting controls both
    well-known endpoints together.
    """

    def get(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """Return MCP OAuth Protected Resource Metadata."""
        if not getattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY", True):
            return JsonResponse({"error": "not_found"}, status=404)
        base = _get_base_url(request)
        mcp_path: str = str(
            getattr(settings, "FRISIAN_MCP_PROTECTED_PATH", None)
            or getattr(settings, "FRISIAN_MCP_PATH", "/mcp/")
        )
        resource_url = f"{base}/{mcp_path.lstrip('/')}"

        return JsonResponse(
            {
                "resource": resource_url,
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp:read", "mcp:write", "mcp:admin"],
            }
        )


class OpenIDConfigurationView(View):
    """
    Stub for ``/.well-known/openid-configuration`` (OIDC discovery).

    Discovery-first MCP clients (e.g. Claude Code) probe this URL in addition
    to RFC 8414's ``oauth-authorization-server``.  This package does not
    implement OIDC, so the view always returns a JSON 404 ``{"error":
    "not_found"}``.  Claiming the URL at the package level prevents the
    request from falling through to the host application's HTML 404 page,
    which clients parsing the response as JSON cannot handle (``SyntaxError:
    Unrecognized token '<'``).
    """

    def get(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """Return a JSON 404 so the discovery cascade fails parseably."""
        return JsonResponse({"error": "not_found"}, status=404)


@method_decorator(csrf_exempt, name="dispatch")
class BareRegisterView(View):
    """
    Stub for the bare ``/register`` path (RFC 7591 default location).

    Clients that do not find ``registration_endpoint`` in the authorization
    server metadata may fall back to ``POST /register`` per RFC 7591 §3.
    The canonical registration endpoint in this package is ``/oauth/register/``;
    the bare path is intentionally not implemented.  Returning a JSON 404 here
    keeps the discovery cascade parseable instead of leaking the host's HTML
    404 page.

    ``GET`` and ``POST`` both return the same JSON 404.
    """

    def _json_404(self) -> JsonResponse:
        return JsonResponse({"error": "not_found"}, status=404)

    def get(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """Return a JSON 404."""
        return self._json_404()

    def post(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """Return a JSON 404 — dynamic registration lives at ``/oauth/register/``."""
        return self._json_404()


# ---------------------------------------------------------------------------
# Authorization code endpoint (RFC 6749 §4.1 + PKCE RFC 7636)
# ---------------------------------------------------------------------------


#: Loopback hosts that are exempt from the HTTPS redirect-URI requirement
#: (RFC 8252 §7.3 native-app loopback redirect).
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _redirect_uri_is_safe(redirect_uri: str) -> bool:
    """
    Return True when *redirect_uri* is acceptable as an OAuth redirect target.

    Per SEC-2 / RFC 6749 §3.1.2.1 and RFC 8252 §7.1, §7.3:

    * The scheme MUST be ``https`` for any non-loopback host.
    * Loopback hosts (``localhost``, ``127.0.0.1``, ``::1``) MAY use
      plain ``http`` because the traffic never leaves the developer's
      machine.
    * A reverse-DNS custom scheme (e.g. ``com.example.app:/callback``) is
      accepted as a native-app redirect — the dot in the scheme keeps
      ``javascript:``, ``data:``, ``file:``, ``vbscript:``, etc. out.

    Returns ``False`` for any other shape (URIs with no scheme,
    ``http`` to public hosts, ``javascript:`` and similar single-token
    schemes).
    """
    if not redirect_uri:
        return False
    parsed = urlparse(redirect_uri)
    scheme = parsed.scheme.lower()
    if not scheme:
        return False
    if scheme == "https":
        return True
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        return host in _LOOPBACK_HOSTS
    # Reverse-DNS custom scheme convention (RFC 8252 §7.1): native-app
    # schemes are expected to use the app's domain in reverse-DNS form so
    # they are unique across the platform.  Requiring a ``.`` in the scheme
    # filters out single-token URI schemes that browsers historically used
    # for passive content (``javascript``, ``data``, ``vbscript``, ``file``,
    # ``mailto``) — none of which should ever appear in an OAuth redirect.
    return "." in scheme and not scheme.startswith(".")


class _AnySchemeAllowed:
    """
    Sentinel ``__contains__``-true container for HttpResponseRedirect.

    Django's :class:`~django.http.HttpResponseRedirect` rejects any redirect
    whose scheme is not in the class-level ``allowed_schemes`` allowlist
    (``http``, ``https``, ``ftp`` by default).  Native-app PKCE flows
    redirect to reverse-DNS custom schemes (``com.example.app:/cb``), which
    that allowlist would otherwise block.

    AuthorizeView already vets the redirect URI through
    :func:`_redirect_uri_is_safe` and the per-client allowlist before
    constructing the redirect, so the response itself can safely defer to
    those checks.  Setting ``allowed_schemes`` to an instance of this class
    short-circuits the redundant scheme check inside Django.
    """

    def __contains__(self, scheme: object) -> bool:
        """Return ``True`` for any scheme (upstream validation is the trust source)."""
        return True


class _OAuthRedirect(HttpResponseRedirect):
    """``HttpResponseRedirect`` permitting custom schemes for vetted OAuth URIs."""

    allowed_schemes = _AnySchemeAllowed()  # type: ignore[assignment]


def _auto_approve_default() -> bool:
    """
    Return the default for ``FRISIAN_MCP_OAUTH_AUTO_APPROVE``.

    Defaults to ``True`` only when ``settings.DEBUG`` is also ``True``.
    Production deployments (DEBUG=False) MUST explicitly opt in to
    auto-approval via the setting — silently issuing codes without consent
    is unsafe in any non-developer context.
    """
    return bool(getattr(settings, "DEBUG", False))


class AuthorizeView(View):
    """
    OAuth 2.0 authorization code endpoint with PKCE (RFC 7636).

    Canonical path: ``GET /oauth/authorize/``

    Required query parameters:
        * ``response_type`` — must be ``"code"``
        * ``client_id`` — must refer to an active :class:`OAuthClient` (or
          to be auto-registered via ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER``)
        * ``redirect_uri`` — must HTTPS or loopback (SEC-2) and exact-match
          one of the client's registered ``redirect_uris``
        * ``code_challenge`` — base64url(sha256(code_verifier))
        * ``code_challenge_method`` — must be ``"S256"``

    Optional:
        * ``state`` — opaque value echoed back in the redirect

    **Design: no session authentication gate (intentional)**

    This endpoint intentionally does **not** require ``request.user.is_authenticated``
    or ``@login_required``.  The primary use case is machine-to-machine (M2M)
    authorization: the "principal" being authorized is the OAuth *client*
    (identified by ``client_id`` + PKCE), not a logged-in human user.  Requiring a
    Django session would break automated agent flows where no browser session exists.

    The security boundary is the PKCE code challenge / code verifier exchange — the
    client proves possession of the secret at token-exchange time (RFC 7636 §4.6)
    rather than via a session cookie.

    **Host apps that need user-consent flows:**

    If your application needs a human user to approve an OAuth client before an
    authorization code is issued (e.g. a third-party integration), you have two
    options:

    1. **Wrap the URL with ``login_required``** in your own ``urlconf`` instead of
       including ``frisian_mcp.contrib.oauth.urls`` directly::

           from django.contrib.auth.decorators import login_required
           from frisian_mcp.contrib.oauth.views import AuthorizeView

           urlpatterns = [
               path(
                   "oauth/authorize/",
                   login_required(AuthorizeView.as_view()),
                   name="oauth_authorize",
               ),
           ]

    2. **Set ``FRISIAN_MCP_OAUTH_AUTO_APPROVE = False``** (the production default)
       and override ``frisian_mcp/oauth/authorize.html`` in your project's template
       directory.  Your template can render inside a layout that already enforces
       authentication (e.g. wrapped in a ``{% if user.is_authenticated %}`` guard or
       rendered by a view mixin that redirects to login).  The POST handler
       re-validates all parameters, so the session check only needs to live in
       the template or the URL wrapper — not duplicated in this view.

    **Behaviour controlled by FRISIAN_MCP_OAUTH_AUTO_APPROVE:**

    Defaults to ``True`` only when ``settings.DEBUG`` is also ``True``; production
    deployments default to ``False`` per SEC-2 to prevent silent code issuance.

    * ``True``: immediately redirects to *redirect_uri* with an authorization code.
      Appropriate for developer / machine-to-machine flows.
    * ``False``: renders ``frisian_mcp/oauth/authorize.html`` with a consent form.
      POST the form with ``allow=true`` or ``allow=false`` to proceed.
      Host apps may override the template via standard Django template discovery.
    """

    def get(self, request: HttpRequest) -> Any:
        """Handle the initial authorization request."""
        response_type = request.GET.get("response_type", "")
        client_id = request.GET.get("client_id", "")
        redirect_uri = request.GET.get("redirect_uri", "")
        code_challenge = request.GET.get("code_challenge", "")
        code_challenge_method = request.GET.get("code_challenge_method", "")
        state = request.GET.get("state", "")

        error = self._validate_authorize_params(
            response_type, client_id, redirect_uri, code_challenge, code_challenge_method
        )
        if error:
            # SEC-2: only redirect back to redirect_uri after we have CONFIRMED
            # it is a registered URI for the named client.  ``error`` is set
            # to ``"invalid_redirect_uri"`` when validation rejected the URI
            # itself; in that case we MUST return a JSON 400 so an attacker
            # cannot be redirected to an arbitrary target with a state echo.
            if redirect_uri and error not in {"invalid_redirect_uri", "invalid_client"}:
                return self._error_redirect(redirect_uri, error, state)
            return JsonResponse({"error": error}, status=400)

        if getattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE", _auto_approve_default()):
            return self._issue_code_redirect(client_id, redirect_uri, code_challenge, state)

        # Render consent page
        return render(
            request,
            "frisian_mcp/oauth/authorize.html",
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "state": state,
            },
        )

    def post(self, request: HttpRequest) -> Any:
        """Handle the consent form submission (auto_approve=False path)."""
        client_id = request.POST.get("client_id", "")
        redirect_uri = request.POST.get("redirect_uri", "")
        code_challenge = request.POST.get("code_challenge", "")
        state = request.POST.get("state", "")
        allow = request.POST.get("allow", "false").lower() == "true"

        # Re-validate on POST: a malicious form submitter cannot bypass the
        # GET-side allowlist by hand-crafting the consent POST.
        error = self._validate_authorize_params(
            "code", client_id, redirect_uri, code_challenge, "S256"
        )
        if error:
            if redirect_uri and error not in {"invalid_redirect_uri", "invalid_client"}:
                return self._error_redirect(redirect_uri, error, state)
            return JsonResponse({"error": error}, status=400)

        if not allow:
            return self._error_redirect(redirect_uri, "access_denied", state)

        return self._issue_code_redirect(client_id, redirect_uri, code_challenge, state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_authorize_params(
        self,
        response_type: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        """
        Return an error string or empty string if params are valid.

        Validation order matters: cheap shape checks first, then the
        scheme/loopback check, then the per-client allowlist.  We DO NOT
        emit ``invalid_redirect_uri`` until the URI itself was rejected;
        that error code signals the caller (``get()``) to refuse the
        redirect and return JSON 400 instead.
        """
        if response_type != "code":
            return "unsupported_response_type"
        if not client_id:
            return "invalid_request"
        if not redirect_uri:
            return "invalid_request"
        if not code_challenge:
            return "invalid_request"
        if code_challenge_method != "S256":
            return "invalid_request"
        # SEC-2: scheme/loopback gate runs BEFORE the client lookup so a
        # javascript: or http://evil.example URI is rejected even when the
        # client_id is bogus and PKCE auto-register would otherwise accept it.
        if not _redirect_uri_is_safe(redirect_uri):
            return "invalid_redirect_uri"
        # SEC-2: client allowlist.  An OAuthClient row is the registration
        # source of truth; an empty redirect_uris list means "this client may
        # not use the authorize endpoint".  PKCE clients without a DB row are
        # accepted only when FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER is True.
        #
        # Enforce client_id length at the authorize step (not only at token
        # exchange) so that unauthenticated callers cannot flood the cache
        # with codes for arbitrarily-long synthetic client_ids.
        if len(client_id) > 255:
            return "invalid_request"
        try:
            client = OAuthClient.objects.get(client_id=client_id, is_active=True)
        except OAuthClient.DoesNotExist:
            if getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER", False):
                return ""
            return "invalid_client"
        registered: list[str] = list(client.redirect_uris or [])
        if redirect_uri not in registered:
            # Do NOT silently add the caller-supplied URI to the client's
            # registered list ("redirect_uri healing").  An empty redirect_uris
            # list means the client was created before redirect_uri support was
            # added; it must be updated via Django admin or re-registered.
            # Auto-healing lets any caller inject an arbitrary redirect target
            # into an existing client's allowlist.
            return "invalid_redirect_uri"
        return ""

    def _issue_code_redirect(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
    ) -> HttpResponseRedirect:
        """Generate an auth code, cache it, and redirect to redirect_uri."""
        code = secrets.token_urlsafe(32)
        django_cache.set(
            f"{_AUTH_CODE_CACHE_PREFIX}{code}",
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
            },
            _AUTH_CODE_TTL,
        )
        params: dict[str, str] = {"code": code}
        if state:
            params["state"] = state
        return _OAuthRedirect(f"{redirect_uri}?{urlencode(params)}")

    def _error_redirect(self, redirect_uri: str, error: str, state: str) -> HttpResponseRedirect:
        """Redirect to redirect_uri with an error parameter."""
        params: dict[str, str] = {"error": error}
        if state:
            params["state"] = state
        return _OAuthRedirect(f"{redirect_uri}?{urlencode(params)}")
