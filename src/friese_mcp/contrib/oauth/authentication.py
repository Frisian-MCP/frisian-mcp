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

import logging
from typing import Any

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import OAuthAccessToken, _hmac_secret
from .views import _get_base_url

logger = logging.getLogger(__name__)


class OAuthServicePrincipal:
    """
    Principal set as ``request.user`` for OAuth-authenticated MCP requests.

    ``is_authenticated = True`` satisfies DRF's ``IsAuthenticated``.  The
    permission tier is mapped to Django's ``is_superuser`` / ``is_staff`` flags
    so that host frameworks that call the standard Django permission interface
    (``has_perm``, ``get_all_permissions``, ``has_module_perms``) work correctly
    without a database-backed user record.

    Tier mapping:

    * ``admin``      — ``is_superuser = True``; Django bypasses all permission
                       checks, which is appropriate for a fully-trusted service
                       principal.
    * ``read_write`` — ``is_staff = True``; permission methods return ``True``
                       because MCP tier filtering is the real access gate.
    * ``read``       — no elevated flags; permission methods return ``False`` for
                       any write-class permission, providing a defence-in-depth
                       layer on top of MCP tool filtering.
    """

    is_authenticated: bool = True
    is_anonymous: bool = False
    is_active: bool = True
    pk: None = None
    id: None = None

    def __init__(self, permission: str = "read") -> None:
        self.permission = permission
        self.is_superuser: bool = permission == "admin"
        self.is_staff: bool = permission in ("read_write", "admin")

    # ------------------------------------------------------------------
    # Django permission interface
    # Required by host apps (e.g. Nautobot) that call these on request.user.
    # ------------------------------------------------------------------

    def get_all_permissions(self, obj: object = None) -> set:
        return set()

    def has_perm(self, perm: str, obj: object = None) -> bool:
        if self.is_superuser:
            return True
        if self.permission == "read_write":
            return True
        return False

    def has_perms(self, perm_list: object, obj: object = None) -> bool:
        return all(self.has_perm(p, obj) for p in perm_list)

    def has_module_perms(self, app_label: str) -> bool:
        return self.is_superuser or self.permission == "read_write"


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
        # Tokens are stored as HMAC-SHA256 digests (SEC-1).  Hash the bearer
        # value before lookup so a leaked DB row cannot be replayed directly.
        try:
            access_token = OAuthAccessToken.objects.select_related("client").get(
                token=_hmac_secret(token_str),
            )
        except OAuthAccessToken.DoesNotExist:
            raise AuthenticationFailed("Invalid OAuth token.") from None

        if not access_token.client.is_active:
            raise AuthenticationFailed("OAuth client is inactive.")

        if access_token.is_expired():
            raise AuthenticationFailed("OAuth token has expired.")

        OAuthAccessToken.objects.filter(pk=access_token.pk).update(last_used_at=timezone.now())

        principal = OAuthServicePrincipal(permission=access_token.permission)

        # Some host frameworks (e.g. Nautobot) require request.user to be a real
        # Django User instance for audit-log FKs (ObjectChange.user).  Resolve a
        # backing User in priority order:
        #   1. FRIESE_MCP_OAUTH_SERVICE_USER setting — explicit named account.
        #   2. Auto-detect: first superuser in the DB (covers Nautobot and similar).
        #   3. Fall back to OAuthServicePrincipal (no User model or no superuser).
        # Tier resolution is unaffected — _resolve_request_tier reads
        # request.auth.permission (the OAuthAccessToken), not request.user.
        try:
            from django.contrib.auth import get_user_model  # pylint: disable=import-outside-toplevel
            User = get_user_model()
            service_username: str | None = getattr(settings, "FRIESE_MCP_OAUTH_SERVICE_USER", None)
            if service_username:
                django_user = User.objects.filter(username=service_username).first()
                if django_user is None:
                    logger.warning(
                        "FRIESE_MCP_OAUTH_SERVICE_USER '%s' not found; trying superuser",
                        service_username,
                    )
            else:
                django_user = None

            if django_user is None:
                django_user = User.objects.filter(is_superuser=True).order_by("pk").first()

            if django_user is not None:
                return (django_user, access_token)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        return (principal, access_token)

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        base = _get_base_url(request)
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        return f'Bearer realm="friese-mcp", resource_metadata="{resource_metadata}"'
