"""Django admin registration for OAuthClient and OAuthAccessToken."""

from django.contrib import admin

from .models import OAuthAccessToken, OAuthClient


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~friese_mcp.contrib.oauth.models.OAuthClient`."""

    list_display = ("name", "is_active", "scope", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "client_id")
    readonly_fields = ("client_id", "client_secret", "created_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "is_active", "scope"),
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
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(OAuthAccessToken)
class OAuthAccessTokenAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~friese_mcp.contrib.oauth.models.OAuthAccessToken`."""

    list_display = ("client", "scope", "expires_at", "created_at")
    list_filter = ("client__is_active", "scope")
    search_fields = ("client__name",)
    readonly_fields = ("token", "client", "expires_at", "scope", "created_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("client", "scope", "expires_at"),
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
