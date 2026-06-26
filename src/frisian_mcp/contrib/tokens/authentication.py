"""
Authentication classes for frisian-mcp Bearer tokens.

Two classes are provided:

``FrisianMcpTokenAuthentication``
    DB-backed tokens: looks up the HMAC of the Bearer value in
    :class:`~frisian_mcp.contrib.tokens.models.FrisianMcpToken`.

``FrisianMcpApiKeyAuthentication``
    Settings-backed static keys: reads the ``FRISIAN_MCP_API_KEYS`` dict
    (``{hmac_of_key: tier}``) and authenticates without any DB lookup.

    Keys **must** be stored as HMAC-SHA256 digests (64 hex chars), not raw
    strings.  Generate a hash with::

        python manage.py mcp_hash_api_key <raw-key>

Wire both into the MCP gateway via settings::

    FRISIAN_MCP_AUTHENTICATION_CLASSES = [
        "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
        "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    ]

Both classes return ``None`` when the Bearer value is absent **or** when it
does not match any stored token, so either can be safely chained ahead of
another Bearer authenticator (e.g.
``frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication``)
without short-circuiting the chain on a lookup miss.

"""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication

from .models import FrisianMcpToken, _hmac_token


class FrisianMcpTokenAuthentication(BaseAuthentication):
    """
    DRF authentication class that validates static MCP Bearer tokens.

    Only requests carrying ``Authorization: Bearer <token>`` are handled.
    All other requests return ``None`` so that DRF can try the next
    configured authenticator.

    On success, returns ``(user, token)`` where *user* is the Django user
    associated with the token, or :class:`~django.contrib.auth.models.AnonymousUser`
    for service tokens with no linked user.

    On failure (header absent, wrong scheme, or token not found / inactive),
    returns ``None`` so that DRF can try the next configured authenticator.
    A Bearer value that does not match any stored ``FrisianMcpToken`` row may
    legitimately belong to another authenticator in the chain (for example an
    OAuth-issued access token), so an unrecognised value is a fall-through
    rather than a hard rejection.

    Updates ``FrisianMcpToken.last_used_at`` on every successful authentication
    using a queryset update (no full model save, no post_save signals).

    """

    def authenticate(self, request: Any) -> tuple[Any, Any] | None:
        """
        Authenticate the request from a Bearer token.

        Returns ``(user, token)`` on success, or ``None`` when the header is
        absent, the scheme is not Bearer, or the Bearer value does not match
        an active ``FrisianMcpToken``.  Never raises ``AuthenticationFailed``:
        unrecognised tokens are passed through to the next authenticator in
        ``FRISIAN_MCP_AUTHENTICATION_CLASSES`` rather than short-circuiting
        the chain.

        """
        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token_str = auth_header[len("Bearer ") :]
        try:
            token = FrisianMcpToken.objects.select_related("user").get(
                token=_hmac_token(token_str),
                is_active=True,
            )
        except FrisianMcpToken.DoesNotExist:
            # Fall through so chained authenticators (e.g. OAuthTokenAuthentication)
            # can validate the Bearer value against their own token store.
            return None

        # Update last_used_at without triggering a full model save or signals.
        FrisianMcpToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        user = token.user if token.user is not None else AnonymousUser()
        return (user, token)

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        from django.apps import apps  # pylint: disable=import-outside-toplevel

        if apps.is_installed("frisian_mcp.contrib.oauth"):
            if not getattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY", True):
                return 'Bearer realm="frisian-mcp"'

            try:
                from frisian_mcp.contrib.oauth.views import (
                    _get_base_url,
                )
            except ImportError:
                return 'Bearer realm="frisian-mcp"'

            base = _get_base_url(request)
            resource_metadata = f"{base}/.well-known/oauth-protected-resource"
            return (
                f'Bearer realm="frisian-mcp", '
                f'resource_metadata="{resource_metadata}"'
            )

        return 'Bearer realm="frisian-mcp"'

class _ApiKeyAuth:
    """Lightweight auth object set as ``request.auth`` for API key authentications."""

    __slots__ = ("permission",)

    def __init__(self, permission: str) -> None:
        self.permission = permission

    is_authenticated: bool = True


class FrisianMcpApiKeyAuthentication(BaseAuthentication):
    """
    DRF authentication class for settings-backed static API keys.

    Reads ``FRISIAN_MCP_API_KEYS`` from Django settings — a ``dict`` mapping
    HMAC-SHA256 digests of raw keys to permission tier strings (e.g.
    ``"read"``, ``"read_write"``, ``"admin"``).  No database queries are
    performed.

    Keys in ``FRISIAN_MCP_API_KEYS`` **must** be the HMAC-SHA256 hex digest of
    the raw key, not the raw key itself.  Generate a digest with::

        python manage.py mcp_hash_api_key <raw-key>

    This matches the storage contract of :class:`FrisianMcpTokenAuthentication`
    (DB-backed tokens store only HMACs) so that raw secrets are never present
    in settings, logs, or error-tracking payloads.

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

        Hashes the incoming Bearer value via :func:`~frisian_mcp.contrib.tokens.models._hmac_token`
        and compares against the stored HMAC digests in ``FRISIAN_MCP_API_KEYS``.

        Returns ``(AnonymousUser, _ApiKeyAuth(permission=tier))`` on a match,
        or ``None`` when the header is absent or no key matches.

        """
        api_keys: dict[str, str] = getattr(settings, "FRISIAN_MCP_API_KEYS", {})
        if not api_keys:
            return None

        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        raw_key = auth_header[len("Bearer ") :]
        incoming_digest = _hmac_token(raw_key)

        for stored_digest, tier in api_keys.items():
            if secrets.compare_digest(incoming_digest, stored_digest):
                return (AnonymousUser(), _ApiKeyAuth(permission=tier))

        return None

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        from django.apps import apps  # pylint: disable=import-outside-toplevel

        if apps.is_installed("frisian_mcp.contrib.oauth"):
            try:
                from frisian_mcp.contrib.oauth.views import (  # pylint: disable=import-outside-toplevel
                    _get_base_url,
                )
            except ImportError:
                return 'Bearer realm="frisian-mcp"'
            base = _get_base_url(request)
            resource_metadata = f"{base}/.well-known/oauth-protected-resource"
            return f'Bearer realm="frisian-mcp", resource_metadata="{resource_metadata}"'
        return 'Bearer realm="frisian-mcp"'
