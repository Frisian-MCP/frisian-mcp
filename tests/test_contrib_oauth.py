"""Tests for frisian_mcp.contrib.oauth — OAuth 2.0 client_credentials flow."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from frisian_mcp.contrib.oauth.authentication import OAuthServicePrincipal, OAuthTokenAuthentication
from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient
from frisian_mcp.contrib.oauth.views import (
    OAuthAuthorizationServerView,
    OAuthProtectedResourceView,
    RegistrationView,
    TokenView,
    _get_base_url,
)
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_mcp_view = McpView.as_view()
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

    def test_default_permission_is_read_write(self) -> None:
        """Default permission tier is read_write."""
        client = OAuthClient.objects.create(name="default-perm")
        assert client.permission == "read_write"

    def test_scope_string_maps_permission(self) -> None:
        """scope_string property returns cumulative scopes for each permission level."""
        client = OAuthClient.objects.create(name="read-client", permission="read")
        assert client.scope_string == "mcp:read"
        client.permission = "read_write"
        assert client.scope_string == "mcp:read mcp:write"
        client.permission = "admin"
        assert client.scope_string == "mcp:read mcp:write mcp:admin"

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
        assert len(token.token) == 64  # HMAC-SHA256 hex digest

    def test_stored_token_is_hmac_not_plaintext(self) -> None:
        """SEC-1: token column holds the HMAC digest, not the raw value."""
        from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            _hmac_secret,
        )

        client = OAuthClient.objects.create(name="c")
        token = OAuthAccessToken.objects.create(client=client)
        # plaintext_token is exposed once on the freshly-saved instance.
        raw = token.plaintext_token
        assert raw and len(raw) == 64
        # token column stores HMAC(raw), not raw.
        assert token.token == _hmac_secret(raw)
        assert token.token != raw

    def test_plaintext_token_not_persisted(self) -> None:
        """SEC-1: plaintext_token is a transient attribute, not in the DB."""
        client = OAuthClient.objects.create(name="c")
        created = OAuthAccessToken.objects.create(client=client)
        raw = created.plaintext_token

        # Re-fetch from the DB — plaintext_token should not be present.
        refetched = OAuthAccessToken.objects.get(pk=created.pk)
        assert (
            not hasattr(refetched, "plaintext_token")
            or getattr(refetched, "plaintext_token", None) is None
        )
        # The stored token still matches the original raw value via HMAC.
        from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            _hmac_secret,
        )

        assert refetched.token == _hmac_secret(raw)

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

        # SEC-1: send the raw token (plaintext_token); the gateway hashes it.
        req = self._fake_request(_bearer(token.plaintext_token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, auth_token = result
        assert isinstance(auth_user, OAuthServicePrincipal)
        assert auth_user.is_authenticated is True
        assert auth_token.pk == token.pk

    def test_service_principal_permission_tiers(self) -> None:
        """OAuthServicePrincipal maps permission tier to Django permission interface."""
        admin = OAuthServicePrincipal(permission="admin")
        assert admin.is_superuser is False
        assert admin.is_staff is True
        assert admin.has_perm("svc.add_item") is True
        assert admin.has_module_perms("svc") is True

        rw = OAuthServicePrincipal(permission="read_write")
        assert rw.is_superuser is False
        assert rw.is_staff is True
        assert rw.has_perm("svc.add_item") is True

        read = OAuthServicePrincipal(permission="read")
        assert read.is_superuser is False
        assert read.is_staff is False
        assert read.has_perm("svc.add_item") is False
        assert read.has_module_perms("svc") is False

    def test_principal_carries_client_permission(self) -> None:
        """Authenticate reads permission from the client rather than the token snapshot."""
        client = OAuthClient.objects.create(name="admin-agent", permission="admin")
        token = OAuthAccessToken.objects.create(client=client, permission="admin")
        req = self._fake_request(_bearer(token.plaintext_token))
        auth_user, _ = self._auth().authenticate(req)
        assert isinstance(auth_user, OAuthServicePrincipal)
        assert auth_user.permission == "admin"
        assert auth_user.is_superuser is False

    def test_invalid_token_returns_none(self) -> None:
        """Unrecognised token string falls through to the next authenticator."""
        req = self._fake_request(_bearer("notarealtoken"))
        assert self._auth().authenticate(req) is None

    def test_expired_token_raises_auth_failed(self) -> None:
        """Expired token raises AuthenticationFailed."""
        client = OAuthClient.objects.create(name="agent")
        past = timezone.now() - timedelta(seconds=1)
        token = OAuthAccessToken.objects.create(client=client, expires_at=past)

        req = self._fake_request(_bearer(token.plaintext_token))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_inactive_client_raises_auth_failed(self) -> None:
        """Token from an inactive client raises AuthenticationFailed."""
        client = OAuthClient.objects.create(name="disabled", is_active=False)
        token = OAuthAccessToken.objects.create(client=client)

        req = self._fake_request(_bearer(token.plaintext_token))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_authenticate_header_returns_bearer(self, rf: RequestFactory) -> None:
        """authenticate_header() returns a Bearer realm with resource_metadata."""
        header = self._auth().authenticate_header(rf.get("/"))
        assert header.startswith("Bearer")
        assert "resource_metadata" in header
        assert ".well-known/oauth-protected-resource" in header

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
    def test_authenticate_header_omits_resource_metadata_when_discovery_disabled(
        self,
        rf: RequestFactory,
    ) -> None:
        """authenticate_header() should not advertise resource metadata when discovery is disabled."""
        header = self._auth().authenticate_header(rf.get("/"))

        assert header == 'Bearer realm="frisian-mcp"'

    def test_last_used_at_stamped_on_success(self) -> None:
        """last_used_at is set after a successful authentication."""
        client = OAuthClient.objects.create(name="tracked")
        token = OAuthAccessToken.objects.create(client=client)
        assert token.last_used_at is None

        req = self._fake_request(_bearer(token.plaintext_token))
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
        assert data["scope"] == "mcp:read mcp:write"

    def test_access_token_persisted_in_db(self, rf: RequestFactory) -> None:
        """Token returned from the endpoint is saved to the database (as HMAC digest)."""
        from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            _hmac_secret,
        )

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
        # SEC-1: the response carries the raw token; the DB row stores HMAC(raw).
        assert OAuthAccessToken.objects.filter(token=_hmac_secret(data["access_token"])).exists()

    def test_wrong_grant_type_returns_400(self, rf: RequestFactory) -> None:
        """Unsupported grant_type returns 400 with error code."""
        request = _post_token(rf, {"grant_type": "implicit"})
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

    def test_token_scope_maps_from_client_permission(self, rf: RequestFactory) -> None:
        """Access token scope string reflects the client's permission tier."""
        client = OAuthClient.objects.create(name="read-client", permission="read")
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
        assert data["scope"] == "mcp:read"

    def test_custom_expiry_seconds_reflected_in_response(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS is reflected in the expires_in field."""
        settings.FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 7200
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
# Token endpoint rate limiting (SEC-6)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenViewRateLimit:
    """FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT throttles the token endpoint per IP."""

    def setup_method(self) -> None:
        """Clear the cache so rate limit counters don't bleed between tests."""
        from django.core.cache import cache  # pylint: disable=import-outside-toplevel

        cache.clear()

    def test_no_rate_limit_by_default(self, rf: RequestFactory) -> None:
        """Without the setting, repeated requests are never rate-limited."""
        client = OAuthClient.objects.create(name="rl-agent")
        for _ in range(5):
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

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="2/minute")
    def test_rate_limit_blocks_after_max_requests(self, rf: RequestFactory) -> None:
        """Requests beyond the configured limit receive HTTP 429."""
        client = OAuthClient.objects.create(name="rl-agent2")
        payload = {
            "grant_type": "client_credentials",
            "client_id": client.client_id,
            "client_secret": client.plaintext_client_secret,
        }
        # First two requests should succeed
        for _ in range(2):
            request = _post_token(rf, payload)
            request.META["REMOTE_ADDR"] = "10.0.0.1"
            response = _token_view(request)
            assert response.status_code == 200
        # Third request from same IP must be rate-limited
        request = _post_token(rf, payload)
        request.META["REMOTE_ADDR"] = "10.0.0.1"
        response = _token_view(request)
        assert response.status_code == 429
        data = json.loads(response.content)
        assert data["error"] == "rate_limit_exceeded"

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="1/minute")
    def test_different_ips_have_independent_counters(self, rf: RequestFactory) -> None:
        """Each client IP has its own rate limit counter."""
        client = OAuthClient.objects.create(name="rl-agent3")
        payload = {
            "grant_type": "client_credentials",
            "client_id": client.client_id,
            "client_secret": client.plaintext_client_secret,
        }
        # Exhaust the limit for IP A
        for ip in ("10.0.0.2", "10.0.0.3"):
            request = _post_token(rf, payload)
            request.META["REMOTE_ADDR"] = ip
            response = _token_view(request)
            assert response.status_code == 200

        # IP A is now blocked but IP B still has capacity (fresh counter)
        request_a = _post_token(rf, payload)
        request_a.META["REMOTE_ADDR"] = "10.0.0.2"
        assert _token_view(request_a).status_code == 429

        request_b = _post_token(rf, payload)
        request_b.META["REMOTE_ADDR"] = "10.0.0.3"
        assert _token_view(request_b).status_code == 429

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="bad-value")
    def test_misconfigured_rate_limit_fails_open(self, rf: RequestFactory) -> None:
        """A malformed rate limit string never blocks requests (fail-open)."""
        client = OAuthClient.objects.create(name="rl-agent4")
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


# ---------------------------------------------------------------------------
# Registration endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistrationView:
    """Tests for the POST /oauth/register/ endpoint."""

    def test_disabled_by_default_returns_403(self, rf: RequestFactory) -> None:
        """Registration endpoint returns 403 when FRISIAN_MCP_OAUTH_REGISTRATION_OPEN is False."""
        request = _post_register(rf, {"client_name": "new-agent"})
        response = _register_view(request)
        assert response.status_code == 403
        data = json.loads(response.content)
        assert data["error"] == "registration_not_supported"

    def test_valid_registration_creates_client(self, rf: RequestFactory, settings: Any) -> None:
        """Valid registration creates an OAuthClient and returns credentials."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"client_name": "new-agent"})
        response = _register_view(request)
        assert response.status_code == 201
        data = json.loads(response.content)
        assert "client_id" in data
        assert "client_secret" in data
        assert data["client_name"] == "new-agent"
        assert OAuthClient.objects.filter(client_id=data["client_id"]).exists()

    def test_registration_scope_string_in_response(self, rf: RequestFactory, settings: Any) -> None:
        """DCR clients without redirect_uris get the PKCE default permission tier (read)."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"client_name": "new-client"})
        response = _register_view(request)
        assert response.status_code == 201
        data = json.loads(response.content)
        assert data["scope"] == "mcp:read"  # default permission=read (PKCE default)

    def test_missing_client_name_returns_400(self, rf: RequestFactory, settings: Any) -> None:
        """Missing client_name returns 400 with invalid_client_metadata error."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        request = _post_register(rf, {"other_field": "mcp"})
        response = _register_view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "invalid_client_metadata"

    def test_invalid_json_returns_400(self, rf: RequestFactory, settings: Any) -> None:
        """Malformed JSON body returns 400."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
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
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """registration_endpoint is absent when both DCR and REGISTRATION_OPEN are disabled."""
        settings.FRISIAN_MCP_OAUTH_DCR = False
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "registration_endpoint" not in data

    def test_authorization_server_includes_registration_when_open(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """registration_endpoint is present when FRISIAN_MCP_OAUTH_REGISTRATION_OPEN is True."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
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
        assert "mcp:read" in data["scopes_supported"]
        assert "mcp:write" in data["scopes_supported"]

    def test_authorization_server_returns_json_404_when_discovery_off(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """PUBLIC_DISCOVERY=False hides the authorization-server metadata behind a JSON 404."""
        settings.FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        assert response.status_code == 404
        # Parseable JSON, not a host HTML 404 page — discovery clients must
        # fall back cleanly to their configured static Bearer.
        body = json.loads(response.content)
        assert body == {"error": "not_found"}

    def test_protected_resource_returns_json_404_when_discovery_off(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """PUBLIC_DISCOVERY=False hides the protected-resource metadata behind a JSON 404."""
        settings.FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False
        request = rf.get("/.well-known/oauth-protected-resource")
        response = _protected_resource_view(request)
        assert response.status_code == 404
        body = json.loads(response.content)
        assert body == {"error": "not_found"}

    def test_public_discovery_default_is_true(self, rf: RequestFactory) -> None:
        """With no setting, well-known continues to serve metadata (back-compat default)."""
        # No FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY in settings → getattr default True.
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        assert response.status_code == 200

    def test_authorization_server_path_scoped_variant_returns_metadata(
        self, rf: RequestFactory
    ) -> None:
        """
        Path-scoped variant returns the same metadata as the bare URL.

        Regression: a path capture of ``<path:resource>`` previously raised
        ``TypeError: OAuthAuthorizationServerView.get() got an unexpected
        keyword argument 'resource'`` because ``get()`` did not accept
        ``**kwargs``.  Discovery-first MCP clients that probe the path-scoped
        variant must receive a parseable response, not a 500.
        """
        request = rf.get("/.well-known/oauth-authorization-server/breakingprod")
        response = _auth_server_view(request, resource="breakingprod")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert "issuer" in data
        assert "token_endpoint" in data

    def test_authorization_server_path_scoped_variant_honours_discovery_off(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Path-scoped variant also JSON-404s when PUBLIC_DISCOVERY=False."""
        settings.FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False
        request = rf.get("/.well-known/oauth-authorization-server/breakingprod")
        response = _auth_server_view(request, resource="breakingprod")
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not_found"}


# ---------------------------------------------------------------------------
# Discovery-cascade fallback stubs (openid-configuration, bare /register)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDiscoveryFallbackStubs:
    """JSON-404 stubs that keep the discovery cascade parseable end-to-end."""

    def test_openid_configuration_returns_json_404(self, rf: RequestFactory) -> None:
        """``/.well-known/openid-configuration`` always returns a JSON 404."""
        from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
            OpenIDConfigurationView,
        )

        view = OpenIDConfigurationView.as_view()
        response = view(rf.get("/.well-known/openid-configuration"))
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not_found"}

    def test_openid_configuration_path_scoped_variant(self, rf: RequestFactory) -> None:
        """Path-scoped openid-configuration variant also JSON-404s."""
        from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
            OpenIDConfigurationView,
        )

        view = OpenIDConfigurationView.as_view()
        response = view(
            rf.get("/.well-known/openid-configuration/breakingprod"),
            resource="breakingprod",
        )
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not_found"}

    def test_bare_register_get_returns_json_404(self, rf: RequestFactory) -> None:
        """``GET /register`` returns a JSON 404 (canonical endpoint is /oauth/register/)."""
        from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
            BareRegisterView,
        )

        view = BareRegisterView.as_view()
        response = view(rf.get("/register"))
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not_found"}

    def test_bare_register_post_returns_json_404(self, rf: RequestFactory) -> None:
        """``POST /register`` (RFC 7591 default) returns a JSON 404, not host HTML."""
        from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
            BareRegisterView,
        )

        view = BareRegisterView.as_view()
        response = view(rf.post("/register", data="{}", content_type="application/json"))
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not_found"}


# ---------------------------------------------------------------------------
# Integration: McpView + OAuthTokenAuthentication + IsAuthenticated
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMcpViewOAuthIntegration:
    """Integration tests: McpView + OAuthTokenAuthentication + IsAuthenticated."""

    def _configure_auth(self, settings: Any) -> None:
        """Point the MCP gateway at OAuthTokenAuthentication + IsAuthenticated."""
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication"
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]

    def test_no_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with no Authorization header is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload)
            response = _mcp_view(request)

        assert response.status_code == 401

    def test_invalid_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with an invalid token is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
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

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(access_token.plaintext_token))
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

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(access_token.plaintext_token))
            response = _mcp_view(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["result"] == {}

    def test_db_token_authenticates_when_chained_behind_oauth(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        Static FrisianMcpToken Bearer authenticates with OAuth FIRST and FrisianMcpToken SECOND.

        Mirror of the FrisianMcpToken-first regression test.  NetBox configures
        ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` with OAuth first so that
        OAuth-issued access tokens hit their validator before the DB-token
        authenticator.  For that order to be safe, ``OAuthTokenAuthentication``
        must return ``None`` on lookup-miss instead of raising
        ``AuthenticationFailed`` — otherwise a static MCP token Bearer dead-ends
        at the OAuth class before reaching ``FrisianMcpTokenAuthentication``.
        """
        # pylint: disable=import-outside-toplevel
        from django.contrib.auth import get_user_model

        from frisian_mcp.contrib.tokens.models import FrisianMcpToken

        user_model = get_user_model()
        user = user_model.objects.create_user(username="db-token-principal", password="pw")
        db_token = FrisianMcpToken.objects.create(name="netbox-agent", user=user)

        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(db_token.plaintext_token))
            response = _mcp_view(request)

        assert response.status_code == 200, response.content


# ---------------------------------------------------------------------------
# _get_base_url — reverse proxy support
# ---------------------------------------------------------------------------


class TestGetBaseUrl:
    """Tests for _get_base_url with and without reverse-proxy headers."""

    def test_issuer_setting_takes_priority(self, rf: RequestFactory, settings: Any) -> None:
        """FRISIAN_MCP_OAUTH_ISSUER overrides everything else."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = "https://api.example.com"
        request = rf.get("/", HTTP_X_FORWARDED_PROTO="http", HTTP_HOST="internal:8000")
        assert _get_base_url(request) == "https://api.example.com"

    def test_issuer_setting_trailing_slash_stripped(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Trailing slash is stripped from FRISIAN_MCP_OAUTH_ISSUER."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = "https://api.example.com/"
        request = rf.get("/")
        assert _get_base_url(request) == "https://api.example.com"

    def test_no_proxy_uses_build_absolute_uri(self, rf: RequestFactory, settings: Any) -> None:
        """Without ISSUER or proxy count, build_absolute_uri is used."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 0
        request = rf.get("/")  # default SERVER_NAME = testserver
        result = _get_base_url(request)
        assert "testserver" in result

    def test_proxy_count_uses_xff_proto(self, rf: RequestFactory, settings: Any) -> None:
        """With proxy_count>0, X-Forwarded-Proto determines the scheme."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        result = _get_base_url(request)
        assert result == "https://api.example.com"

    def test_proxy_count_uses_xff_host(self, rf: RequestFactory, settings: Any) -> None:
        """With proxy_count>0, X-Forwarded-Host overrides the Host header."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="public.example.com",
            SERVER_NAME="internal-host",
        )
        result = _get_base_url(request)
        assert "public.example.com" in result
        assert "internal-host" not in result

    def test_proxy_xff_proto_last_value_used_when_multiple(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """The last value of X-Forwarded-Proto is used — rightmost is set by the nearest proxy."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="http, https",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        result = _get_base_url(request)
        assert result.startswith("https://")

    def test_proxy_xff_host_last_value_used_when_multiple(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """The last value of X-Forwarded-Host is used — rightmost is set by the nearest proxy."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="proxy.internal, api.example.com",
        )
        result = _get_base_url(request)
        assert "api.example.com" in result
        assert "proxy.internal" not in result

    def test_proxy_no_xff_host_falls_back_to_request_get_host(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Falls back to request.get_host() when X-Forwarded-Host is absent."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        settings.ALLOWED_HOSTS = ["testserver"]
        # No HTTP_X_FORWARDED_HOST — falls back to request.get_host() which reads Host header.
        # RequestFactory defaults SERVER_NAME to "testserver".
        request = rf.get("/", HTTP_X_FORWARDED_PROTO="https")
        result = _get_base_url(request)
        assert "testserver" in result
        assert result.startswith("https://")

    def test_well_known_issuer_reflects_proxy_url(self, rf: RequestFactory, settings: Any) -> None:
        """Authorization server metadata uses the proxy-resolved base URL as issuer."""
        settings.FRISIAN_MCP_OAUTH_ISSUER = ""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1
        request = rf.get(
            "/.well-known/oauth-authorization-server",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="api.example.com",
        )
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert data["issuer"] == "https://api.example.com"


# ---------------------------------------------------------------------------
# OAuthConfig.ready() validation
# ---------------------------------------------------------------------------


class TestOAuthConfigReady:
    """Tests for startup validation in OAuthConfig.ready()."""

    def _call_ready(self) -> None:
        from django.apps import apps

        apps.get_app_config("frisian_mcp_oauth").ready()

    def test_valid_zero_proxy_count(self, settings: Any) -> None:
        """proxy_count=0 is valid and does not raise."""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 0
        self._call_ready()  # no exception

    def test_valid_positive_proxy_count(self, settings: Any) -> None:
        """proxy_count=2 is valid and does not raise."""
        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = 2
        self._call_ready()  # no exception

    def test_string_proxy_count_raises(self, settings: Any) -> None:
        """A string value for FRISIAN_MCP_TRUSTED_PROXY_COUNT raises ImproperlyConfigured."""
        from django.core.exceptions import ImproperlyConfigured

        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = "1"
        with pytest.raises(ImproperlyConfigured, match="must be a non-negative integer"):
            self._call_ready()

    def test_bool_proxy_count_raises(self, settings: Any) -> None:
        """A bool value (subclass of int) raises ImproperlyConfigured."""
        from django.core.exceptions import ImproperlyConfigured

        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = True
        with pytest.raises(ImproperlyConfigured, match="must be a non-negative integer"):
            self._call_ready()

    def test_negative_proxy_count_raises(self, settings: Any) -> None:
        """A negative integer raises ImproperlyConfigured."""
        from django.core.exceptions import ImproperlyConfigured

        settings.FRISIAN_MCP_TRUSTED_PROXY_COUNT = -1
        with pytest.raises(ImproperlyConfigured, match="must be >= 0"):
            self._call_ready()

    def test_locmem_cache_in_production_logs_warning(self, settings: Any, caplog: Any) -> None:
        """LocMemCache + DEBUG=False emits a startup warning about multi-worker risk."""
        import logging  # pylint: disable=import-outside-toplevel

        settings.DEBUG = False
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        }
        with caplog.at_level(logging.WARNING, logger="frisian_mcp.contrib.oauth.apps"):
            self._call_ready()
        assert any("LocMemCache" in r.message for r in caplog.records)
        assert any("multi-worker" in r.message for r in caplog.records)

    def test_shared_cache_in_production_no_warning(self, settings: Any, caplog: Any) -> None:
        """A Redis cache backend in production does not trigger the LocMemCache warning."""
        import logging  # pylint: disable=import-outside-toplevel

        settings.DEBUG = False
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": "redis://127.0.0.1:6379/1",
            }
        }
        with caplog.at_level(logging.WARNING, logger="frisian_mcp.contrib.oauth.apps"):
            self._call_ready()
        assert not any("LocMemCache" in r.message for r in caplog.records)

    def test_locmem_in_debug_mode_no_warning(self, settings: Any, caplog: Any) -> None:
        """LocMemCache in DEBUG=True does not emit the multi-worker warning."""
        import logging  # pylint: disable=import-outside-toplevel

        settings.DEBUG = True
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        }
        with caplog.at_level(logging.WARNING, logger="frisian_mcp.contrib.oauth.apps"):
            self._call_ready()
        assert not any("LocMemCache" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _verify_pkce helper
# ---------------------------------------------------------------------------


class TestVerifyPkce:
    """Tests for the _verify_pkce(code_verifier, code_challenge) -> bool helper."""

    def test_valid_verifier_returns_true(self) -> None:
        """Correct code_verifier matches its pre-computed S256 challenge."""
        import base64  # pylint: disable=import-outside-toplevel
        import hashlib  # pylint: disable=import-outside-toplevel

        from frisian_mcp.contrib.oauth.views import (
            _verify_pkce,  # pylint: disable=import-outside-toplevel
        )

        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert _verify_pkce(verifier, challenge) is True

    def test_wrong_verifier_returns_false(self) -> None:
        """Wrong code_verifier does not match the challenge."""
        from frisian_mcp.contrib.oauth.views import (
            _verify_pkce,  # pylint: disable=import-outside-toplevel
        )

        assert _verify_pkce("wrong-verifier", "some-challenge") is False

    def test_empty_verifier_returns_false(self) -> None:
        """Empty code_verifier does not match a real challenge."""
        from frisian_mcp.contrib.oauth.views import (
            _verify_pkce,  # pylint: disable=import-outside-toplevel
        )

        assert _verify_pkce("", "some-challenge") is False


# ---------------------------------------------------------------------------
# FRISIAN_MCP_HMAC_KEY switching for OAuth client secrets
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOAuthHmacKeySwitch:
    """FRISIAN_MCP_HMAC_KEY overrides SECRET_KEY for client_secret HMAC digests."""

    def test_custom_hmac_key_produces_different_digest(self, settings: Any) -> None:
        """Clients created with FRISIAN_MCP_HMAC_KEY use a different HMAC than SECRET_KEY."""
        from frisian_mcp.contrib.oauth.models import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            _hmac_secret,
        )

        raw = "supersecretvalue"
        settings.FRISIAN_MCP_HMAC_KEY = ""
        digest_default = _hmac_secret(raw)

        settings.FRISIAN_MCP_HMAC_KEY = "dedicated-oauth-key"
        digest_custom = _hmac_secret(raw)

        assert digest_default != digest_custom

    def test_token_endpoint_uses_hmac_key(self, rf: RequestFactory, settings: Any) -> None:
        """Token endpoint validates client_secret using the current FRISIAN_MCP_HMAC_KEY."""
        settings.FRISIAN_MCP_HMAC_KEY = "my-oauth-hmac-key"
        client = OAuthClient.objects.create(name="hmac-key-client")
        raw_secret = client.plaintext_client_secret

        request = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": raw_secret,
            },
        )
        view = TokenView.as_view()
        response = view(request)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# AuthorizeView — OAuth 2.0 authorization code + PKCE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorizeView:
    """Tests for GET /oauth/authorize/ and POST /oauth/authorize/."""

    from frisian_mcp.contrib.oauth.views import AuthorizeView

    _view = AuthorizeView.as_view()

    def _make_client(self, redirect_uri: str = "https://example.com/cb") -> OAuthClient:
        """
        Register an OAuthClient with *redirect_uri* allowlisted.

        SEC-2: AuthorizeView now enforces that ``client_id`` refers to a real,
        active ``OAuthClient`` whose ``redirect_uris`` list exactly contains
        the request's ``redirect_uri``.  Tests therefore must register a
        client up front rather than passing arbitrary ``client_id`` strings.
        """
        return OAuthClient.objects.create(name="authorize-test", redirect_uris=[redirect_uri])

    def _valid_params(self, client_id: str = "test-client") -> dict[str, str]:
        return {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/cb",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
            "state": "xyz",
        }

    def test_get_auto_approve_redirects_with_code(self, rf: RequestFactory, settings: Any) -> None:
        """GET with valid params + FRISIAN_MCP_OAUTH_AUTO_APPROVE=True redirects with a code."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        # SEC-2: AUTO_APPROVE now defaults to bool(DEBUG); test_settings has
        # no DEBUG so the default is False.  Set the flag explicitly.
        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = True
        client = self._make_client()
        view = AuthorizeView.as_view()
        request = rf.get("/oauth/authorize/", self._valid_params(client.client_id))
        response = view(request)
        assert response.status_code == 302
        location = response["Location"]
        assert location.startswith("https://example.com/cb")
        assert "code=" in location
        assert "state=xyz" in location

    def test_get_auto_approve_false_renders_consent_template(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """GET with AUTO_APPROVE=False renders the consent HTML template."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
        client = self._make_client()

        view = AuthorizeView.as_view()
        request = rf.get("/oauth/authorize/", self._valid_params(client.client_id))
        response = view(request)
        # Template rendered → 200 TemplateResponse (not redirect)
        assert response.status_code == 200

    def test_get_missing_response_type_returns_400_no_redirect_uri(
        self, rf: RequestFactory
    ) -> None:
        """GET with missing response_type and no redirect_uri returns 400 JSON (no redirect)."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        view = AuthorizeView.as_view()
        params = self._valid_params()
        params.pop("response_type")
        params.pop("redirect_uri")
        request = rf.get("/oauth/authorize/", params)
        response = view(request)
        assert response.status_code == 400
        data = json.loads(response.content)
        assert "error" in data

    def test_get_wrong_response_type_redirects_with_error(self, rf: RequestFactory) -> None:
        """GET with response_type=token redirects with error=unsupported_response_type."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        view = AuthorizeView.as_view()
        params = self._valid_params()
        params["response_type"] = "token"
        request = rf.get("/oauth/authorize/", params)
        response = view(request)
        assert response.status_code == 302
        assert "error=unsupported_response_type" in response["Location"]

    def test_get_wrong_code_challenge_method_redirects_error(self, rf: RequestFactory) -> None:
        """GET with code_challenge_method=plain redirects with error=invalid_request."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        view = AuthorizeView.as_view()
        params = self._valid_params()
        params["code_challenge_method"] = "plain"
        request = rf.get("/oauth/authorize/", params)
        response = view(request)
        assert response.status_code == 302
        assert "error=invalid_request" in response["Location"]

    def test_post_allow_redirects_with_code(self, rf: RequestFactory) -> None:
        """POST allow=true issues a code and redirects."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        client = self._make_client()
        view = AuthorizeView.as_view()
        request = rf.post(
            "/oauth/authorize/",
            data={
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "state": "xyz",
                "allow": "true",
            },
        )
        response = view(request)
        assert response.status_code == 302
        assert "code=" in response["Location"]

    def test_post_deny_redirects_with_access_denied(self, rf: RequestFactory) -> None:
        """POST allow=false redirects with error=access_denied."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        client = self._make_client()
        view = AuthorizeView.as_view()
        request = rf.post(
            "/oauth/authorize/",
            data={
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code_challenge": "abc",
                "state": "xyz",
                "allow": "false",
            },
        )
        response = view(request)
        assert response.status_code == 302
        assert "error=access_denied" in response["Location"]


# ---------------------------------------------------------------------------
# SEC-2 — AuthorizeView client + redirect_uri allowlist enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorizeViewSEC2:
    """SEC-2: client_id must be registered, redirect_uri must allowlist-match."""

    @staticmethod
    def _params(client_id: str, redirect_uri: str = "https://example.com/cb") -> dict[str, str]:
        return {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
            "state": "xyz",
        }

    def test_unknown_client_returns_400_invalid_client(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        An unknown client_id is rejected with a JSON 400, NOT a redirect.

        We MUST NOT redirect because we cannot trust an unverified
        redirect_uri to be the legitimate target.
        """
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
        view = AuthorizeView.as_view()
        request = rf.get("/oauth/authorize/", self._params("ghost-id"))
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_client"

    def test_inactive_client_returns_400_invalid_client(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """An inactive client cannot pass authorize even with a registered URI."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
        client = OAuthClient.objects.create(
            name="disabled",
            redirect_uris=["https://example.com/cb"],
            is_active=False,
        )
        view = AuthorizeView.as_view()
        request = rf.get("/oauth/authorize/", self._params(client.client_id))
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_client"

    def test_redirect_uri_not_in_allowlist_returns_400(self, rf: RequestFactory) -> None:
        """An exact-match against client.redirect_uris is required."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        client = OAuthClient.objects.create(
            name="strict",
            redirect_uris=["https://example.com/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="https://evil.example/cb"),
        )
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_redirect_uri"

    def test_http_to_public_host_rejected(self, rf: RequestFactory) -> None:
        """Plain http:// to a non-loopback host is rejected by the scheme gate."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        client = OAuthClient.objects.create(
            name="http-public",
            redirect_uris=["http://example.com/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="http://example.com/cb"),
        )
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_redirect_uri"

    def test_http_loopback_accepted(self, rf: RequestFactory, settings: Any) -> None:
        """http://localhost is allowed (RFC 8252 §7.3 native-app loopback)."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = True
        client = OAuthClient.objects.create(
            name="loopback",
            redirect_uris=["http://localhost:8080/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="http://localhost:8080/cb"),
        )
        response = view(request)
        assert response.status_code == 302
        assert response["Location"].startswith("http://localhost:8080/cb")

    def test_custom_native_scheme_accepted(self, rf: RequestFactory, settings: Any) -> None:
        """Custom native-app scheme (e.g. ``com.example.app:/cb``) is allowed."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = True
        client = OAuthClient.objects.create(
            name="native",
            redirect_uris=["com.example.app:/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="com.example.app:/cb"),
        )
        response = view(request)
        assert response.status_code == 302

    def test_javascript_scheme_rejected(self, rf: RequestFactory) -> None:
        """A javascript: URI is rejected by the scheme gate (no dot in scheme)."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        # Register an unrelated allowlist so the client lookup is irrelevant —
        # the scheme gate runs first.
        client = OAuthClient.objects.create(
            name="js-attempt",
            redirect_uris=["https://example.com/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="javascript:alert('xss')"),
        )
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_redirect_uri"

    def test_data_scheme_rejected(self, rf: RequestFactory) -> None:
        """A data: URI is also rejected by the scheme gate."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        client = OAuthClient.objects.create(
            name="data-attempt",
            redirect_uris=["https://example.com/cb"],
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            self._params(client.client_id, redirect_uri="data:text/html,<script>"),
        )
        response = view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_redirect_uri"

    def test_pkce_auto_register_skips_client_check(self, rf: RequestFactory, settings: Any) -> None:
        """
        When PKCE_AUTO_REGISTER is True, an unknown client_id is allowed through.

        The token exchange step still requires PKCE proof, so the actual
        threat model isn't widened — operators who opt in have already
        accepted that any caller can hold a client_id.
        """
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = True
        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = True
        view = AuthorizeView.as_view()
        request = rf.get("/oauth/authorize/", self._params("on-the-fly-pkce"))
        response = view(request)
        # Bypasses the client lookup but still gets the scheme gate.
        assert response.status_code == 302
        assert "code=" in response["Location"]


# ---------------------------------------------------------------------------
# SEC-2 — auto_approve default tracks DEBUG
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAutoApproveDefault:
    """SEC-2: FRISIAN_MCP_OAUTH_AUTO_APPROVE defaults to bool(DEBUG)."""

    def test_default_is_false_outside_debug(self, rf: RequestFactory, settings: Any) -> None:
        """With DEBUG False (or absent) and AUTO_APPROVE absent → consent template."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.DEBUG = False
        if hasattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE"):
            del settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE
        client = OAuthClient.objects.create(
            name="prod-mode", redirect_uris=["https://example.com/cb"]
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256",
                "state": "xyz",
            },
        )
        response = view(request)
        # Consent template, not a redirect.
        assert response.status_code == 200

    def test_default_is_true_when_debug_true(self, rf: RequestFactory, settings: Any) -> None:
        """With DEBUG=True and AUTO_APPROVE absent → auto-approve redirect."""
        from frisian_mcp.contrib.oauth.views import (
            AuthorizeView,  # pylint: disable=import-outside-toplevel
        )

        settings.DEBUG = True
        if hasattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE"):
            del settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE
        client = OAuthClient.objects.create(
            name="dev-mode", redirect_uris=["https://example.com/cb"]
        )
        view = AuthorizeView.as_view()
        request = rf.get(
            "/oauth/authorize/",
            {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256",
                "state": "xyz",
            },
        )
        response = view(request)
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# SEC-2 — metadata response_types_supported only advertises 'code'
# ---------------------------------------------------------------------------


class TestMetadataResponseTypes:
    """SEC-2: metadata must NOT advertise the unimplemented implicit flow."""

    def test_response_types_supported_is_code_only(self, rf: RequestFactory) -> None:
        """response_types_supported is the single-element list ['code']."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert data["response_types_supported"] == ["code"]
        assert "token" not in data["response_types_supported"]


# ---------------------------------------------------------------------------
# SEC-2 — RegistrationView accepts redirect_uris and validates them
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistrationViewRedirectUris:
    """SEC-2: dynamic registration accepts and validates redirect_uris."""

    @staticmethod
    def _post(rf: RequestFactory, body: dict[str, Any]) -> Any:
        from frisian_mcp.contrib.oauth.views import (
            RegistrationView,  # pylint: disable=import-outside-toplevel
        )

        view = RegistrationView.as_view()
        request = rf.post(
            "/oauth/register/",
            data=json.dumps(body),
            content_type="application/json",
        )
        return view(request)

    def test_registration_persists_redirect_uris(self, rf: RequestFactory, settings: Any) -> None:
        """A registered client persists the supplied redirect_uris list."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        response = self._post(
            rf,
            {
                "client_name": "agent",
                "redirect_uris": ["https://example.com/cb", "http://localhost:9000/cb"],
            },
        )
        assert response.status_code == 201
        data = json.loads(response.content)
        assert data["redirect_uris"] == [
            "https://example.com/cb",
            "http://localhost:9000/cb",
        ]
        # And the persisted row matches.
        client = OAuthClient.objects.get(client_id=data["client_id"])
        assert client.redirect_uris == [
            "https://example.com/cb",
            "http://localhost:9000/cb",
        ]

    def test_registration_rejects_http_to_public_host(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Registration refuses http:// URIs to non-loopback hosts."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        response = self._post(
            rf,
            {
                "client_name": "agent",
                "redirect_uris": ["http://evil.example/cb"],
            },
        )
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "invalid_redirect_uri"

    def test_registration_rejects_non_list_redirect_uris(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """redirect_uris must be a list of strings, not a single string or other shape."""
        settings.FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True
        response = self._post(
            rf,
            {
                "client_name": "agent",
                "redirect_uris": "https://example.com/cb",  # wrong type
            },
        )
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["error"] == "invalid_client_metadata"


# ---------------------------------------------------------------------------
# TokenView — authorization_code grant (PKCE)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenViewAuthorizationCodeGrant:
    """Tests for POST /oauth/token/ with grant_type=authorization_code (PKCE)."""

    def _setup_code_in_cache(
        self,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> str:
        """Store a PKCE auth code in cache and return the code."""
        import base64  # pylint: disable=import-outside-toplevel
        import hashlib  # pylint: disable=import-outside-toplevel
        import secrets  # pylint: disable=import-outside-toplevel

        from django.core.cache import cache  # pylint: disable=import-outside-toplevel

        from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
            _AUTH_CODE_CACHE_PREFIX,
            _AUTH_CODE_TTL,
        )

        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        code = secrets.token_urlsafe(32)
        cache.set(
            f"{_AUTH_CODE_CACHE_PREFIX}{code}",
            {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": challenge},
            _AUTH_CODE_TTL,
        )
        return code

    def test_valid_pkce_exchange_returns_token(self, rf: RequestFactory) -> None:
        """Valid authorization_code + code_verifier returns access token."""
        client = OAuthClient.objects.create(name="pkce-client")
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code = self._setup_code_in_cache(client.client_id, "https://example.com/cb", code_verifier)

        request = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "authorization_code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        response = _token_view(request)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["token_type"] == "Bearer"
        assert "access_token" in data

    def test_wrong_code_verifier_returns_400(self, rf: RequestFactory) -> None:
        """Wrong code_verifier returns 400 invalid_grant."""
        client = OAuthClient.objects.create(name="pkce-wrong")
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code = self._setup_code_in_cache(client.client_id, "https://example.com/cb", code_verifier)

        request = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "authorization_code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code": code,
                "code_verifier": "wrong-verifier",
            },
        )
        response = _token_view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_grant"

    def test_expired_or_missing_code_returns_400(self, rf: RequestFactory) -> None:
        """Non-existent auth code returns 400 invalid_grant."""
        client = OAuthClient.objects.create(name="pkce-expired")
        request = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "authorization_code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code": "no-such-code",
                "code_verifier": "anything",
            },
        )
        response = _token_view(request)
        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "invalid_grant"

    def test_code_is_single_use(self, rf: RequestFactory) -> None:
        """Auth code cannot be reused; second exchange returns 400."""
        client = OAuthClient.objects.create(name="pkce-one-time")
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code = self._setup_code_in_cache(client.client_id, "https://example.com/cb", code_verifier)
        payload = {
            "grant_type": "authorization_code",
            "client_id": client.client_id,
            "redirect_uri": "https://example.com/cb",
            "code": code,
            "code_verifier": code_verifier,
        }
        _token_view(rf.post("/oauth/token/", data=payload))
        response2 = _token_view(rf.post("/oauth/token/", data=payload))
        assert response2.status_code == 400

    def test_access_token_inherits_client_permission(self, rf: RequestFactory) -> None:
        """OAuthAccessToken created via auth_code inherits the client's permission tier."""
        client = OAuthClient.objects.create(name="pkce-perm", permission="admin")
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code = self._setup_code_in_cache(client.client_id, "https://example.com/cb", code_verifier)
        request = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "authorization_code",
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/cb",
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        response = _token_view(request)
        data = json.loads(response.content)
        # SEC-1: data["access_token"] is the raw bearer; storage holds the HMAC
        # digest, so look up by hashing the issued raw token.
        from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            _hmac_secret,
        )

        token = OAuthAccessToken.objects.get(token=_hmac_secret(data["access_token"]))
        assert token.permission == "admin"


# ---------------------------------------------------------------------------
# Well-known: authorization_endpoint and FRISIAN_MCP_OAUTH_AUTHORIZE_URL
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWellKnownAuthorizationEndpoint:
    """Well-known metadata advertises the correct authorization_endpoint."""

    def test_authorization_endpoint_advertised(self, rf: RequestFactory) -> None:
        """Authorization server metadata includes authorization_endpoint."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "authorization_endpoint" in data
        assert data["authorization_endpoint"].endswith("/oauth/authorize/")

    def test_authorization_code_in_grant_types(self, rf: RequestFactory) -> None:
        """authorization_code is listed in grant_types_supported."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "authorization_code" in data["grant_types_supported"]

    def test_s256_in_code_challenge_methods(self, rf: RequestFactory) -> None:
        """S256 is listed in code_challenge_methods_supported."""
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert "S256" in data["code_challenge_methods_supported"]

    def test_custom_authorize_url_override(self, rf: RequestFactory, settings: Any) -> None:
        """FRISIAN_MCP_OAUTH_AUTHORIZE_URL overrides the advertised authorization_endpoint."""
        settings.FRISIAN_MCP_OAUTH_AUTHORIZE_URL = "https://auth.example.com/authorize"
        request = rf.get("/.well-known/oauth-authorization-server")
        response = _auth_server_view(request)
        data = json.loads(response.content)
        assert data["authorization_endpoint"] == "https://auth.example.com/authorize"
