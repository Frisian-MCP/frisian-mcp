"""Tests for friese_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from friese_mcp.contrib.tokens.models import FrieseMcpToken
from friese_mcp.contrib.tokens.permissions import IsAuthenticatedOrServiceToken
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView

User = get_user_model()

_view = McpEndpointView.as_view()
_PERM = "friese_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken"
_AUTH = "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(user: Any = None, auth: Any = None) -> Any:
    """Return a minimal request-like object with user and auth attributes."""

    class _Req:
        pass

    req = _Req()
    req.user = user if user is not None else AnonymousUser()  # pylint: disable=attribute-defined-outside-init
    req.auth = auth  # pylint: disable=attribute-defined-outside-init
    return req


def _post_mcp(rf: RequestFactory, payload: Any, meta: dict[str, str] | None = None) -> Any:
    """Build a POST request to the MCP endpoint."""
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if meta:
        kwargs.update(meta)
    return rf.post("/mcp/", data=json.dumps(payload), **kwargs)


# ---------------------------------------------------------------------------
# Unit tests for has_permission()
# ---------------------------------------------------------------------------


class TestIsAuthenticatedOrServiceToken:
    """Unit tests for the has_permission logic."""

    def test_authenticated_user_is_allowed(self) -> None:
        """A request with an authenticated user passes the permission check."""

        class _AuthUser:
            is_authenticated = True

        req = _make_request(user=_AuthUser())
        assert IsAuthenticatedOrServiceToken().has_permission(req, None) is True

    def test_anonymous_user_with_no_auth_is_denied(self) -> None:
        """AnonymousUser with no auth token is denied."""
        req = _make_request()
        assert IsAuthenticatedOrServiceToken().has_permission(req, None) is False

    def test_anonymous_user_with_service_token_is_allowed(self) -> None:
        """AnonymousUser whose request.auth is an active FrieseMcpToken is allowed."""
        token = FrieseMcpToken.__new__(FrieseMcpToken)
        token.is_active = True
        req = _make_request(user=AnonymousUser(), auth=token)
        assert IsAuthenticatedOrServiceToken().has_permission(req, None) is True

    def test_inactive_service_token_is_denied(self) -> None:
        """AnonymousUser with an inactive FrieseMcpToken is denied."""
        token = FrieseMcpToken.__new__(FrieseMcpToken)
        token.is_active = False
        req = _make_request(user=AnonymousUser(), auth=token)
        assert IsAuthenticatedOrServiceToken().has_permission(req, None) is False

    def test_non_token_auth_object_is_denied(self) -> None:
        """Non-FrieseMcpToken auth object does not satisfy the service-token path."""
        req = _make_request(user=AnonymousUser(), auth=object())
        assert IsAuthenticatedOrServiceToken().has_permission(req, None) is False

    def test_none_user_with_service_token_is_allowed(self) -> None:
        """None user with an active FrieseMcpToken auth is allowed."""

        class _Req:
            user = None
            auth = FrieseMcpToken.__new__(FrieseMcpToken)
            auth.is_active = True

        assert IsAuthenticatedOrServiceToken().has_permission(_Req(), None) is True

    def test_is_base_permission_subclass(self) -> None:
        """IsAuthenticatedOrServiceToken is a DRF BasePermission subclass."""
        from rest_framework.permissions import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            BasePermission,
        )

        assert issubclass(IsAuthenticatedOrServiceToken, BasePermission)


# ---------------------------------------------------------------------------
# Integration: McpEndpointView + FrieseMcpTokenAuthentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsAuthenticatedOrServiceTokenIntegration:
    """Integration: permission class with token auth and the MCP endpoint."""

    def _configure(self, settings: Any) -> None:
        """Wire token auth + IsAuthenticatedOrServiceToken into the MCP gateway."""
        settings.FRIESE_MCP_AUTHENTICATION_CLASSES = [_AUTH]
        settings.FRIESE_MCP_PERMISSION_CLASSES = [_PERM]

    def test_user_token_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """A token linked to a Django user is allowed."""
        self._configure(settings)
        user = User.objects.create_user(username="bob", password="pw")
        token = FrieseMcpToken.objects.create(name="bob-token", user=user)

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        bearer = {"HTTP_AUTHORIZATION": f"Bearer {token.plaintext_token}"}
        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, bearer)
            response = _view(request)

        assert response.status_code == 200

    def test_service_token_allows_request(self, rf: RequestFactory, settings: Any) -> None:
        """A service token (no linked user) is allowed."""
        self._configure(settings)
        token = FrieseMcpToken.objects.create(name="service-token")

        isolated = ToolRegistry()
        isolated.register("ping", lambda a, r: {}, "Ping", {})
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        bearer = {"HTTP_AUTHORIZATION": f"Bearer {token.plaintext_token}"}
        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, bearer)
            response = _view(request)

        assert response.status_code == 200

    def test_no_token_is_rejected(self, rf: RequestFactory, settings: Any) -> None:
        """A request with no Authorization header is rejected with 401."""
        self._configure(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload)
            response = _view(request)

        assert response.status_code == 401

    def test_invalid_token_is_rejected(self, rf: RequestFactory, settings: Any) -> None:
        """A request with an unrecognised token is rejected with 401."""
        self._configure(settings)
        isolated = ToolRegistry()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

        with patch("friese_mcp.views.tool_registry", isolated):
            request = _post_mcp(rf, payload, {"HTTP_AUTHORIZATION": "Bearer notavalidtoken"})
            response = _view(request)

        assert response.status_code == 401
