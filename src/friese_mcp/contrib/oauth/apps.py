"""AppConfig for friese_mcp.contrib.oauth."""

import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class OAuthConfig(AppConfig):
    """Django app configuration for the contrib.oauth module."""

    name = "friese_mcp.contrib.oauth"
    label = "friese_mcp_oauth"
    verbose_name = "Friese MCP OAuth"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Warn when FRIESE_MCP_OAUTH_ISSUER is unset in production."""
        if not getattr(settings, "DEBUG", False) and not getattr(
            settings, "FRIESE_MCP_OAUTH_ISSUER", ""
        ):
            logger.warning(
                "friese_mcp.contrib.oauth: FRIESE_MCP_OAUTH_ISSUER is not set. "
                "OAuth metadata endpoints will use request.build_absolute_uri(), "
                "which may return an internal hostname behind a reverse proxy. "
                "Set FRIESE_MCP_OAUTH_ISSUER to your public base URL (e.g. "
                "'https://api.example.com') or set FRIESE_MCP_TRUSTED_PROXY_COUNT "
                "to the number of trusted proxies in front of this server."
            )
