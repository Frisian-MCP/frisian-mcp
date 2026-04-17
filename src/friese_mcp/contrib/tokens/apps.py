"""AppConfig for friese_mcp.contrib.tokens."""

from django.apps import AppConfig


class TokensConfig(AppConfig):
    """Django app configuration for the contrib.tokens module."""

    name = "friese_mcp.contrib.tokens"
    label = "friese_mcp_tokens"
    verbose_name = "Friese MCP Tokens"
    default_auto_field = "django.db.models.BigAutoField"
