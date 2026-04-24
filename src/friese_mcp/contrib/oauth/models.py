"""
OAuthClient and OAuthAccessToken models — OAuth 2.0 client_credentials flow.

Usage::

    # In INSTALLED_APPS
    INSTALLED_APPS = [
        ...
        "friese_mcp.contrib.oauth",
    ]

    # In settings (optional — defaults shown)
    FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 3600  # 1 hour
    FRIESE_MCP_OAUTH_REGISTRATION_OPEN = False     # dynamic registration disabled

    # Then run: python manage.py migrate
    # Create clients in Django admin or (if registration is open) via /oauth/register/

"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone


def _default_expires_at() -> datetime:
    """Return the default expiry timestamp for a new access token."""
    expiry: int = getattr(settings, "FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS", 3600)
    return timezone.now() + timedelta(seconds=expiry)


class OAuthClient(models.Model):
    """
    Registered OAuth 2.0 client (AI agent or programmatic MCP consumer).

    ``client_id`` and ``client_secret`` are auto-generated on first save using
    :func:`secrets.token_hex`.  Both are stored in plaintext and must be treated
    as secrets by the host application.

    Clients are created via Django admin or, when
    ``FRIESE_MCP_OAUTH_REGISTRATION_OPEN`` is ``True``, via the
    ``/oauth/register/`` endpoint (RFC 7591 dynamic registration).
    """

    client_id = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
        help_text="Auto-generated OAuth client identifier.",
    )
    client_secret = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="Auto-generated OAuth client secret.  Treat as a password.",
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable label for this client (e.g. 'claude-agent', 'gpt-mcp-client').",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive clients cannot obtain new tokens; existing tokens are rejected.",
    )
    scope = models.CharField(
        max_length=200,
        default="mcp",
        help_text="Space-separated list of permitted scopes for this client.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "OAuth Client"
        verbose_name_plural = "OAuth Clients"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return a human-readable representation."""
        state = "active" if self.is_active else "inactive"
        return f"{self.name} ({state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate ``client_id`` and ``client_secret`` on first save."""
        if not self.client_id:
            self.client_id = secrets.token_hex(16)  # 32 hex chars
        if not self.client_secret:
            self.client_secret = secrets.token_hex(32)  # 64 hex chars
        super().save(*args, **kwargs)


class OAuthAccessToken(models.Model):
    """
    Short-lived OAuth 2.0 Bearer token issued via the ``client_credentials`` grant.

    The ``token`` field is auto-generated on first save.  Tokens expire after
    ``FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS`` seconds (default 3600).  Expired
    tokens are rejected by
    :class:`~friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication`.

    The host is responsible for purging expired tokens periodically (e.g. via a
    management command or scheduled task).
    """

    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="Auto-generated Bearer token secret.  Treat as a password.",
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
    scope = models.CharField(
        max_length=200,
        default="mcp",
        help_text="Space-separated list of scopes granted to this token.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "OAuth Access Token"
        verbose_name_plural = "OAuth Access Tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token", "expires_at"], name="friese_mcp_oat_expires_idx"),
        ]

    def __str__(self) -> str:
        """Return a human-readable representation."""
        return (
            f"{self.client.name} — {self.token[:8]}... (expires {self.expires_at:%Y-%m-%d %H:%M})"
        )

    def is_expired(self) -> bool:
        """Return ``True`` if this token has passed its expiry time."""
        return timezone.now() >= self.expires_at

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate ``token`` value on first save."""
        if not self.token:
            self.token = secrets.token_hex(32)  # 64 hex chars
        super().save(*args, **kwargs)
