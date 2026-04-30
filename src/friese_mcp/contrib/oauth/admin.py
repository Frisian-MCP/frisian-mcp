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
        "client_id", "client_secret", "created_at", "connector_mcp_url", "connector_client_id"
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
                    "The client_id and client_secret are auto-generated and shown "
                    "after creation.  Distribute them to your MCP agent client."
                ),
            },
        ),
        (
            "Connector config",
            {
                "fields": ("connector_mcp_url", "connector_client_id"),
                "description": (
                    "Copy these values into your MCP client's Advanced settings. "
                    "Use client_secret from the Credentials section as the Client Secret."
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

    @admin.display(description="Client ID")
    def connector_client_id(self, obj: OAuthClient) -> str:
        """Return client_id in <code> tags for copy-paste into connector config."""
        return format_html("<code>{}</code>", obj.client_id)

    @admin.display(description="MCP Server URL")
    def connector_mcp_url(self, obj: OAuthClient) -> str:  # pylint: disable=unused-argument
        """Return the MCP server base URL for copy-paste into connector config."""
        issuer: str = getattr(settings, "FRIESE_MCP_OAUTH_ISSUER", "").rstrip("/")
        url = f"{issuer}/mcp/"
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
