"""Tests for frisian_mcp.contrib.tokens — FrisianMcpToken model + authentication."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.contrib.tokens.authentication import (
    FrisianMcpApiKeyAuthentication,
    FrisianMcpTokenAuthentication,
    _ApiKeyAuth,
)
from frisian_mcp.contrib.tokens.models import FrisianMcpToken, _hmac_token
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

User = get_user_model()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_view = McpView.as_view()


def _bearer(token: str) -> dict[str, str]:
    """Return a META dict with an Authorization: Bearer header."""
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post_mcp(rf: RequestFactory, payload: Any, meta: dict[str, str] | None = None) -> Any:
    """Build a POST request to the MCP endpoint."""
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if meta:
        kwargs.update(meta)
    return rf.post("/mcp/", data=json.dumps(payload), **kwargs)


# ---------------------------------------------------------------------------
# FrisianMcpToken model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFrisianMcpTokenModel:
    """Tests for the FrisianMcpToken model."""

    def test_token_auto_generated_on_save(self) -> None:
        """Token field is populated automatically when a new token is saved."""
        token = FrisianMcpToken.objects.create(name="test-token")
        assert token.token  # stored HMAC
        assert len(token.token) == 64  # HMAC-SHA256 → 64 hex chars
        assert hasattr(token, "plaintext_token")
        assert len(token.plaintext_token) == 64  # raw: secrets.token_hex(32) → 64 hex chars

    def test_token_not_overwritten_on_update(self) -> None:
        """Existing token value is preserved on subsequent saves."""
        token = FrisianMcpToken.objects.create(name="test-token")
        original = token.token
        token.name = "renamed"
        token.save()
        token.refresh_from_db()
        assert token.token == original

    def test_str_active(self) -> None:
        """__str__ includes name and 'active' for an active token."""
        token = FrisianMcpToken(name="my-agent", is_active=True)
        assert "my-agent" in str(token)
        assert "active" in str(token)

    def test_str_inactive(self) -> None:
        """__str__ includes 'inactive' for a deactivated token."""
        token = FrisianMcpToken(name="old-token", is_active=False)
        assert "inactive" in str(token)

    def test_each_token_unique(self) -> None:
        """Two tokens created back-to-back have different stored HMACs and raw values."""
        t1 = FrisianMcpToken.objects.create(name="t1")
        t2 = FrisianMcpToken.objects.create(name="t2")
        assert t1.token != t2.token
        assert t1.plaintext_token != t2.plaintext_token

    def test_service_token_no_user(self) -> None:
        """Tokens can be created without a linked user (service tokens)."""
        token = FrisianMcpToken.objects.create(name="service")
        assert token.user is None
        assert token.user_id is None

    def test_user_linked_token(self) -> None:
        """Tokens can be linked to a Django user."""
        user = User.objects.create_user(username="alice", password="pw")
        token = FrisianMcpToken.objects.create(name="alice-token", user=user)
        token.refresh_from_db()
        assert token.user == user

    def test_stored_token_is_hmac_not_plaintext(self) -> None:
        """The stored token field is not the raw secret — it's the HMAC."""
        token = FrisianMcpToken.objects.create(name="hash-check")
        assert token.token != token.plaintext_token

    def test_plaintext_token_absent_on_fresh_db_fetch(self) -> None:
        """plaintext_token is not present on a freshly fetched instance (creation-time only)."""
        token = FrisianMcpToken.objects.create(name="reload-check")
        fetched = FrisianMcpToken.objects.get(pk=token.pk)
        assert not hasattr(fetched, "plaintext_token")

    def test_auth_rejects_hmac_used_as_bearer(self) -> None:
        """Sending the stored HMAC as the Bearer value does not authenticate."""
        token = FrisianMcpToken.objects.create(name="hmac-bearer-check")

        class _Req:
            META = {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

        # The HMAC value re-hashes to a different digest at lookup time, so
        # the row is never matched.  The class falls through (returns None)
        # rather than raising — see authenticate() for the chain rationale.
        assert FrisianMcpTokenAuthentication().authenticate(_Req()) is None


# ---------------------------------------------------------------------------
# FrisianMcpTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFrisianMcpTokenAuthentication:
    """Tests for the FrisianMcpTokenAuthentication DRF class."""

    @staticmethod
    def _auth() -> FrisianMcpTokenAuthentication:
        """Return a fresh auth instance."""
        return FrisianMcpTokenAuthentication()

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

    def test_valid_user_token_returns_user(self) -> None:
        """Valid token linked to a user returns (user, token)."""
        user = User.objects.create_user(username="bob", password="pw")
        token = FrisianMcpToken.objects.create(name="bob-token", user=user)

        req = self._fake_request(_bearer(token.plaintext_token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, auth_token = result
        assert auth_user == user
        assert auth_token.pk == token.pk

    def test_valid_service_token_returns_anonymous(self) -> None:
        """Valid token with no user returns (AnonymousUser, token)."""
        token = FrisianMcpToken.objects.create(name="svc")
        req = self._fake_request(_bearer(token.plaintext_token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, _ = result
        assert isinstance(auth_user, AnonymousUser)

    def test_invalid_token_returns_none(self) -> None:
        """Unrecognised token string falls through to the next authenticator."""
        req = self._fake_request(_bearer("notarealtoken"))
        assert self._auth().authenticate(req) is None

    def test_inactive_token_returns_none(self) -> None:
        """Inactive token falls through (lookup excludes is_active=False rows)."""
        token = FrisianMcpToken.objects.create(name="disabled", is_active=False)
        req = self._fake_request(_bearer(token.plaintext_token))
        assert self._auth().authenticate(req) is None

    def test_last_used_at_updated_on_auth(self) -> None:
        """last_used_at is set after a successful authentication."""
        token = FrisianMcpToken.objects.create(name="t")
        assert token.last_used_at is None
        req = self._fake_request(_bearer(token.plaintext_token))
        self._auth().authenticate(req)
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_authenticate_header_returns_bearer(self, rf: RequestFactory) -> None:
        """authenticate_header() returns a Bearer realm string with resource_metadata."""
        header = self._auth().authenticate_header(rf.get("/"))
        assert header.startswith("Bearer")
        assert "resource_metadata" in header

        @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
        def test_authenticate_header_omits_resource_metadata_when_discovery_disabled(
            self,
            rf: RequestFactory,
        ) -> None:
            """authenticate_header() should omit OAuth discovery metadata when disabled."""
            header = self._auth().authenticate_header(rf.get("/"))

            assert header == 'Bearer realm="frisian-mcp"'

# ---------------------------------------------------------------------------
# Integration: McpView + FrisianMcpTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMcpViewTokenIntegration:
    """Integration tests: McpView + FrisianMcpTokenAuthentication + IsAuthenticated."""

    def _configure_auth(self, settings: Any) -> None:
        """Point the MCP gateway at FrisianMcpTokenAuthentication + IsAuthenticated."""
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication"
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
            response = _view(request)

        assert response.status_code == 401

    def test_invalid_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with an invalid token is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer("invalidtoken"))
            response = _view(request)

        assert response.status_code == 401

    def test_valid_token_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """Request with a valid active token succeeds."""
        self._configure_auth(settings)
        user = User.objects.create_user(username="carol", password="pw")
        token = FrisianMcpToken.objects.create(name="carol-token", user=user)

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(token.plaintext_token))
            response = _view(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["result"] == {}

    def test_oauth_token_authenticates_when_chained_behind_db_tokens(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        OAuth-issued Bearer authenticates when chained behind FrisianMcpTokenAuthentication.

        Reproduces the symptom where Claude.ai completes the OAuth code flow,
        ``POST /oauth/token/`` returns 200, and the very next ``POST /mcp/``
        carrying the new Bearer returns 401 because the DB-token authenticator
        was raising ``AuthenticationFailed`` on lookup-miss and stopping the
        chain.  Lookup-miss must fall through so the OAuth authenticator can
        validate the token.
        """
        # pylint: disable=import-outside-toplevel
        from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient

        user = User.objects.create_user(username="oauth-principal", password="pw")
        client = OAuthClient.objects.create(name="claude-ai", user=user)
        access_token = OAuthAccessToken.objects.create(client=client)
        raw_bearer = access_token.plaintext_token

        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
            "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(raw_bearer))
            response = _view(request)

        assert response.status_code == 200, response.content


# ---------------------------------------------------------------------------
# FRISIAN_MCP_HMAC_KEY switching
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHmacKeySwitch:
    """FRISIAN_MCP_HMAC_KEY overrides SECRET_KEY for token HMAC digests."""

    def test_custom_hmac_key_produces_different_digest(self, settings: Any) -> None:
        """Tokens created with FRISIAN_MCP_HMAC_KEY differ from those keyed by SECRET_KEY."""
        settings.FRISIAN_MCP_HMAC_KEY = ""
        t1 = FrisianMcpToken.objects.create(name="key-default")
        raw = t1.plaintext_token

        settings.FRISIAN_MCP_HMAC_KEY = "dedicated-hmac-secret"
        t2 = FrisianMcpToken.objects.create(name="key-custom")
        raw2 = t2.plaintext_token

        # Same raw value → different HMAC because the key changed
        from frisian_mcp.contrib.tokens.models import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            _hmac_token,
        )

        settings.FRISIAN_MCP_HMAC_KEY = ""
        digest_default = _hmac_token(raw)
        settings.FRISIAN_MCP_HMAC_KEY = "dedicated-hmac-secret"
        digest_custom = _hmac_token(raw)
        assert digest_default != digest_custom

        # Stored digest for t2 matches the custom-key HMAC
        settings.FRISIAN_MCP_HMAC_KEY = "dedicated-hmac-secret"
        assert _hmac_token(raw2) == t2.token

    def test_auth_uses_hmac_key_at_lookup_time(self, rf: RequestFactory, settings: Any) -> None:
        """Authentication reads the token HMAC using the current FRISIAN_MCP_HMAC_KEY."""
        settings.FRISIAN_MCP_HMAC_KEY = "my-dedicated-key"
        token = FrisianMcpToken.objects.create(name="hmac-auth-test")
        raw = token.plaintext_token

        request = rf.get("/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        auth = FrisianMcpTokenAuthentication()
        result = auth.authenticate(request)
        assert result is not None
        assert result[1] == token


# ---------------------------------------------------------------------------
# _ApiKeyAuth helper
# ---------------------------------------------------------------------------


class TestApiKeyAuthObject:
    """Unit tests for the _ApiKeyAuth lightweight auth object."""

    def test_permission_stored(self) -> None:
        """Permission tier is accessible on the auth object."""
        auth = _ApiKeyAuth(permission="read_write")
        assert auth.permission == "read_write"

    def test_is_authenticated_true(self) -> None:
        """is_authenticated class attribute is True."""
        assert _ApiKeyAuth(permission="read").is_authenticated is True

    def test_different_tiers(self) -> None:
        """All standard tier strings are stored verbatim."""
        for tier in ("read", "read_write", "admin"):
            assert _ApiKeyAuth(permission=tier).permission == tier


# ---------------------------------------------------------------------------
# FrisianMcpApiKeyAuthentication
# ---------------------------------------------------------------------------


class TestFrisianMcpApiKeyAuthentication:
    """Tests for the settings-backed static API key auth class."""

    @staticmethod
    def _auth() -> FrisianMcpApiKeyAuthentication:
        return FrisianMcpApiKeyAuthentication()

    @staticmethod
    def _fake_request(meta: dict[str, str]) -> Any:
        class _Req:
            META = meta

        return _Req()

    def test_no_header_returns_none(self, settings: Any) -> None:
        """No Authorization header → None."""
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token("somekey"): "read"}
        req = self._fake_request({})
        assert self._auth().authenticate(req) is None

    def test_wrong_prefix_returns_none(self, settings: Any) -> None:
        """Authorization: Token <x> (not Bearer) → None."""
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token("somekey"): "read"}
        req = self._fake_request({"HTTP_AUTHORIZATION": "Token somekey"})
        assert self._auth().authenticate(req) is None

    def test_empty_api_keys_returns_none(self, settings: Any) -> None:
        """FRISIAN_MCP_API_KEYS = {} → None (nothing to match against)."""
        settings.FRISIAN_MCP_API_KEYS = {}
        req = self._fake_request({"HTTP_AUTHORIZATION": "Bearer anything"})
        assert self._auth().authenticate(req) is None

    def test_missing_api_keys_setting_returns_none(self, settings: Any) -> None:
        """FRISIAN_MCP_API_KEYS absent → None."""
        if hasattr(settings, "FRISIAN_MCP_API_KEYS"):
            del settings.FRISIAN_MCP_API_KEYS
        req = self._fake_request({"HTTP_AUTHORIZATION": "Bearer anything"})
        assert self._auth().authenticate(req) is None

    def test_valid_key_returns_anonymous_user_with_tier(self, settings: Any) -> None:
        """Matching key returns (AnonymousUser, _ApiKeyAuth) with correct tier."""
        raw = "my-secret-key"
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token(raw): "read_write"}
        req = self._fake_request({"HTTP_AUTHORIZATION": f"Bearer {raw}"})
        result = self._auth().authenticate(req)
        assert result is not None
        user, auth = result
        assert isinstance(user, AnonymousUser)
        assert isinstance(auth, _ApiKeyAuth)
        assert auth.permission == "read_write"

    def test_raw_key_in_settings_does_not_match(self, settings: Any) -> None:
        """Raw (unhashed) key stored in settings does not authenticate the raw Bearer."""
        raw = "correct-key"
        settings.FRISIAN_MCP_API_KEYS = {raw: "read"}  # raw, not hashed
        req = self._fake_request({"HTTP_AUTHORIZATION": f"Bearer {raw}"})
        # The incoming Bearer is hashed before comparison; raw stored key won't match.
        assert self._auth().authenticate(req) is None

    def test_unrecognised_key_returns_none(self, settings: Any) -> None:
        """Unrecognised Bearer token → None (does NOT raise AuthenticationFailed)."""
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token("correct-key"): "read"}
        req = self._fake_request({"HTTP_AUTHORIZATION": "Bearer wrong-key"})
        assert self._auth().authenticate(req) is None

    def test_multiple_keys_correct_tier_returned(self, settings: Any) -> None:
        """Multiple keys in the dict — matching key's tier is returned."""
        settings.FRISIAN_MCP_API_KEYS = {
            _hmac_token("read-key"): "read",
            _hmac_token("rw-key"): "read_write",
        }
        req = self._fake_request({"HTTP_AUTHORIZATION": "Bearer rw-key"})
        result = self._auth().authenticate(req)
        assert result is not None
        assert result[1].permission == "read_write"

    def test_authenticate_header_returns_bearer(self, rf: RequestFactory) -> None:
        """authenticate_header() returns a Bearer realm string."""
        req = rf.get("/")
        header = self._auth().authenticate_header(req)
        assert header.startswith("Bearer")
        assert "frisian-mcp" in header

        @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
        def test_authenticate_header_omits_resource_metadata_when_discovery_disabled(
            self,
            rf: RequestFactory,
         ) -> None:
            """authenticate_header() should omit OAuth discovery metadata when disabled."""
            header = self._auth().authenticate_header(rf.get("/"))

            assert header == 'Bearer realm="frisian-mcp"'

    def test_is_authenticated_on_result(self, settings: Any) -> None:
        """The returned auth object has is_authenticated=True."""
        raw = "key"
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token(raw): "read"}
        req = self._fake_request({"HTTP_AUTHORIZATION": f"Bearer {raw}"})
        _, auth = self._auth().authenticate(req)
        assert auth.is_authenticated is True


# ---------------------------------------------------------------------------
# Integration: McpView + FrisianMcpApiKeyAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMcpViewApiKeyIntegration:
    """Integration tests: McpView + FrisianMcpApiKeyAuthentication."""

    def test_no_key_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """No Authorization header → 401 (authenticate_header triggers WWW-Auth challenge)."""
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token("secret"): "read"}
        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload)
            response = _view(request)

        assert response.status_code == 401

    def test_unrecognised_key_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """
        Bearer token that doesn't match any API key or DB token → 401.

        Both authenticators return ``None`` for unrecognised Bearer values so
        that an upstream OAuth authenticator can still validate them.  The
        401 comes from ``IsAuthenticated`` after the chain falls through to
        ``AnonymousUser``.
        """
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token("correct"): "read"}
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer("wrong"))
            response = _view(request)

        assert response.status_code == 401

    def test_valid_key_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """
        Valid API key with no permission guard → request succeeds.

        The MCP gateway is tier-based, not IsAuthenticated-based.  API keys
        authenticate the request (setting request.auth.permission) without
        requiring a linked Django user, so AllowAny / empty permission classes
        is the correct gate for this authenticator.
        """
        raw = "valid-key"
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token(raw): "read"}
        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("frisian_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(raw))
            response = _view(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["result"] == {}
