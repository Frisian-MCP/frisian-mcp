"""AppConfig for friese_mcp.contrib.tokens."""

import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class TokensConfig(AppConfig):
    """Django app configuration for the contrib.tokens module."""

    name = "friese_mcp.contrib.tokens"
    label = "friese_mcp_tokens"
    verbose_name = "Friese MCP Tokens"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Warn when FRIESE_MCP_HMAC_KEY is unset in production."""
        if not getattr(settings, "DEBUG", False) and not getattr(
            settings, "FRIESE_MCP_HMAC_KEY", ""
        ):
            logger.warning(
                "friese_mcp.contrib.tokens: FRIESE_MCP_HMAC_KEY is not set. "
                "Token HMAC digests will be keyed by Django's SECRET_KEY. "
                "Set FRIESE_MCP_HMAC_KEY to a dedicated secret so that rotating "
                "SECRET_KEY does not invalidate all issued tokens."
            )
