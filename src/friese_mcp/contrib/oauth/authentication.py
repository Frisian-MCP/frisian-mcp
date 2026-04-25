"""
OAuthTokenAuthentication — DRF authentication class for OAuth 2.0 Bearer tokens.

Reads the ``Authorization: Bearer <token>`` header, looks up the token in
:class:`~friese_mcp.contrib.oauth.models.OAuthAccessToken`, checks expiry and
client active status, and returns ``(OAuthServicePrincipal, access_token)`` on success.

Wire into the MCP gateway via settings::

    FRIESE_MCP_AUTHENTICATION_CLASSES = [
        "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
    ]

To accept *either* OAuth tokens or static Bearer tokens::

    FRIESE_MCP_AUTHENTICATION_CLASSES = [
        "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
        "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
    ]

"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import OAuthAccessToken
from .views import _get_base_url


class OAuthServicePrincipal:
    """
    Minimal principal object set as ``request.user`` for OAuth client_credentials auth.

    Unlike :class:`~django.contrib.auth.models.AnonymousUser`, this principal has
    ``is_authenticated = True`` so that DRF's ``IsAuthenticated`` permission class
    allows the request.  There is no associated Django user — the MCP client is an
    AI agent or service, not a human account.
    """

    is_authenticated: bool = True
    is_anonymous: bool = False
    is_active: bool = True
    is_staff: bool = False
    is_superuser: bool = False
    pk: None = None
    id: None = None


class OAuthTokenAuthentication(BaseAuthentication):
    """
    DRF authentication class that validates OAuth 2.0 Bearer tokens.

    Only requests carrying ``Authorization: Bearer <token>`` are handled.
    All other requests return ``None`` so that DRF can try the next
    configured authenticator.

    On success, returns ``(OAuthServicePrincipal, access_token)`` where *access_token*
    is the :class:`~friese_mcp.contrib.oauth.models.OAuthAccessToken` instance.

    On failure (token not found, expired, or client inactive), raises
    :class:`~rest_framework.exceptions.AuthenticationFailed`.
    """

    def authenticate(self, request: Any) -> tuple[Any, Any] | None:
        """
        Authenticate the request from an OAuth 2.0 Bearer token.

        Returns ``(OAuthServicePrincipal, access_token)`` on success, ``None`` when the
        header is absent, or raises
        :class:`~rest_framework.exceptions.AuthenticationFailed` when the token
        is invalid, expired, or the issuing client is inactive.
        """
        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token_str = auth_header[len("Bearer ") :]
        try:
            access_token = OAuthAccessToken.objects.select_related("client").get(
                token=token_str,
            )
        except OAuthAccessToken.DoesNotExist:
            raise AuthenticationFailed("Invalid OAuth token.") from None

        if not access_token.client.is_active:
            raise AuthenticationFailed("OAuth client is inactive.")

        if access_token.is_expired():
            raise AuthenticationFailed("OAuth token has expired.")

        OAuthAccessToken.objects.filter(pk=access_token.pk).update(last_used_at=timezone.now())

        return (OAuthServicePrincipal(), access_token)

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        base = _get_base_url(request)
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        return (
            f'Bearer realm="friese-mcp",'
            f' resource_metadata="{resource_metadata}",'
            f' error="invalid_token"'
        )
