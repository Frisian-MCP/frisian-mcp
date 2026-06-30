"""
Redirect-URI allowlist helpers for the PKCE auto-register code path.

These helpers gate the unknown-client branch of the OAuth authorize
endpoint when ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`` is enabled.  A
request whose redirect URI does not match an entry on the
``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST`` setting is
rejected with ``invalid_client`` (not ``invalid_redirect_uri``) so the
response does not leak which check rejected the request.

Pattern syntax is intentionally narrow:

* Exact match: ``"claude.ai"`` matches ``"claude.ai"`` only.
* Leading-``*.`` wildcard: ``"*.anthropic.com"`` matches
  ``"api.anthropic.com"`` and ``"x.y.anthropic.com"`` (any non-empty
  left-hand label sequence), but never the bare apex ``"anthropic.com"``
  and never a suffix-substring attacker host like
  ``"anthropic.com.evil.example"``.
* Reverse-DNS custom-scheme native-app redirects (e.g.
  ``com.example.app:/cb``) match on the URI scheme; allowlist entries
  for that flow are the reverse-DNS string itself.

Both pattern and host are IDNA-normalized before comparison so a
Cyrillic look-alike cannot bypass an entry spelled in ASCII.
"""

from __future__ import annotations

from urllib.parse import urlparse

#: Canonical log-event name emitted when ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER``
#: is True but the host allowlist is empty/unset.  Tests and external log
#: consumers should import this symbol rather than hard-coding the string.
OAUTH_PKCE_AUTO_REGISTER_ALLOWLIST_EMPTY: str = "oauth_pkce_auto_register_allowlist_empty"

#: Canonical log-event name emitted when ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER``
#: is True, the host allowlist is non-empty, and the inbound redirect_uri host
#: does not match any allowlist entry.
OAUTH_PKCE_AUTO_REGISTER_HOST_REJECTED: str = "oauth_pkce_auto_register_host_rejected"


def _normalize_host(host: str) -> str:
    """
    Return the IDNA-normalized, lowercased form of *host*.

    Performs IDNA-2003 ``encode`` then decodes back to ASCII, lowercases,
    and strips a trailing dot.  A Cyrillic-look-alike host either fails
    at the encode step (returns ``""``) or is normalized to its
    ``xn--`` punycode form so a comparison against the ASCII spelling
    cannot accidentally match.

    Returns ``""`` on any normalization failure; callers treat that as
    "no match" rather than as an error.
    """
    if not host:
        return ""
    candidate = host.strip().rstrip(".").lower()
    if not candidate:
        return ""
    try:
        return candidate.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""


def match_host_pattern(pattern: str, host: str) -> bool:
    """
    Return True when the IDNA-normalized *host* matches *pattern*.

    See module docstring for full pattern syntax.  An empty pattern or
    host never matches.
    """
    if not pattern or not host:
        return False
    if pattern.startswith("*."):
        suffix_pat = _normalize_host(pattern[2:])
        if not suffix_pat:
            return False
        # The left side must be a non-empty label sequence followed by a
        # single dot and the normalized suffix.  Bare-suffix matches
        # (``host == suffix``) are rejected by the length check, which
        # also blocks a ``*.com`` pattern from matching ``com`` itself.
        suffix_tail = f".{suffix_pat}"
        return host.endswith(suffix_tail) and len(host) > len(suffix_tail)
    return host == _normalize_host(pattern)


def redirect_uri_matches_auto_register_allowlist(redirect_uri: str, allowlist: list[str]) -> bool:
    """
    Return True when *redirect_uri* is permitted under PKCE AUTO_REGISTER.

    Match rules:

    * ``http`` / ``https`` URIs match on the IDNA-normalized hostname.
    * Reverse-DNS custom-scheme URIs (``com.example.app:/cb``) match on
      the URI scheme (the allowlist entry is the reverse-DNS string).
    * An empty *allowlist* never matches — operators must explicitly opt
      in to a trusted set; AUTO_REGISTER without an allowlist behaves as
      if AUTO_REGISTER were disabled.

    Loopback hosts (``localhost``, ``127.0.0.1``, ``::1``) STILL need an
    explicit allowlist entry under AUTO_REGISTER; there is no implicit
    loopback bypass on the unknown-client path.

    The redirect URI is presumed to already pass the per-view
    scheme/loopback safety check; this helper only decides whether the
    *target* is on the auto-register trusted set.
    """
    if not allowlist:
        return False
    # Fail-closed on parse error.  ``urlparse`` is lenient on its own, but
    # ``parsed.hostname`` raises ``ValueError`` on malformed IPv6 inputs
    # such as ``http://[::g]/``.  Any parse failure here means the URI is
    # not safely matchable against the allowlist, so reject the request.
    try:
        parsed = urlparse(redirect_uri)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
    except ValueError:
        return False
    if scheme in {"http", "https"}:
        host = _normalize_host(hostname or "")
        if not host:
            return False
        return any(match_host_pattern(entry, host) for entry in allowlist)
    if "." in scheme:
        # Reverse-DNS native-app scheme.  Case-insensitive compare on the
        # bare scheme; allowlist entries are lowercased and trailing-dot-
        # stripped to share the same comparison primitive.
        scheme_key = scheme.rstrip(".")
        return any(scheme_key == entry.strip().lower().rstrip(".") for entry in allowlist if entry)
    return False
