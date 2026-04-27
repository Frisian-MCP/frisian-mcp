"""
FrieseMcpTokenAuthentication — DRF authentication class for static Bearer tokens.

Reads the ``Authorization: Bearer <token>`` header, looks up the token in
:class:`~friese_mcp.contrib.tokens.models.FrieseMcpToken`, and returns the
associated user (or ``AnonymousUser`` for service tokens).

Wire into the MCP gateway via settings::

    FRIESE_MCP_AUTHENTICATION_CLASSES = [
        "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
    ]

"""

from __future__ import annotations

from typing import Any

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
