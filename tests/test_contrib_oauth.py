"""Tests for friese_mcp.contrib.oauth — OAuth 2.0 client_credentials flow."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from friese_mcp.contrib.oauth.authentication import OAuthServicePrincipal, OAuthTokenAuthentication
from friese_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient
from friese_mcp.contrib.oauth.views import (
    OAuthAuthorizationServerView,
    OAuthProtectedResourceView,
    RegistrationView,
    TokenView,
    _get_base_url,
)
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_mcp_view = McpEndpointView.as_view()
_token_view = TokenView.as_view()
_register_view = RegistrationView.as_view()
_auth_server_view = OAuthAuthorizationServerView.as_view()
_protected_resource_view = OAuthProtectedResourceView.as_view()


def _bearer(token: str) -> dict[str, str]:
    """Return a META dict with an Authorization: Bearer header."""
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post_mcp(rf: RequestFactory, payload: Any, meta: dict[str, str] | None = None) -> Any:
    """Build a POST request to the MCP endpoint."""
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if meta:
        kwargs.update(meta)
    return rf.post("/mcp/", data=json.dumps(payload), **kwargs)


def _post_token(rf: RequestFactory, data: dict[str, str]) -> Any:
    """Build a form-encoded POST request to the OAuth token endpoint."""
    return rf.post("/oauth/token/", data=data)


def _post_register(rf: RequestFactory, body: Any) -> Any:
    """Build a JSON POST request to the OAuth register endpoint."""
    return rf.post(
        "/oauth/register/",
        data=json.dumps(body),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# OAuthClient model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOAuthClientModel:
    """Tests for the OAuthClient model."""

    def test_client_id_auto_generated(self) -> None:
        """client_id is populated automatically on first save."""
        client = OAuthClient.objects.create(name="test-client")
        assert client.client_id
        assert len(client.client_id) == 32  # secrets.token_hex(16) → 32 hex chars

    def test_client_secret_auto_generated(self) -> None:
        """client_secret (stored HMAC) and plaintext_client_secret are populated on first save."""
        client = OAuthClient.objects.create(name="test-client")
        assert client.client_secret  # stored HMAC
        assert len(client.client_secret) == 64  # HMAC-SHA256 → 64 hex chars
        assert hasattr(client, "plaintext_client_secret")
        assert len(client.plaintext_client_secret) == 64  # raw: token_hex(32) → 64 hex chars

    def test_credentials_not_overwritten_on_update(self) -> None:
        """client_id and client_secret are preserved on subsequent saves."""
        client = OAuthClient.objects.create(name="test-client")
        original_id = client.client_id
        original_secret = client.client_secret
        client.name = "renamed"
        client.save()
        client.refresh_from_db()
        assert client.client_id == original_id
        assert client.client_secret == original_secret

    def test_str_active(self) -> None:
        """__str__ includes name and 'active' for an active client."""
        client = OAuthClient(name="claude-agent", is_active=True)
        assert "claude-agent" in str(client)
        assert "active" in str(client)

    def test_str_inactive(self) -> None:
        """__str__ includes 'inactive' for a deactivated client."""
        client = OAuthClient(name="old-client", is_active=False)
        assert "inactive" in str(client)

    def test_each_client_unique_credentials(self) -> None:
        """Two clients created back-to-back have different credentials."""
        c1 = OAuthClient.objects.create(name="c1")
        c2 = OAuthClient.objects.create(name="c2")
        assert c1.client_id != c2.client_id
        assert c1.client_secret != c2.client_secret
        assert c1.plaintext_client_secret != c2.plaintext_client_secret

    def test_default_scope_is_mcp(self) -> None:
        """Default scope is 'mcp'."""
        client = OAuthClient.objects.create(name="default-scope")
        assert client.scope == "mcp"

    def test_custom_scope_stored(self) -> None:
        """Custom scope is persisted correctly."""
        client = OAuthClient.objects.create(name="scoped", scope="mcp read write")
        client.refresh_from_db()
        assert client.scope == "mcp read write"

    def test_stored_secret_is_hmac_not_plaintext(self) -> None:
        """The stored client_secret is the HMAC, not the raw value."""
        client = OAuthClient.objects.create(name="hash-check")
        assert client.client_secret != client.plaintext_client_secret

    def test_plaintext_secret_absent_on_fresh_db_fetch(self) -> None:
        """plaintext_client_secret is not present on a freshly fetched instance."""
        client = OAuthClient.objects.create(name="reload-check")
        fetched = OAuthClient.objects.get(pk=client.pk)
        assert not hasattr(fetched, "plaintext_client_secret")

    def test_token_endpoint_rejects_hmac_as_secret(self, rf: RequestFactory) -> None:
        """Sending the stored HMAC as client_secret is rejected (wrong layer)."""
        client = OAuthClient.objects.create(name="hmac-secret-check")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.client_secret,  # HMAC, not raw
            },
        )
        response = _token_view(request)
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# OAuthAccessToken model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOAuthAccessTokenModel:
    """Tests for the OAuthAccessToken model."""

    def test_token_auto_generated(self) -> None:
        """Token field is populated automatically on first save."""
        client = OAuthClient.objects.create(name="c")
        token = OAuthAccessToken.objects.create(client=client)
        assert token.token
        assert len(token.token) == 64  # secrets.token_hex(32) → 64 hex chars

    def test_expires_at_set_on_creation(self) -> None:
        """expires_at is populated with a future timestamp on creation."""
        client = OAuthClient.objects.create(name="c")
        token = OAuthAccessToken.objects.create(client=client)
        assert token.expires_at > timezone.now()

    def test_default_expiry_approximately_one_hour(self) -> None:
        """Default expiry is ~3600 seconds in the future."""
        client = OAuthClient.objects.create(name="c")
        before = timezone.now()
        token = OAuthAccessToken.objects.create(client=client)
        after = timezone.now()
        lower = before + timedelta(seconds=3590)
        upper = after + timedelta(seconds=3610)
        assert lower <= token.expires_at <= upper

    def test_is_expired_false_for_fresh_token(self) -> None:
        """is_expired() returns False for a freshly created token."""
        client = OAuthClient.objects.create(name="c")
        token = OAuthAccessToken.objects.create(client=client)
        assert token.is_expired() is False

    def test_is_expired_true_for_old_token(self) -> None:
        """is_expired() returns True for a token with a past expires_at."""
        client = OAuthClient.objects.create(name="c")
        past = timezone.now() - timedelta(seconds=1)
        token = OAuthAccessToken.objects.create(client=client, expires_at=past)
        assert token.is_expired() is True

    def test_each_token_unique(self) -> None:
        """Two access tokens have different token values."""
        client = OAuthClient.objects.create(name="c")
        t1 = OAuthAccessToken.objects.create(client=client)
        t2 = OAuthAccessToken.objects.create(client=client)
        assert t1.token != t2.token

    def test_str_contains_client_name(self) -> None:
        """__str__ includes the owning client's name."""
        client = OAuthClient.objects.create(name="gpt-agent")
        token = OAuthAccessToken.objects.create(client=client)
        assert "gpt-agent" in str(token)


# ---------------------------------------------------------------------------
# OAuthTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOAuthTokenAuthentication:
    """Tests for the OAuthTokenAuthentication DRF class."""

    @staticmethod
    def _auth() -> OAuthTokenAuthentication:
        """Return a fresh auth instance."""
        return OAuthTokenAuthentication()

    @staticmethod
    def _fake_request(meta: dict[str, str]) -> Any:
        """Return a minimal request-like object with the given META."""

        class _Req:
            META = meta

        return _Req()

    def test_no_header_returns_none(self) -> None:
        """No Authorization header → None (try next authenticator)."""
        req = self._fake_request({})
        assert self._auth().authenticate(req) is None

    def test_wrong_prefix_returns_none(self) -> None:
        """Authorization: Token <x> (not Bearer) → None."""
        req = self._fake_request({"HTTP_AUTHORIZATION": "Token abc123"})
        assert self._auth().authenticate(req) is None

    def test_valid_token_returns_service_principal(self) -> None:
        """Valid unexpired token returns (OAuthServicePrincipal, access_token)."""
        client = OAuthClient.objects.create(name="agent")
        token = OAuthAccessToken.objects.create(client=client)

        req = self._fake_request(_bearer(token.token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, auth_token = result
        assert isinstance(auth_user, OAuthServicePrincipal)
        assert auth_user.is_authenticated is True
        assert auth_token.pk == token.pk

    def test_invalid_token_raises_auth_failed(self) -> None:
        """Unrecognised token string raises AuthenticationFailed."""
        req = self._fake_request(_bearer("notarealtoken"))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_expired_token_raises_auth_failed(self) -> None:
        """Expired token raises AuthenticationFailed."""
        client = OAuthClient.objects.create(name="agent")
        past = timezone.now() - timedelta(seconds=1)
        token = OAuthAccessToken.objects.create(client=client, expires_at=past)

        req = self._fake_request(_bearer(token.token))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_inactive_client_raises_auth_failed(self) -> None:
        """Token from an inactive client raises AuthenticationFailed."""
        client = OAuthClient.objects.create(name="disabled", is_active=False)
        token = OAuthAccessToken.objects.create(client=client)

        req = self._fake_request(_bearer(token.token))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_authenticate_header_returns_bearer(self, rf: RequestFactory) -> None:
        """authenticate_header() returns a Bearer realm with resource_metadata."""
        header = self._auth().authenticate_header(rf.get("/"))
        assert header.startswith("Bearer")
        assert "resource_metadata" in header
        assert ".well-known/oauth-protected-resource" in header

    def test_last_used_at_stamped_on_success(self) -> None:
        """last_used_at is set after a successful authentication."""
        client = OAuthClient.objects.create(name="tracked")
        token = OAuthAccessToken.objects.create(client=client)
        assert token.last_used_at is None

        req = self._fake_request(_bearer(token.token))
        self._auth().authenticate(req)

        token.refresh_from_db()
        assert token.last_used_at is not None


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenView:
    """Tests for the POST /oauth/token/ endpoint."""

    def test_valid_credentials_return_token(self, rf: RequestFactory) -> None:
        """Valid client_id + client_secret returns an access token."""
        client = OAuthClient.objects.create(name="agent")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
            },
        )
        response = _token_view(request)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["token_type"] == "Bearer"
        assert "access_token" in data
        assert data["expires_in"] == 3600
        assert data["scope"] == "mcp"

    def test_access_token_persisted_in_db(self, rf: RequestFactory) -> None:
        """Token returned from the endpoint is saved to the database."""
        client = OAuthClient.objects.create(name="agent")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
            },
        )
        response = _token_view(request)
        data = json.loads(response.content)
        assert OAuthAccessToken.objects.filter(token=data["access_token"]).exists()

    def test_wrong_grant_type_returns_400(self, rf: RequestFactory) -> None:
        """Unsupported grant_type returns 400 with error code."""
        request = _post_token(rf, {"grant_type": "authorization_code"})
        response = _token_view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "unsupported_grant_type"

    def test_missing_grant_type_returns_400(self, rf: RequestFactory) -> None:
        """Missing grant_type returns 400."""
        request = _post_token(rf, {"client_id": "x", "client_secret": "y"})
        response = _token_view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "unsupported_grant_type"

    def test_missing_client_credentials_returns_400(self, rf: RequestFactory) -> None:
        """Missing client_id or client_secret returns 400."""
        request = _post_token(rf, {"grant_type": "client_credentials"})
        response = _token_view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "invalid_request"

    def test_invalid_client_credentials_returns_401(self, rf: RequestFactory) -> None:
        """Wrong client_secret returns 401 with invalid_client error."""
        client = OAuthClient.objects.create(name="agent")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": "wrongsecret",
            },
        )
        response = _token_view(request)
        assert response.status_code == 401
        data = json.loads(response.content)
        assert data["error"] == "invalid_client"

    def test_inactive_client_returns_401(self, rf: RequestFactory) -> None:
        """Inactive client returns 401 with invalid_client error."""
        client = OAuthClient.objects.create(name="disabled", is_active=False)
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
            },
        )
        response = _token_view(request)
        assert response.status_code == 401

    def test_json_body_also_accepted(self, rf: RequestFactory) -> None:
        """JSON-encoded request body is also accepted (not just form-encoded)."""
        client = OAuthClient.objects.create(name="json-agent")
        request = rf.post(
            "/oauth/token/",
            data=json.dumps(
                {
                    "grant_type": "client_credentials",
                    "client_id": client.client_id,
                    "client_secret": client.plaintext_client_secret,
                }
            ),
            content_type="application/json",
        )
        response = _token_view(request)
        assert response.status_code == 200

    def test_unknown_client_id_returns_401(self, rf: RequestFactory) -> None:
        """Non-existent client_id returns 401 with invalid_client error."""
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": "doesnotexist00000000000000000000",
                "client_secret": "doesnotmatter",
            },
        )
        response = _token_view(request)
        assert response.status_code == 401
        data = json.loads(response.content)
        assert data["error"] == "invalid_client"

    def test_token_scope_matches_client_scope(self, rf: RequestFactory) -> None:
        """Access token scope reflects the client's configured scope."""
        client = OAuthClient.objects.create(name="scoped", scope="mcp read")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
            },
        )
        response = _token_view(request)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["scope"] == "mcp read"

    def test_custom_expiry_seconds_reflected_in_response(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS is reflected in the expires_in field."""
        settings.FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 7200
        client = OAuthClient.objects.create(name="agent")
        request = _post_token(
            rf,
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.plaintext_client_secret,
            },
        )
        response = _token_view(request)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["expires_in"] == 7200

    def test_get_returns_405(self, rf: RequestFactory) -> None:
        """GET request to token endpoint returns 405."""
        request = rf.get("/oauth/token/")
        response = _token_view(request)
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# Registration endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistrationView:
    """Tests for the POST /oauth/register/ endpoint."""

    def test_disabled_by_default_returns_403(self, rf: RequestFactory) -> None:
        """Registration endpoint returns 403 when FRIESE_MCP_OAUTH_REGISTRATION_OPEN is False."""
        request = _post_register(rf, {"client_name": "new-agent"})
        response = _register_view(request)
        assert response.status_code == 403
        data = json.loads(response.content)
        assert data["error"] == "registration_not_supported"

    def test_valid_registration_creates_client(self, rf: RequestFactory, settings: Any) -> None:
        """Valid registration creates an OAuthClient and returns credentials."""
        settings.FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"client_name": "new-agent"})
        response = _register_view(request)
        assert response.status_code == 201
        data = json.loads(response.content)
        assert "client_id" in data
        assert "client_secret" in data
        assert data["client_name"] == "new-agent"
        assert OAuthClient.objects.filter(client_id=data["client_id"]).exists()

    def test_registration_with_scope(self, rf: RequestFactory, settings: Any) -> None:
        """Scope in registration body is stored on the created client."""
        settings.FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"client_name": "scoped-agent", "scope": "mcp read"})
        response = _register_view(request)
        assert response.status_code == 201
        data = json.loads(response.content)
        client = OAuthClient.objects.get(client_id=data["client_id"])
        assert client.scope == "mcp read"

    def test_missing_client_name_returns_400(self, rf: RequestFactory, settings: Any) -> None:
        """Missing client_name returns 400 with invalid_client_metadata error."""
        settings.FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"scope": "mcp"})
        response = _register_view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "invalid_client_metadata"

    def test_invalid_json_returns_400(self, rf: RequestFactory, settings: Any) -> None:
        """Malformed JSON body returns 400."""
        settings.FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True
        request = rf.post(
            "/oauth/register/",
            data="not-json",
            content_type="application/json",
        )
        response = _register_view(request)
        assert response.status_code == 400

    def test_get_returns_405(self, rf: RequestFactory) -> None:
        """GET request to register endpoint returns 405."""
        request = rf.get("/oauth/register/")
        response = _register_view(request)
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# Well-known metadata endpoints
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWellKnownEndpoints:
    """Tests for the /.well-known/ discovery endpoints."""

    def test_authorization_server_returns_200(self, rf: RequestFactory) -> None:
        """/.well-known/oauth-authorization-server returns 200 JSON."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        assert response.status_code == 200

    def test_authorization_server_metadata_structure(self, rf: RequestFactory) -> None:
        """Authorization server metadata contains required RFC 8414 fields."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "issuer" in data
        assert "token_endpoint" in data
        assert "client_credentials" in data["grant_types_supported"]
        assert "client_secret_post" in data["token_endpoint_auth_methods_supported"]

    def test_authorization_server_no_registration_endpoint_when_closed(
        self, rf: RequestFactory
    ) -> None:
        """registration_endpoint is absent when FRIESE_MCP_OAUTH_REGISTRATION_OPEN is False."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "registration_endpoint" not in data

    def test_authorization_server_includes_registration_when_open(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """registration_endpoint is present when FRIESE_MCP_OAUTH_REGISTRATION_OPEN is True."""
        settings.FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "registration_endpoint" in data

    def test_protected_resource_returns_200(self, rf: RequestFactory) -> None:
        """/.well-known/oauth-protected-resource returns 200 JSON."""
        request = rf.get("/.well-known/oauth-protected-resource")
        response = _protected_resource_view(request)
        assert response.status_code == 200

    def test_protected_resource_metadata_structure(self, rf: RequestFactory) -> None:
        """Protected resource metadata contains required fields."""
        request = rf.get("/.well-known/oauth-protected-resource")
        response = _protected_resource_view(request)
        data = json.loads(response.content)
        assert "resource" in data
        assert "authorization_servers" in data
        assert "bearer_methods_supported" in data
        assert "header" in data["bearer_methods_supported"]
        assert "mcp" in data["scopes_supported"]


# ---------------------------------------------------------------------------
# Integration: McpEndpointView + OAuthTokenAuthentication + IsAuthenticated
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMcpEndpointOAuthIntegration:
    """Integration tests: McpEndpointView + OAuthTokenAuthentication + IsAuthenticated."""

    def _configure_auth(self, settings: Any) -> None:
        """Point the MCP gateway at OAuthTokenAuthentication + IsAuthenticated."""
        settings.FRIESE_MCP_AUTHENTICATION_CLASSES = [
            "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication"
        ]
        settings.FRIESE_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]

    def test_no_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with no Authorization header is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload)
            response = _mcp_view(request)

        assert response.status_code == 401

    def test_invalid_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with an invalid token is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer("invalidtoken"))
            response = _mcp_view(request)

        assert response.status_code == 401

    def test_expired_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with an expired token is rejected with 401."""
        self._configure_auth(settings)
        client = OAuthClient.objects.create(name="agent")
        past = timezone.now() - timedelta(seconds=1)
        access_token = OAuthAccessToken.objects.create(client=client, expires_at=past)

        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(access_token.token))
            response = _mcp_view(request)

        assert response.status_code == 401

    def test_valid_token_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """Request with a valid unexpired token from an active client succeeds."""
        self._configure_auth(settings)
        client = OAuthClient.objects.create(name="agent")
        access_token = OAuthAccessToken.objects.create(client=client)

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(access_token.token))
            response = _mcp_view(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["result"] == {}


# ---------------------------------------------------------------------------
# _get_base_url — reverse proxy support
# ---------------------------------------------------------------------------


class TestGetBaseUrl:
    """Tests for _get_base_url with and without reverse-proxy headers."""

    def test_issuer_setting_takes_priority(self, rf: RequestFactory, settings: Any) -> None:
        """FRIESE_MCP_OAUTH_ISSUER overrides everything else."""
        settings.FRIESE_MCP_OAUTH_ISSUER = "https://api.example.com"
        request = rf.get("/", HTTP_X_FORWARDED_PROTO="http", HTTP_HOST="internal:8000")
        assert _get_base_url(request) == "https://api.example.com"

    def test_issuer_setting_trailing_slash_stripped(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Trailing slash is stripped from FRIESE_MCP_OAUTH_ISSUER."""
        settings.FRIESE_MCP_OAUTH_ISSUER = "https://api.example.com/"
        request = rf.get("/")
        assert _get_base_url(request) == "https://api.example.com"

    def test_no_proxy_uses_build_absolute_uri(self, rf: RequestFactory, settings: Any) -> None:
        """Without ISSUER or proxy count, build_absolute_uri is used."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 0
        request = rf.get("/")  # default SERVER_NAME = testserver
        result = _get_base_url(request)
        assert "testserver" in result

    def test_proxy_count_uses_xff_proto(self, rf: RequestFactory, settings: Any) -> None:
        """With proxy_count>0, X-Forwarded-Proto determines the scheme."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        result = _get_base_url(request)
        assert result == "https://api.example.com"

    def test_proxy_count_uses_xff_host(self, rf: RequestFactory, settings: Any) -> None:
        """With proxy_count>0, X-Forwarded-Host overrides the Host header."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="public.example.com",
            SERVER_NAME="internal-host",
        )
        result = _get_base_url(request)
        assert "public.example.com" in result
        assert "internal-host" not in result

    def test_proxy_xff_proto_first_value_used_when_multiple(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """The first value of X-Forwarded-Proto is used when multiple are present."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https, http",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        result = _get_base_url(request)
        assert result.startswith("https://")

    def test_proxy_xff_host_first_value_used_when_multiple(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """The first value of X-Forwarded-Host is used when multiple are present."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="api.example.com, proxy.internal",
        )
        result = _get_base_url(request)
        assert "api.example.com" in result
        assert "proxy.internal" not in result

    def test_proxy_no_xff_host_falls_back_to_request_get_host(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Falls back to request.get_host() when X-Forwarded-Host is absent."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        settings.ALLOWED_HOSTS = ["testserver"]
        # No HTTP_X_FORWARDED_HOST — falls back to request.get_host() which reads Host header.
        # RequestFactory defaults SERVER_NAME to "testserver".
        request = rf.get("/", HTTP_X_FORWARDED_PROTO="https")
        result = _get_base_url(request)
        assert "testserver" in result
        assert result.startswith("https://")

    def test_well_known_issuer_reflects_proxy_url(self, rf: RequestFactory, settings: Any) -> None:
        """Authorization server metadata uses the proxy-resolved base URL as issuer."""
        settings.FRIESE_MCP_OAUTH_ISSUER = ""
        settings.FRIESE_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/.well-known/oauth-authorization-server",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert data["issuer"] == "https://api.example.com"
        assert data["token_endpoint"].startswith("https://api.example.com")
