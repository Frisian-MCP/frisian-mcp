"""
Token-endpoint rate limiting for ``frisian_mcp.contrib.oauth``.

Extracted from ``views.py`` (V11-24) so that module stays under the 1200-line
cap; this is a self-contained concern (client-IP resolution + a fixed-window
counter in the Django cache) with a single caller, ``TokenView.post``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache as django_cache
from django.http import HttpRequest

_TOKEN_RL_PREFIX = "frisian_mcp:oauth_token_rl:"  # noqa: S105  # cache key prefix, not a password
_RATE_LIMIT_PERIODS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def _get_client_ip(request: HttpRequest) -> str:
    """
    Return the best-guess client IP address.

    Respects ``FRISIAN_MCP_TRUSTED_PROXY_COUNT``: when set, reads the
    ``X-Forwarded-For`` header and returns the entry just before the
    rightmost *N* proxy-added entries (which are attacker-injectable
    upstream of the trust boundary).  Falls back to ``REMOTE_ADDR`` when
    no proxy count is configured.
    """
    proxy_count: int = getattr(settings, "FRISIAN_MCP_TRUSTED_PROXY_COUNT", 0)
    if proxy_count > 0:
        xff = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
        if xff:
            parts = [p.strip() for p in xff.split(",")]
            # The rightmost proxy_count entries are set by trusted proxies;
            # the entry just before them is the real originating client.
            index = max(0, len(parts) - proxy_count)
            return parts[index]
    return str(request.META.get("REMOTE_ADDR", ""))


def _token_rate_limit_exceeded(request: HttpRequest) -> bool:
    """
    Return ``True`` when the token endpoint rate limit is exceeded for this IP.

    Reads ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` (format ``"N/period"``,
    e.g. ``"10/minute"``).  Supported periods: ``second``, ``minute``,
    ``hour``, ``day``.

    Returns ``False`` (not exceeded) when the setting is absent, ``None``,
    or malformed — fail-open to avoid breaking token issuance on cache
    failure or misconfiguration.

    **Deployment note:** enable this in production to mitigate brute-force
    and credential-stuffing against client secrets.  A value of
    ``"20/minute"`` is a reasonable starting point for most deployments;
    tighten based on observed legitimate traffic.  Nginx / load-balancer
    rate limiting is a complementary layer and does not replace this.
    """
    rate_limit: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT", None)
    if not rate_limit:
        return False
    try:
        count_str, period = rate_limit.split("/", 1)
        max_count = int(count_str.strip())
        period_seconds = _RATE_LIMIT_PERIODS[period.strip().lower()]
    except (ValueError, KeyError):
        return False  # Misconfigured — fail open

    ip = _get_client_ip(request)
    cache_key = f"{_TOKEN_RL_PREFIX}{ip}"
    try:
        # add() is a no-op when the key already exists — sets counter to 0
        # with TTL only on the first request in the window.
        django_cache.add(cache_key, 0, period_seconds)
        count = django_cache.incr(cache_key)
    except Exception:  # pylint: disable=broad-except  # cache backend unavailable
        return False  # Fail open — do not block token issuance on cache errors
    return count > max_count
