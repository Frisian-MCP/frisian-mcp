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

#: Tier ranking shared with the OAuth views' tier-promotion ladder.  Indexed
#: lookup is the comparison primitive used by :func:`_effective_tier`.  Tier
#: strings outside this list are treated as the lowest tier ("read") per the
#: package's safe-default convention.
_TIER_RANK: tuple[str, ...] = ("read", "read_write", "admin")

#: In-memory throttle for the "snapshot narrower than client" INFO log.
#: A frozenset entry of (token_pk, observed_client_tier) is appended on first
#: emission; subsequent observations of the same pair within the process
#: lifetime are suppressed.  Bounded growth (one entry per unique observed
#: pairing per process); resets on process restart.  Not persisted, not shared
#: across workers — the log signal is operational, not authoritative.
_LOGGED_NARROWER_AUTHORITY: set[tuple[Any, str]] = set()


def _tier_rank(tier: str) -> int:
    """Return the ordinal rank of *tier* (unknown tiers fall through to 0 / "read")."""
    if tier in _TIER_RANK:
        return _TIER_RANK.index(tier)
    return 0


def _tier_permissions_for(tier: str) -> set[str]:
    """Return the accumulated Django-perm allowlist for *tier* with inheritance.

    Reads ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` (default ``{}``), which is a
    ``dict[str, list[str]]`` mapping each MCP tier name to the
    fully-qualified Django permission strings the operator wants to grant at
    that tier.  Inheritance is monotonic up the tier ladder: ``admin``
    accumulates its own list plus ``read_write`` plus ``read``;
    ``read_write`` accumulates its own plus ``read``; ``read`` returns only
    its own list.  This matches the typical RBAC mental model where a
    higher tier is a superset of every lower tier.

    Returns an empty set when ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` is
    unset, empty, or not a ``dict`` (defensive against operator
    misconfiguration).  Unknown tier keys in the dict (anything outside
    ``_TIER_RANK``) are silently ignored.

    Closes M-oauth-has-perm-blanket-true (T10).
    """
    tier_perms: object = getattr(settings, "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS", {})
    if not isinstance(tier_perms, dict):
        return set()
    rank = _tier_rank(tier)
    accumulated: set[str] = set()
    for lower in _TIER_RANK[: rank + 1]:
        entries: object = tier_perms.get(lower, [])
        if not isinstance(entries, list):
            # Defensive: an operator might set a tier to a single string by
            # mistake.  Skip the malformed entry rather than raising at
            # request time.
            continue
        for entry in entries:
            if isinstance(entry, str) and entry:
                accumulated.add(entry)
    return accumulated


def _effective_tier(access_token: OAuthAccessToken) -> str:
    """
    Return the LESSER of the token's issuance snapshot and the live client tier.

    Token authority is fixed at issuance.  An admin-console *downgrade* of the
    issuing client takes effect live (the client tier acts as a narrowing cap
    on every outstanding token).  An admin-console *upgrade* does NOT widen
    previously-issued tokens — the issuance snapshot remains the ceiling.

    Unknown tier strings on either side fall through to ``"read"`` via the
    rank lookup; the function never raises.

    Emits ``oauth_token_authority_narrower_than_client_tier`` (INFO) at most
    once per (token_pk, observed_client_tier) pair per process lifetime when
    the snapshot is strictly narrower than the live client tier.  The signal
    surfaces either a legitimate operator downgrade or a propagated-but-
    blocked escalation attempt; downstream alerting can correlate it with
    the OAuthClient admin audit log to distinguish the two.
    """
    token_tier = access_token.permission
    client_tier = access_token.client.permission
    token_rank = _tier_rank(token_tier)
    client_rank = _tier_rank(client_tier)
    if token_rank < client_rank:
        cache_key = (access_token.pk, client_tier)
        if cache_key not in _LOGGED_NARROWER_AUTHORITY:
            _LOGGED_NARROWER_AUTHORITY.add(cache_key)
            logger.info(
                "oauth_token_authority_narrower_than_client_tier",
                extra={
                    "token_id": access_token.pk,
                    "snapshot_tier": token_tier,
                    "client_tier": client_tier,
                },
            )
    if token_rank <= client_rank:
        return token_tier if token_tier in _TIER_RANK else "read"
    return client_tier if client_tier in _TIER_RANK else "read"


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

    Tier mapping (T10 default-deny + opt-in via TIER_PERMISSIONS):

    * ``admin``      — ``is_staff = True``.  ``has_perm`` / ``has_module_perms``
                       return ``True`` only for perm strings explicitly listed
                       in ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` at the
                       ``admin`` / ``read_write`` / ``read`` keys
                       (inheritance).
    * ``read_write`` — ``is_staff = True``.  ``has_perm`` /
                       ``has_module_perms`` consult only the ``read_write``
                       and ``read`` keys.
    * ``read``       — no elevated flags.  ``has_perm`` /
                       ``has_module_perms`` consult only the ``read`` key.

    Default-deny applies whenever ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` is
    unset, empty, or the perm string is not in the relevant accumulated
    allowlist.  The MCP-internal tier gate is unchanged; T10 only narrows
    host-code reads of ``request.user.has_perm`` so that hosts cannot rely
    on a blanket-True signal.
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

    def has_perm(  # pylint: disable=unused-argument  # ``obj`` is interface-only
        self, perm: str, obj: object = None
    ) -> bool:
        """Return True iff *perm* is in the tier's allowlist (with inheritance).

        Default-deny when ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` is unset or
        does not list *perm* at the current tier (or any lower tier through
        inheritance).  Empty *perm* strings fail closed.  The *obj*
        argument is accepted for ``BaseBackend``-style call compatibility
        but is not used: T10 enforces a string-level allowlist, not an
        object-level check.
        """
        if not perm:
            return False
        return perm in _tier_permissions_for(self.permission)

    def has_perms(self, perm_list: Iterable[str], obj: object = None) -> bool:
        """Return True only when has_perm passes for every permission in perm_list."""
        return all(self.has_perm(p, obj) for p in perm_list)

    def has_module_perms(self, app_label: str) -> bool:
        """Return True iff any allowlisted perm is scoped to *app_label*.

        Default-deny when ``FRISIAN_MCP_OAUTH_TIER_PERMISSIONS`` is unset or
        contains no perm string prefixed with ``app_label + "."``.  Empty
        *app_label* fails closed.
        """
        if not app_label:
            return False
        prefix = f"{app_label}."
        return any(p.startswith(prefix) for p in _tier_permissions_for(self.permission))


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

        # Token authority is fixed at issuance.  Admin-console downgrades take
        # effect live (the client.permission cap narrows effective tier);
        # admin-console upgrades do NOT widen previously-issued tokens.
        principal = OAuthServicePrincipal(permission=_effective_tier(access_token))

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
