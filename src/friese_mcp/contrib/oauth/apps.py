"""AppConfig for friese_mcp.contrib.oauth."""

from django.apps import AppConfig


class OAuthConfig(AppConfig):
    """Django app configuration for the contrib.oauth module."""

    name = "friese_mcp.contrib.oauth"
    label = "friese_mcp_oauth"
    verbose_name = "Friese MCP OAuth"
    default_auto_field = "django.db.models.BigAutoField"
