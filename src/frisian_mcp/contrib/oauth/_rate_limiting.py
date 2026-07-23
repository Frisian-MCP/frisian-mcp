"""
Token-endpoint rate limiting for ``frisian_mcp.contrib.oauth``.

Extracted from ``views.py`` (V11-24) so that module stays under the 1200-line
cap; this is a self-contained concern (client-IP resolution + a fixed-window
counter in the Django cache) with a single caller, ``TokenView.post``.

Fail-open posture (V11-26 ruling)
---------------------------------

The limiter fails OPEN — on a malformed setting or an unavailable cache it
stops limiting rather than stop issuing tokens.  Fail-closed would convert
every cache blip into a full authentication outage for every client, which is
a worse trade than briefly losing brute-force throttling on an endpoint that
should also sit behind load-balancer rate limiting.  What it must never do is
fail open *silently*: both fail-open causes emit a WARNING (once per process
per cause) and the malformed-setting case is additionally a boot-time system
check (``frisian_mcp.W013``), so an inert limiter is visible in CI, at boot,
and in logs — never discovered during an incident.

Trust boundary: ``FRISIAN_MCP_TRUSTED_PROXY_COUNT`` must equal the number of
trusted proxies that append to ``X-Forwarded-For``.  When it does, the selected
entry was written by the innermost trusted hop and attacker-prepended entries
shift harmlessly leftward; when it overstates the real proxy count, the
selected slot becomes attacker-controlled and NO static defense here can
recover — that is a deployment invariant, not a code path.  With the setting
unset the limiter keys on ``REMOTE_ADDR``, which is not spoofable over TCP.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache as django_cache
from django.http import HttpRequest

logger = logging.getLogger(__name__)

_TOKEN_RL_PREFIX = "frisian_mcp:oauth_token_rl:"  # noqa: S105  # cache key prefix, not a password
_RATE_LIMIT_PERIODS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}

#: Canonical log-event name emitted (once per process) when
#: ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` is set but unparseable — the limiter
#: is configured yet INERT.  Mirrored at boot by ``frisian_mcp.W013``.
OAUTH_TOKEN_RATE_LIMIT_MISCONFIGURED: str = (
    "oauth_token_rate_limit_misconfigured"  # noqa: S105  # log-event name, not a secret
)

#: Canonical log-event name emitted (once per process) when the cache backend
#: raised during the counter update — token issuance continues UNLIMITED until
#: the cache recovers.
OAUTH_TOKEN_RATE_LIMIT_CACHE_UNAVAILABLE: str = (
    "oauth_token_rate_limit_cache_unavailable"  # noqa: S105  # log-event name, not a secret
)

#: Once-per-process guard so a sustained cache outage warns once instead of
#: once per token request (same pattern as views._PKCE_TIER_SIGNAL_LOG_SEEN).
_LOGGED_FAIL_OPEN_EVENTS: set[str] = set()

#: Length cap for the IP-derived cache-key slot.  A legitimate address (IPv6
#: max 45 chars) never approaches it; only hostile header content in a
#: misconfigured deployment can.
_BUCKET_IP_CAP = 64


def parse_rate_limit(raw: object) -> tuple[int, int] | None:
    """
    Parse ``"N/period"`` into ``(max_count, period_seconds)``, or ``None``.

    Single source of truth for the setting's grammar: the runtime limiter and
    the ``frisian_mcp.W013`` system check both call this, so boot-time
    validation and request-time behavior cannot drift (the V11-19 lesson —
    two readers, one parser).

    A non-positive count (``0/minute``, ``-5/minute``) is rejected as
    malformed rather than accepted: the runtime gate is ``count > max_count``,
    so ``max_count <= 0`` would block the very first request and silently DoS
    all token issuance — the fail-CLOSED direction, contradicting the
    documented fail-open posture.  Routing it through ``None`` makes it fail
    open AND surfaces it via ``frisian_mcp.W013`` instead of a silent outage.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        count_str, period = raw.split("/", 1)
        max_count = int(count_str.strip())
        period_seconds = _RATE_LIMIT_PERIODS[period.strip().lower()]
    except (ValueError, KeyError):
        return None
    if max_count <= 0:
        return None
    return (max_count, period_seconds)


def _log_fail_open_once(event: str, detail: str) -> None:
    """WARN the first time each fail-open *event* fires in this process."""
    if event in _LOGGED_FAIL_OPEN_EVENTS:
        return
    _LOGGED_FAIL_OPEN_EVENTS.add(event)
    logger.warning("%s: %s", event, detail)


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


def _bucket_ip(request: HttpRequest) -> str:
    """
    Return the sanitized cache-key slot for the caller's rate bucket.

    A correctly-configured deployment only ever sees real addresses here, but
    a misconfigured ``TRUSTED_PROXY_COUNT`` can let attacker header text reach
    the key: strip non-printables (memcached rejects control characters in
    keys — a poisoned key would otherwise DISABLE limiting via the fail-open
    path) and cap the length.  Anything left empty collapses into one shared
    ``"invalid"`` bucket, so garbage-flooding self-throttles instead of
    minting a fresh bucket per request.
    """
    raw = _get_client_ip(request)
    cleaned = "".join(ch for ch in raw if ch.isprintable() and not ch.isspace())
    return cleaned[:_BUCKET_IP_CAP] or "invalid"


def _token_rate_limit_exceeded(request: HttpRequest) -> bool:
    """
    Return ``True`` when the token endpoint rate limit is exceeded for this IP.

    Reads ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` (format ``"N/period"``,
    e.g. ``"10/minute"``).  Supported periods: ``second``, ``minute``,
    ``hour``, ``day``.

    Fails open — see the module docstring for the posture and why every
    fail-open cause is loud rather than silent.

    **Deployment note:** enable this in production to mitigate brute-force
    and credential-stuffing against client secrets.  A value of
    ``"20/minute"`` is a reasonable starting point for most deployments;
    tighten based on observed legitimate traffic.  Nginx / load-balancer
    rate limiting is a complementary layer and does not replace this.
    """
    raw: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT", None)
    if not raw:
        return False
    parsed = parse_rate_limit(raw)
    if parsed is None:
        _log_fail_open_once(
            OAUTH_TOKEN_RATE_LIMIT_MISCONFIGURED,
            f"FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT={raw!r} is not 'N/period' "
            "(second/minute/hour/day) — the token-endpoint rate limiter is INERT. "
            "manage.py check reports this as frisian_mcp.W013.",
        )
        return False
    max_count, period_seconds = parsed

    cache_key = f"{_TOKEN_RL_PREFIX}{_bucket_ip(request)}"
    try:
        # add() is a no-op when the key already exists — sets counter to 0
        # with TTL only on the first request in the window.
        django_cache.add(cache_key, 0, period_seconds)
        count = django_cache.incr(cache_key)
    except Exception:  # pylint: disable=broad-except  # cache backend unavailable
        _log_fail_open_once(
            OAUTH_TOKEN_RATE_LIMIT_CACHE_UNAVAILABLE,
            "cache backend raised during the rate-limit counter update — token "
            "issuance continues UNLIMITED until the cache recovers.",
        )
        return False  # Fail open — do not block token issuance on cache errors
    return count > max_count
