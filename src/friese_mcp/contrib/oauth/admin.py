"""Django admin registration for OAuthClient and OAuthAccessToken."""

from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

from .models import OAuthAccessToken, OAuthClient


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~friese_mcp.contrib.oauth.models.OAuthClient`."""

    list_display = ("name", "is_active", "permission", "created_at")
    list_filter = ("is_active", "permission")
    search_fields = ("name", "client_id")
    readonly_fields = (
        "client_id", "client_secret", "created_at",
        "connector_auth_url", "connector_mcp_url",
    )
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "is_active", "permission"),
            },
        ),
        (
            "Credentials",
            {
                "fields": ("client_id", "client_secret"),
                "description": (
                    "The client_id is the public identifier. "
                    "The client_secret is shown once at creation — only the HMAC digest is stored."
                ),
            },
        ),
        (
            "Connector config",
            {
                "fields": ("connector_auth_url", "connector_mcp_url"),
                "description": (
                    "Distribute the MCP server URL to agent clients. "
                    "Use the authorization endpoint for the OAuth handshake only."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Authorization endpoint")
    def connector_auth_url(self, obj: OAuthClient) -> str:  # pylint: disable=unused-argument
        """OAuth 2.0 authorize URL — for the initial handshake only, not the MCP server URL."""
        issuer: str = getattr(settings, "FRIESE_MCP_OAUTH_ISSUER", "").rstrip("/")
        url = f"{issuer}/oauth/authorize/"
        return format_html("<code>{}</code>", url)

    @admin.display(description="MCP server URL")
    def connector_mcp_url(self, obj: OAuthClient) -> str:  # pylint: disable=unused-argument
        """MCP endpoint — this is the URL to enter in Claude.ai / agent connector settings."""
        issuer: str = getattr(settings, "FRIESE_MCP_OAUTH_ISSUER", "").rstrip("/")
        mcp_path: str = getattr(settings, "FRIESE_MCP_PATH", "/mcp/")
        url = f"{issuer}{mcp_path}"
        return format_html("<code>{}</code>", url)


@admin.register(OAuthAccessToken)
class OAuthAccessTokenAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~friese_mcp.contrib.oauth.models.OAuthAccessToken`."""

    list_display = ("client", "permission", "expires_at", "created_at")
    list_filter = ("client__is_active", "permission")
    search_fields = ("client__name",)
    readonly_fields = ("token", "client", "expires_at", "permission", "created_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("client", "permission", "expires_at"),
            },
        ),
        (
            "Token",
            {
                "fields": ("token",),
                "description": "The raw Bearer token secret.",
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )
