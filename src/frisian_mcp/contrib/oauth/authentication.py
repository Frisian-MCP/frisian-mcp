"""
OAuthTokenAuthentication — DRF authentication class for OAuth 2.0 Bearer tokens.

Reads the ``Authorization: Bearer <token>`` header, looks up the token in
:class:`~frisian_mcp.contrib.oauth.models.OAuthAccessToken`, checks expiry and
client active status, and returns ``(OAuthServicePrincipal, access_token)`` on success.

Wire into the MCP gateway via settings::

    FRISIAN_MCP_AUTHENTICATION_CLASSES = [
        "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
    ]

To accept *either* OAuth tokens or static Bearer tokens::

    FRISIAN_MCP_AUTHENTICATION_CLASSES = [
        "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
        "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    ]

"""

from __future__ import annotations

import logging
from collections.abc import Iterable
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
    permission tier controls the Django staff flag and permission methods so
    that host frameworks using the standard Django permission interface
    (``has_perm``, ``get_all_permissions``, ``has_module_perms``) work
    correctly without a database-backed user record.

    ``is_superuser`` is intentionally never set to ``True``.  Django bypasses
    all object-level permission checks for superusers, which is too broad for
    a service principal that may interact with host-app models.  Host code that
    needs to distinguish the admin MCP tier should check
    ``request.auth.permission == "admin"`` directly rather than relying on
    ``request.user.is_superuser``.

    Tier mapping:

    * ``admin``      — ``is_staff = True``; ``has_perm`` / ``has_module_perms``
                       return ``True`` (MCP tier filtering is the real gate).
    * ``read_write`` — ``is_staff = True``; same as admin at the Django level.
    * ``read``       — no elevated flags; permission methods return ``False``
                       for any write-class check.
    """

    is_authenticated: bool = True
    is_anonymous: bool = False
    is_active: bool = True
    is_superuser: bool = False
    pk: None = None
    id: None = None
    #: Marker checked by ``_ensure_perm_context_on_request`` so that blanket-tier
    #: OAuth clients (no linked Django User) skip per-capability filtering and let
    #: the tier be the sole gate — matching the "API token" behaviour the operator
    #: expects when leaving the User field blank on the OAuthClient admin form.
    _mcp_is_service_principal: bool = True

    def __init__(self, permission: str = "read") -> None:
        """Set the permission tier and derive is_staff from it."""
        self.permission = permission
        self.is_staff: bool = permission in ("read_write", "admin")

    # ------------------------------------------------------------------
    # Django permission interface
    # Required by host apps that call permission methods on request.user.
    # ------------------------------------------------------------------

    def get_all_permissions(  # pylint: disable=unused-argument
        self, obj: object = None
    ) -> set[str]:
        """Return an empty set; MCP tier filtering is the real permission gate."""
        return set()

    def has_perm(self, perm: str, obj: object = None) -> bool:  # pylint: disable=unused-argument
        """Return True for read_write and admin tiers; False for read-only."""
        return self.permission in ("read_write", "admin")

    def has_perms(self, perm_list: Iterable[str], obj: object = None) -> bool:
        """Return True only when has_perm passes for every permission in perm_list."""
        return all(self.has_perm(p, obj) for p in perm_list)

    def has_module_perms(self, app_label: str) -> bool:  # pylint: disable=unused-argument
        """Return True for read_write and admin tiers; False for read-only."""
        return self.permission in ("read_write", "admin")


class OAuthTokenAuthentication(BaseAuthentication):
    """
    DRF authentication class that validates OAuth 2.0 Bearer tokens.

    Only requests carrying ``Authorization: Bearer <token>`` are handled.
    All other requests return ``None`` so that DRF can try the next
    configured authenticator.

    On success, returns ``(OAuthServicePrincipal, access_token)`` where *access_token*
    is the :class:`~frisian_mcp.contrib.oauth.models.OAuthAccessToken` instance.

    A Bearer value that does not match any stored ``OAuthAccessToken`` row
    returns ``None`` (fall-through) — it may legitimately belong to another
    authenticator in the chain (e.g.
    :class:`~frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication`),
    so an unrecognised value must not short-circuit the chain.  Concrete
    failures (expired token, inactive client) still raise
    :class:`~rest_framework.exceptions.AuthenticationFailed` because they
    describe a token that *did* match this class but cannot be used.
    """

    def authenticate(self, request: Any) -> tuple[Any, Any] | None:
        """
        Authenticate the request from an OAuth 2.0 Bearer token.

        Returns ``(OAuthServicePrincipal, access_token)`` on success.
        Returns ``None`` when the header is absent, the scheme is not Bearer,
        or the Bearer value does not match any stored ``OAuthAccessToken``
        row — the request falls through to the next authenticator in
        ``FRISIAN_MCP_AUTHENTICATION_CLASSES``.  Raises
        :class:`~rest_framework.exceptions.AuthenticationFailed` only when
        the token matches a row but the row is expired or the issuing client
        is inactive.

        The permission tier is read from the issuing **client** at authentication
        time (not the token's stored snapshot) so that permission changes on the
        client propagate to outstanding tokens without waiting for expiry.

        ``request.user`` is set to either:

        * The Django user named by ``FRISIAN_MCP_OAUTH_SERVICE_USER`` (if set and
          the account exists), for host apps that need a real User FK on audit
          records.
        * :class:`OAuthServicePrincipal` otherwise — a lightweight stand-in that
          satisfies DRF's ``IsAuthenticated`` without touching the database.

        The ``is_superuser`` fallback (auto-detecting the first DB superuser) was
        removed because it silently granted superuser-level ``request.user`` access
        to every OAuth token regardless of the token's permission tier.
        """
        auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
        # RFC 7235 §2.1 / RFC 6750 §2.1: scheme names are case-insensitive.
        if not auth_header.lower().startswith("bearer "):
            return None

        token_str = auth_header[7:]  # len("bearer ") == 7; raw case preserved
        # Tokens are stored as HMAC-SHA256 digests (SEC-1).  Hash the bearer
        # value before lookup so a leaked DB row cannot be replayed directly.
        try:
            access_token = OAuthAccessToken.objects.select_related("client", "client__user").get(
                token=_hmac_secret(token_str),
            )
        except OAuthAccessToken.DoesNotExist:
            # Fall through so chained authenticators (e.g. FrisianMcpTokenAuthentication)
            # can validate the Bearer value against their own token store.
            return None

        if not access_token.client.is_active:
            raise AuthenticationFailed("OAuth client is inactive.")

        if access_token.is_expired():
            raise AuthenticationFailed("OAuth token has expired.")

        OAuthAccessToken.objects.filter(pk=access_token.pk).update(last_used_at=timezone.now())

        # Read permission from the client so that admin-console permission
        # changes take effect immediately without waiting for token expiry.
        principal = OAuthServicePrincipal(permission=access_token.client.permission)

        # Resolve request.user to a real Django User instance so that
        # FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY can call get_all_permissions().
        # Priority: per-client user → global FRISIAN_MCP_OAUTH_SERVICE_USER → OAuthServicePrincipal.
        # Do NOT fall back to "first superuser in DB" — that silently elevates
        # every OAuth token to superuser-level request.user (SEC-839c3b7c).

        # 1. Per-client user (set in admin on the OAuthClient record).
        if access_token.client.user_id is not None:
            return (access_token.client.user, access_token)

        # 2. Global service user fallback.
        service_username: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_SERVICE_USER", None)
        if service_username:
            try:
                from django.contrib.auth import (  # pylint: disable=import-outside-toplevel
                    get_user_model,
                )

                user_model = get_user_model()
                django_user = user_model.objects.filter(username=service_username).first()
                if django_user is not None:
                    return (django_user, access_token)
                logger.warning(
                    "FRISIAN_MCP_OAUTH_SERVICE_USER '%s' not found; "
                    "falling back to OAuthServicePrincipal",
                    service_username,
                )
            except Exception:  # pylint: disable=broad-exception-caught  # noqa: BLE001
                logger.debug(
                    "Could not resolve FRISIAN_MCP_OAUTH_SERVICE_USER; "
                    "falling back to OAuthServicePrincipal",
                    exc_info=True,
                )

        return (principal, access_token)

    def authenticate_header(self, request: Any) -> str:
        """Return the WWW-Authenticate header value for 401 responses."""
        if not getattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY", True):
            return 'Bearer realm="frisian-mcp"'

        base = _get_base_url(request)
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        return f'Bearer realm="frisian-mcp", resource_metadata="{resource_metadata}"'
