"""AppConfig for frisian_mcp.contrib.oauth."""

import logging

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class OAuthConfig(AppConfig):
    """Django app configuration for the contrib.oauth module."""

    name = "frisian_mcp.contrib.oauth"
    label = "frisian_mcp_oauth"
    verbose_name = "Frisian MCP OAuth"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Validate proxy config and warn when FRISIAN_MCP_OAUTH_ISSUER is unset in production."""
        proxy_count = getattr(settings, "FRISIAN_MCP_TRUSTED_PROXY_COUNT", 0)
        if not isinstance(proxy_count, int) or isinstance(proxy_count, bool):
            raise ImproperlyConfigured(
                "FRISIAN_MCP_TRUSTED_PROXY_COUNT must be a non-negative integer, "
                f"got {type(proxy_count).__name__!r}: {proxy_count!r}"
            )
        if proxy_count < 0:
            raise ImproperlyConfigured(
                f"FRISIAN_MCP_TRUSTED_PROXY_COUNT must be >= 0, got {proxy_count!r}"
            )

        if not getattr(settings, "DEBUG", False) and not getattr(
            settings, "FRISIAN_MCP_OAUTH_ISSUER", ""
        ):
            logger.warning(
                "frisian_mcp.contrib.oauth: FRISIAN_MCP_OAUTH_ISSUER is not set. "
                "OAuth metadata endpoints will use request.build_absolute_uri(), "
                "which may return an internal hostname behind a reverse proxy. "
                "Set FRISIAN_MCP_OAUTH_ISSUER to your public base URL (e.g. "
                "'https://api.example.com') or set FRISIAN_MCP_TRUSTED_PROXY_COUNT "
                "to the number of trusted proxies in front of this server."
            )

        if not getattr(settings, "DEBUG", False) and not getattr(
            settings, "FRISIAN_MCP_HMAC_KEY", ""
        ):
            logger.warning(
                "frisian_mcp.contrib.oauth: FRISIAN_MCP_HMAC_KEY is not set. "
                "Client secret HMAC digests will be keyed by Django's SECRET_KEY. "
                "Set FRISIAN_MCP_HMAC_KEY to a dedicated secret so that rotating "
                "SECRET_KEY does not invalidate all registered OAuth clients."
            )

        _locmem = "django.core.cache.backends.locmem.LocMemCache"
        cache_backend = getattr(settings, "CACHES", {}).get("default", {}).get("BACKEND", "")
        if not getattr(settings, "DEBUG", False) and cache_backend == _locmem:
            logger.warning(
                "frisian_mcp.contrib.oauth: CACHES['default'] is LocMemCache. "
                "Authorization codes are stored in the per-process cache. "
                "In a multi-worker production deployment (gunicorn, uWSGI) an "
                "authorization code written by one worker will not be visible to "
                "another, causing intermittent invalid_grant errors on token "
                "exchange. Configure a shared cache backend (Redis, Memcached) "
                "before deploying with multiple workers."
            )
