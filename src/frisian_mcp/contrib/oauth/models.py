"""
OAuthClient and OAuthAccessToken models — OAuth 2.0 client_credentials flow.

Usage::

    # In INSTALLED_APPS
    INSTALLED_APPS = [
        ...
        "frisian_mcp.contrib.oauth",
    ]

    # In settings (optional — defaults shown)
    FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 3600  # 1 hour
    FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False     # dynamic registration disabled

    # Then run: python manage.py migrate
    # Create clients in Django admin or (if registration is open) via /oauth/register/

"""

from __future__ import annotations

import hashlib
import hmac as _hmac_lib
import secrets
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

_PERMISSION_CHOICES = [
    ("read", "Read Only"),
    ("read_write", "Read Write"),
    ("admin", "Admin"),
]

_SCOPE_MAP: dict[str, str] = {
    "read": "mcp:read",
    "read_write": "mcp:read mcp:write",
    "admin": "mcp:read mcp:write mcp:admin",
}


def _hmac_secret(raw: str) -> str:
    """Return HMAC-SHA256 of *raw* keyed by FRISIAN_MCP_HMAC_KEY (or SECRET_KEY) as hex."""
    hmac_key: str = getattr(settings, "FRISIAN_MCP_HMAC_KEY", "") or settings.SECRET_KEY
    return _hmac_lib.new(hmac_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _default_expires_at() -> datetime:
    """Return the default expiry timestamp for a new access token."""
    expiry: int = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)
    return timezone.now() + timedelta(seconds=expiry)


class OAuthClient(models.Model):
    """
    Registered OAuth 2.0 client (AI agent or programmatic MCP consumer).

    ``client_id`` is auto-generated on first save using :func:`secrets.token_hex`
    and stored as a plain string (it is the public identifier).  ``client_secret``
    is auto-generated, stored as an **HMAC-SHA256 digest** keyed by
    ``FRISIAN_MCP_HMAC_KEY`` (or ``SECRET_KEY``), and must be treated as a secret
    by the host application.  The raw secret value is exposed exactly once via
    ``plaintext_client_secret`` on the freshly-saved instance and is never
    persisted.

    Clients are created via Django admin or, when
    ``FRISIAN_MCP_OAUTH_REGISTRATION_OPEN`` is ``True``, via the
    ``/oauth/register/`` endpoint (RFC 7591 dynamic registration).
    """

    client_id = models.CharField(
        max_length=255,
        unique=True,
        editable=False,
        help_text=(
            "OAuth client identifier — auto-generated (32 hex chars) or supplied by the client."
        ),
    )
    client_secret = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="HMAC-SHA256 of the raw client secret keyed by SECRET_KEY.  Never the raw value.",
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable label for this client (e.g. 'claude-agent', 'gpt-mcp-client').",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive clients cannot obtain new tokens; existing tokens are rejected.",
    )
    permission = models.CharField(
        max_length=10,
        choices=_PERMISSION_CHOICES,
        default="read_write",
        help_text=(
            "Controls which tier of tools tokens issued to this client can access: "
            "Read Only, Read Write, or Admin."
        ),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text=(
            "Django user whose permissions govern tool visibility for this client "
            "when FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY is enabled. "
            "Leave blank to use the global FRISIAN_MCP_OAUTH_SERVICE_USER setting."
        ),
    )
    redirect_uris = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Registered OAuth 2.0 redirect URIs (RFC 6749 §3.1.2).  The "
            "authorize endpoint requires an exact-match against this list "
            "before issuing an authorization code (SEC-2).  An empty list "
            "disables the authorize endpoint for this client; populate it via "
            "Django admin, RFC 7591 dynamic registration, or set "
            "``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True`` to permit on-demand "
            "PKCE clients (HTTPS / loopback validation still applies)."
        ),
    )
    grant_types = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Allowed OAuth 2.0 grant types for this client (RFC 7591 §2).  "
            "An empty list means no restriction — all supported grant types "
            "(``client_credentials``, ``authorization_code``) are permitted.  "
            'Set to ``[\\"client_credentials\\"]`` for service-to-service clients '
            'that should never use the PKCE flow, or ``[\\"authorization_code\\"]`` '
            "for browser/native clients that should not use client_credentials."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "OAuth Client"
        verbose_name_plural = "OAuth Clients"
        ordering = ["-created_at"]

    @property
    def scope_string(self) -> str:
        """Return the RFC 6749 scope string for this client's permission tier."""
        return _SCOPE_MAP.get(self.permission, "mcp:read")

    def __str__(self) -> str:
        """Return a human-readable representation."""
        state = "active" if self.is_active else "inactive"
        return f"{self.name} ({state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate ``client_id`` and ``client_secret`` on first save."""
        if not self.client_id:
            self.client_id = secrets.token_hex(16)  # 32 hex chars — public identifier
        if not self.client_secret:
            raw = secrets.token_hex(32)
            self.plaintext_client_secret: str = (  # pylint: disable=attribute-defined-outside-init
                raw
            )
            self.client_secret = _hmac_secret(raw)
        super().save(*args, **kwargs)


class OAuthAccessToken(models.Model):
    """
    Short-lived OAuth 2.0 Bearer token issued via the ``client_credentials`` grant.

    The ``token`` field is auto-generated on first save.  Tokens expire after
    ``FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS`` seconds (default 3600).  Expired
    tokens are rejected by
    :class:`~frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication`.

    The host is responsible for purging expired tokens periodically (e.g. via a
    management command or scheduled task).
    """

    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text=(
            "HMAC-SHA256 of the raw Bearer token keyed by SECRET_KEY.  Never the "
            "raw value — the raw token is exposed exactly once via "
            "``plaintext_token`` on the freshly-saved instance and is never "
            "persisted, so a leaked DB row is not directly exploitable."
        ),
    )
    client = models.ForeignKey(
        OAuthClient,
        on_delete=models.CASCADE,
        related_name="access_tokens",
        help_text="OAuth client that obtained this token.",
    )
    expires_at = models.DateTimeField(
        default=_default_expires_at,
        help_text="Token expiry time.  Tokens past this time are rejected.",
    )
    permission = models.CharField(
        max_length=10,
        choices=_PERMISSION_CHOICES,
        default="read_write",
        help_text="Permission tier inherited from the issuing client at token creation time.",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent authenticated request using this token.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "OAuth Access Token"
        verbose_name_plural = "OAuth Access Tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token", "expires_at"], name="frisian_mcp_oat_expires_idx"),
        ]

    @property
    def scope_string(self) -> str:
        """Return the RFC 6749 scope string for this token's permission tier."""
        return _SCOPE_MAP.get(self.permission, "mcp:read")

    def __str__(self) -> str:
        """Return a human-readable representation."""
        masked = f"{self.token[:4]}****" if self.token else "****"
        return f"{self.client.name} — {masked} (expires {self.expires_at:%Y-%m-%d %H:%M})"

    def is_expired(self) -> bool:
        """Return ``True`` if this token has passed its expiry time."""
        return timezone.now() >= self.expires_at

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Auto-generate ``token`` on first save; expose raw value once via ``plaintext_token``.

        Mirrors the :class:`~frisian_mcp.contrib.tokens.models.FrisianMcpToken`
        and :class:`OAuthClient` HMAC pattern: a leaked ``OAuthAccessToken`` row
        cannot be replayed against the gateway because only the digest is
        stored.  Callers must read ``plaintext_token`` immediately after
        ``create()`` / ``save()`` to obtain the raw Bearer value to return to
        the client (see ``contrib.oauth.views.TokenView``).
        """
        if not self.token:
            raw = secrets.token_hex(32)
            self.plaintext_token: str = raw  # pylint: disable=attribute-defined-outside-init
            self.token = _hmac_secret(raw)
        super().save(*args, **kwargs)


class OAuthAuthorizeConsent(models.Model):
    """Persisted approval of an OAuth authorize request.

    Records that a Django *user* has approved a specific
    ``(client_id, redirect_uri, scope)`` tuple at least once.  Subsequent
    requests for the same tuple by the same user can be auto-approved without
    re-rendering the consent form when
    ``FRISIAN_MCP_OAUTH_AUTO_APPROVE`` is ``True``.

    Operators can pre-populate consent rows via Django admin to grant
    silent re-approval for trusted clients without requiring an interactive
    consent step.  Per-row delete is the revocation mechanism.

    Closes M-oauth-auto-approve-debug-default.  See ADR:
    "Unauthenticated PKCE authorize-path: request inputs are never
    authority."
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_authorize_consents",
        help_text="Django user who granted this consent.",
    )
    client_id = models.CharField(
        max_length=255,
        help_text="OAuth client_id from the authorize request at the time of consent.",
    )
    redirect_uri = models.CharField(
        max_length=2000,
        help_text="Exact redirect_uri the user approved.  Must match on subsequent requests.",
    )
    scope = models.CharField(
        max_length=64,
        help_text=(
            "Permission tier at the time of consent.  Currently one of "
            "``read`` / ``read_write`` / ``admin`` (the OAuthClient.permission "
            "value at consent time).  Stored as a free-form string so future "
            "scope refactors do not require a migration."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "OAuth Authorize Consent"
        verbose_name_plural = "OAuth Authorize Consents"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_id", "redirect_uri", "scope"],
                name="frisian_mcp_oac_unique_grant",
            ),
        ]

    def __str__(self) -> str:
        """Return a human-readable representation."""
        return f"{self.user} → {self.client_id} ({self.scope})"
