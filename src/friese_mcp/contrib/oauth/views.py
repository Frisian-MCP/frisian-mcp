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

_AUTH_CODE_CACHE_PREFIX = "friese_mcp:oauth_code:"
_AUTH_CODE_TTL = 300  # 5 minutes

logger = logging.getLogger(__name__)


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

    proxy_count: int = getattr(settings, "FRIESE_MCP_TRUSTED_PROXY_COUNT", 0)
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

        access_token = OAuthAccessToken.objects.create(
            client=client, permission=client.permission
        )
        expiry: int = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)

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

        try:
            client = OAuthClient.objects.get(client_id=client_id, is_active=True)
        except OAuthClient.DoesNotExist:
            # PKCE clients (e.g. Claude.ai, Cursor) generate their own client_id and
            # never pre-register.  When FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER is True the
            # server creates a new OAuthClient on first use rather than rejecting the
            # exchange.  The permission tier defaults to
            # FRIESE_MCP_OAUTH_PKCE_DEFAULT_PERMISSION (default "read").
            if not getattr(settings, "FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER", False):
                return JsonResponse(
                    {"error": "invalid_client", "error_description": "Unknown or inactive client."},
                    status=401,
                )
            default_permission = getattr(
                settings, "FRIESE_MCP_OAUTH_PKCE_DEFAULT_PERMISSION", "read"
            )
            client = OAuthClient.objects.create(
                client_id=client_id,
                name=f"pkce-{client_id[:8]}",
                permission=default_permission,
                redirect_uris=[redirect_uri],
            )
            logger.info("oauth_pkce_client_auto_registered", extra={"client_id": client_id})

        access_token = OAuthAccessToken.objects.create(
            client=client, permission=client.permission
        )
        expiry: int = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)

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

        client = OAuthClient.objects.create(
            name=client_name.strip(),
            redirect_uris=list(raw_uris),
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
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
        base = _get_base_url(request)
        token_path: str = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_PATH", "/oauth/token/")
        token_endpoint = f"{base}{token_path}"

        # authorization_endpoint: prefer the override setting, fall back to package default.
        authorize_url_override: str = getattr(settings, "FRIESE_MCP_OAUTH_AUTHORIZE_URL", "")
        if authorize_url_override:
            authorization_endpoint = authorize_url_override
        else:
            authorize_path: str = getattr(
                settings, "FRIESE_MCP_OAUTH_AUTHORIZE_PATH", "/oauth/authorize/"
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

        # Advertise registration_endpoint when DCR is enabled (default True).
        # FRIESE_MCP_OAUTH_DCR controls advertisement independently of
        # FRIESE_MCP_OAUTH_REGISTRATION_OPEN — DCR clients need to see the
        # endpoint to auto-register before the authorize step.
        dcr_enabled: bool = getattr(settings, "FRIESE_MCP_OAUTH_DCR", True)
        if dcr_enabled or getattr(settings, "FRIESE_MCP_OAUTH_REGISTRATION_OPEN", False):
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

    def get(self, request: HttpRequest, **kwargs: object) -> JsonResponse:
        """Return MCP OAuth Protected Resource Metadata."""
        base = _get_base_url(request)
        mcp_path: str = getattr(settings, "FRIESE_MCP_PATH", "/mcp/")
        resource_url = f"{base}/{mcp_path.lstrip('/')}"

        return JsonResponse(
            {
                "resource": resource_url,
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp:read", "mcp:write", "mcp:admin"],
            }
        )


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
    Return the default for ``FRIESE_MCP_OAUTH_AUTO_APPROVE``.

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
          to be auto-registered via ``FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER``)
        * ``redirect_uri`` — must HTTPS or loopback (SEC-2) and exact-match
          one of the client's registered ``redirect_uris``
        * ``code_challenge`` — base64url(sha256(code_verifier))
        * ``code_challenge_method`` — must be ``"S256"``

    Optional:
        * ``state`` — opaque value echoed back in the redirect

    Behaviour is controlled by ``FRIESE_MCP_OAUTH_AUTO_APPROVE``.  Defaults
    to ``True`` only when ``settings.DEBUG`` is also ``True``; production
    deployments default to consent (False) per SEC-2 to prevent silent
    code issuance.

    * ``True``: immediately redirects to *redirect_uri* with an authorization code.
      Appropriate for developer / machine-to-machine flows.
    * ``False``: renders ``friese_mcp/oauth/authorize.html`` with a consent form.
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

        if getattr(settings, "FRIESE_MCP_OAUTH_AUTO_APPROVE", _auto_approve_default()):
            return self._issue_code_redirect(
                client_id, redirect_uri, code_challenge, state
            )

        # Render consent page
        return render(
            request,
            "friese_mcp/oauth/authorize.html",
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
        # accepted only when FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER is True.
        try:
            client = OAuthClient.objects.get(client_id=client_id, is_active=True)
        except OAuthClient.DoesNotExist:
            if getattr(settings, "FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER", False):
                return ""
            return "invalid_client"
        registered: list[str] = list(client.redirect_uris or [])
        if redirect_uri not in registered:
            # Heal clients auto-registered before the redirect_uris fix (empty list).
            if getattr(settings, "FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER", False) and not registered:
                client.redirect_uris = [redirect_uri]
                client.save(update_fields=["redirect_uris"])
                logger.info(
                    "oauth_pkce_client_redirect_uri_healed",
                    extra={"client_id": client_id, "redirect_uri": redirect_uri},
                )
                return ""
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

    def _error_redirect(
        self, redirect_uri: str, error: str, state: str
    ) -> HttpResponseRedirect:
        """Redirect to redirect_uri with an error parameter."""
        params: dict[str, str] = {"error": error}
        if state:
            params["state"] = state
        return _OAuthRedirect(f"{redirect_uri}?{urlencode(params)}")
