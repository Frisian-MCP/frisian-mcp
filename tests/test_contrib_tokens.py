"""Tests for friese_mcp.contrib.tokens — FrieseMcpToken model + authentication."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from rest_framework.exceptions import AuthenticationFailed

from friese_mcp.contrib.tokens.authentication import FrieseMcpTokenAuthentication
from friese_mcp.contrib.tokens.models import FrieseMcpToken
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView

User = get_user_model()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_view = McpEndpointView.as_view()


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
# FrieseMcpToken model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFrieseMcpTokenModel:
    """Tests for the FrieseMcpToken model."""

    def test_token_auto_generated_on_save(self) -> None:
        """Token field is populated automatically when a new token is saved."""
        token = FrieseMcpToken.objects.create(name="test-token")
        assert token.token
        assert len(token.token) == 64  # secrets.token_hex(32) → 64 hex chars

    def test_token_not_overwritten_on_update(self) -> None:
        """Existing token value is preserved on subsequent saves."""
        token = FrieseMcpToken.objects.create(name="test-token")
        original = token.token
        token.name = "renamed"
        token.save()
        token.refresh_from_db()
        assert token.token == original

    def test_str_active(self) -> None:
        """__str__ includes name and 'active' for an active token."""
        token = FrieseMcpToken(name="my-agent", is_active=True)
        assert "my-agent" in str(token)
        assert "active" in str(token)

    def test_str_inactive(self) -> None:
        """__str__ includes 'inactive' for a deactivated token."""
        token = FrieseMcpToken(name="old-token", is_active=False)
        assert "inactive" in str(token)

    def test_each_token_unique(self) -> None:
        """Two tokens created back-to-back have different token values."""
        t1 = FrieseMcpToken.objects.create(name="t1")
        t2 = FrieseMcpToken.objects.create(name="t2")
        assert t1.token != t2.token

    def test_service_token_no_user(self) -> None:
        """Tokens can be created without a linked user (service tokens)."""
        token = FrieseMcpToken.objects.create(name="service")
        assert token.user is None
        assert token.user_id is None

    def test_user_linked_token(self) -> None:
        """Tokens can be linked to a Django user."""
        user = User.objects.create_user(username="alice", password="pw")
        token = FrieseMcpToken.objects.create(name="alice-token", user=user)
        token.refresh_from_db()
        assert token.user == user


# ---------------------------------------------------------------------------
# FrieseMcpTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFrieseMcpTokenAuthentication:
    """Tests for the FrieseMcpTokenAuthentication DRF class."""

    @staticmethod
    def _auth() -> FrieseMcpTokenAuthentication:
        """Return a fresh auth instance."""
        return FrieseMcpTokenAuthentication()

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
        token = FrieseMcpToken.objects.create(name="bob-token", user=user)

        req = self._fake_request(_bearer(token.token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, auth_token = result
        assert auth_user == user
        assert auth_token.pk == token.pk

    def test_valid_service_token_returns_anonymous(self) -> None:
        """Valid token with no user returns (AnonymousUser, token)."""
        token = FrieseMcpToken.objects.create(name="svc")
        req = self._fake_request(_bearer(token.token))
        result = self._auth().authenticate(req)
        assert result is not None
        auth_user, _ = result
        assert isinstance(auth_user, AnonymousUser)

    def test_invalid_token_raises_auth_failed(self) -> None:
        """Unrecognised token string raises AuthenticationFailed."""
        req = self._fake_request(_bearer("notarealtoken"))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_inactive_token_raises_auth_failed(self) -> None:
        """Inactive token raises AuthenticationFailed."""
        token = FrieseMcpToken.objects.create(name="disabled", is_active=False)
        req = self._fake_request(_bearer(token.token))
        with pytest.raises(AuthenticationFailed):
            self._auth().authenticate(req)

    def test_last_used_at_updated_on_auth(self) -> None:
        """last_used_at is set after a successful authentication."""
        token = FrieseMcpToken.objects.create(name="t")
        assert token.last_used_at is None
        req = self._fake_request(_bearer(token.token))
        self._auth().authenticate(req)
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_authenticate_header_returns_bearer(self) -> None:
        """authenticate_header() returns a Bearer realm string."""
        req = self._fake_request({})
        header = self._auth().authenticate_header(req)
        assert header.startswith("Bearer")


# ---------------------------------------------------------------------------
# Integration: McpEndpointView + FrieseMcpTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMcpEndpointTokenIntegration:
    """Integration tests: McpEndpointView + FrieseMcpTokenAuthentication + IsAuthenticated."""

    def _configure_auth(self, settings: Any) -> None:
        """Point the MCP gateway at FrieseMcpTokenAuthentication + IsAuthenticated."""
        settings.FRIESE_MCP_AUTHENTICATION_CLASSES = [
            "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication"
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
            response = _view(request)

        assert response.status_code == 401

    def test_invalid_token_returns_401(self, rf: RequestFactory, settings: Any) -> None:
        """Request with an invalid token is rejected with 401."""
        self._configure_auth(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer("invalidtoken"))
            response = _view(request)

        assert response.status_code == 401

    def test_valid_token_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """Request with a valid active token succeeds."""
        self._configure_auth(settings)
        user = User.objects.create_user(username="carol", password="pw")
        token = FrieseMcpToken.objects.create(name="carol-token", user=user)

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, _bearer(token.token))
            response = _view(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["result"] == {}
