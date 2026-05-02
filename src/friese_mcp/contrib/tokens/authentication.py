"""
Authentication classes for friese-mcp Bearer tokens.

Two classes are provided:

``FrieseMcpTokenAuthentication``
    DB-backed tokens: looks up the HMAC of the Bearer value in
    :class:`~friese_mcp.contrib.tokens.models.FrieseMcpToken`.

``FrieseMcpApiKeyAuthentication``
    Settings-backed static keys: reads the ``FRIESE_MCP_API_KEYS`` dict
    (``{raw_key: tier}``) and authenticates without any DB lookup.

Wire both into the MCP gateway via settings::

    FRIESE_MCP_AUTHENTICATION_CLASSES = [
        "friese_mcp.contrib.tokens.authentication.FrieseMcpApiKeyAuthentication",
        "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
    ]

``FrieseMcpApiKeyAuthentication`` should come first so that static keys are
recognised before ``FrieseMcpTokenAuthentication`` (which raises
``AuthenticationFailed`` for tokens it does not recognise).

"""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import FrieseMcpToken, _hmac_token


class FrieseMcpTokenAuthentication(BaseAuthentication):
    """
    DRF authentication class that validates static MCP Bearer tokens.

    Only requests carrying ``Authorization: Bearer <token>`` are handled.
    All other requests return ``None`` so that DRF can try the next
    configured authenticator.

    On success, returns ``(user, token)`` where *user* is the Django user
    associated with the token, or :class:`~django.contrib.auth.models.AnonymousUser`
    for service tokens with no linked user.

    On failure (token not found or inactive), raises
    :class:`~rest_framework.exceptions.AuthenticationFailed`.

    Updates ``FrieseMcpToken.last_used_at`` on every successful authentication
    using a queryset update (no full model save, no post_save signals).

    """

    def authenticate(self, request: Any) -> tuple[Any, Any] | None:
        """
        Authenticate the request from a Bearer token.

        Returns ``(user, token)`` on success, ``None`` when the header is
        absent, or raises :class:`~rest_framework.exceptions.AuthenticationFailed`
        when the token is invalid or inactive.

        """
        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token_str = auth_header[len("Bearer ") :]
        try:
            token = FrieseMcpToken.objects.select_related("user").get(
                token=_hmac_token(token_str),
                is_active=True,
            )
        except FrieseMcpToken.DoesNotExist:
            raise AuthenticationFailed("Invalid or inactive MCP token.") from None

        # Update last_used_at without triggering a full model save or signals.
        FrieseMcpToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        user = token.user if token.user is not None else AnonymousUser()
        return (user, token)

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        from django.apps import apps  # pylint: disable=import-outside-toplevel

        if apps.is_installed("friese_mcp.contrib.oauth"):
            try:
                from friese_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
                    _get_base_url,
                )
            except ImportError:
                return 'Bearer realm="friese-mcp"'
            base = _get_base_url(request)
            resource_metadata = f"{base}/.well-known/oauth-protected-resource"
            return f'Bearer realm="friese-mcp", resource_metadata="{resource_metadata}"'
        return 'Bearer realm="friese-mcp"'


class _ApiKeyAuth:
    """Lightweight auth object set as ``request.auth`` for API key authentications."""

    __slots__ = ("permission",)

    def __init__(self, permission: str) -> None:
        self.permission = permission

    is_authenticated: bool = True


class FrieseMcpApiKeyAuthentication(BaseAuthentication):
    """
    DRF authentication class for settings-backed static API keys.

    Reads ``FRIESE_MCP_API_KEYS`` from Django settings — a ``dict`` mapping
    plaintext raw keys to permission tier strings (e.g. ``"read"``,
    ``"read_write"``, ``"admin"``).  No database queries are performed.

    Only requests carrying ``Authorization: Bearer <key>`` are handled.
    All other requests return ``None`` so that DRF can try the next
    configured authenticator.

    On success, returns ``(AnonymousUser, _ApiKeyAuth(permission=tier))``.
    On failure (no matching key), returns ``None`` so subsequent authenticators
    can try.  Never raises ``AuthenticationFailed`` — unrecognised tokens are
    passed through rather than rejected.

    All key comparisons use :func:`secrets.compare_digest` to prevent
    timing-based key enumeration.

    """

    def authenticate(self, request: Any) -> tuple[Any, Any] | None:
        """
        Authenticate the request from a static API key.

        Returns ``(AnonymousUser, _ApiKeyAuth(permission=tier))`` on a match,
        or ``None`` when the header is absent or no key matches.

        """
        api_keys: dict[str, str] = getattr(settings, "FRIESE_MCP_API_KEYS", {})
        if not api_keys:
            return None

        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        raw_key = auth_header[len("Bearer ") :]

        for key, tier in api_keys.items():
            if secrets.compare_digest(raw_key, key):
                return (AnonymousUser(), _ApiKeyAuth(permission=tier))

        return None

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        return 'Bearer realm="friese-mcp"'
