"""The synthetic inner request must carry the caller's real origin (OX-6).

``SyncInvocation._build_request`` used to hardcode
``META["SERVER_NAME"] = "localhost"``, discarding the caller's Host entirely.
Anything a host serializer builds with ``build_absolute_uri()`` -- DRF
hyperlinked fields, pagination ``next``/``previous``, ``Location`` headers --
was therefore wrong, and on a host with a strict ``ALLOWED_HOSTS`` every tool
call failed outright with ``DisallowedHost``.

A second, independent defect lived on the same lines: the synthetic request
was a bare ``HttpRequest``, whose ``_get_scheme()`` returns the literal
``"http"`` and never reads ``META``.  Every HTTPS deployment therefore served
protocol-downgraded absolute URLs.

Both are pinned here, separately.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import DisallowedHost
from django.test import RequestFactory, override_settings

from frisian_mcp.backends.invocation import SyncInvocation


def _outer(host: str, *, secure: bool = False, forwarded: str | None = None) -> Any:
    """Build an outer MCP gateway request the way a real deployment would."""
    request = RequestFactory().post("/mcp/", HTTP_HOST=host, secure=secure)
    if forwarded is not None:
        request.META["HTTP_X_FORWARDED_HOST"] = forwarded
    return request


def _inner(original: Any) -> Any:
    # pylint: disable=protected-access
    return SyncInvocation()._build_request("get", {}, {}, original)  # noqa: SLF001


class TestForwardedHost:
    """The caller's host reaches the URLs the host app generates."""

    @override_settings(ALLOWED_HOSTS=["example.test"])
    def test_absolute_url_carries_the_callers_host(self) -> None:
        """The plain case: no more ``localhost``."""
        inner = _inner(_outer("example.test"))
        assert inner.build_absolute_uri("/api/thing/1/") == "http://example.test/api/thing/1/"

    @override_settings(ALLOWED_HOSTS=["example.test"])
    def test_non_default_port_is_preserved(self) -> None:
        """The port must survive -- it is dropped by the naive fix.

        ``SERVER_PORT`` stays ``"80"``; ``get_host()`` already returns
        ``host:port``, and ``_get_raw_host`` only appends ``SERVER_PORT`` when
        it differs from the default.
        """
        inner = _inner(_outer("example.test:8083"))
        assert inner.build_absolute_uri("/api/thing/1/") == "http://example.test:8083/api/thing/1/"

    @override_settings(ALLOWED_HOSTS=["lms.example.test", "lms"])
    def test_strict_allowed_hosts_does_not_raise(self) -> None:
        """The hard-failure case: a host that does not permit ``localhost``.

        This is what made every tool call error on a strict deployment -- the
        synthetic request carried a Host the host app had never allowed.
        """
        inner = _inner(_outer("lms.example.test"))
        assert inner.get_host() == "lms.example.test"

    @override_settings(ALLOWED_HOSTS=["public.example.test"], USE_X_FORWARDED_HOST=True)
    def test_proxied_deployment_resolves_the_public_origin(self) -> None:
        """Behind a proxy the public origin wins, not the internal one.

        ``get_host()`` honours ``USE_X_FORWARDED_HOST`` by construction, and
        the resolved value is written into ``SERVER_NAME`` -- the LOWEST
        precedence slot in ``_get_raw_host`` -- so the synthetic request
        carries no forwarding header of its own that could re-poison it.
        """
        inner = _inner(_outer("internal-nginx:8000", forwarded="public.example.test"))
        assert inner.build_absolute_uri("/x/") == "http://public.example.test/x/"

    @override_settings(ALLOWED_HOSTS=["good.example.test"])
    def test_spoofed_host_is_rejected_where_allowed_hosts_is_a_real_list(self) -> None:
        """A spoofed Host must not reach generated URLs.

        Note the precondition in the name.  ``validate_host`` treats ``"*"`` as
        matching anything, so on ``ALLOWED_HOSTS = ["*"]`` this property does
        not exist for ANY fix shape and cannot be tested.  It is only
        meaningful where ``ALLOWED_HOSTS`` is a real list -- which is why this
        test sets one.
        """
        with pytest.raises(DisallowedHost):
            _inner(_outer("attacker.example.test"))


class TestForwardedScheme:
    """The caller's scheme reaches the URLs the host app generates."""

    @override_settings(ALLOWED_HOSTS=["secure.example.test"])
    def test_https_caller_gets_https_urls(self) -> None:
        """An HTTPS caller must not be handed ``http://`` links.

        An agent that follows a downgraded link sends its Bearer token in
        cleartext.
        """
        inner = _inner(_outer("secure.example.test", secure=True))
        assert inner.scheme == "https"
        assert inner.build_absolute_uri("/x/") == "https://secure.example.test/x/"

    @override_settings(ALLOWED_HOSTS=["plain.example.test"])
    def test_http_caller_still_gets_http_urls(self) -> None:
        """The scheme is forwarded, not forced -- a plain caller is unchanged."""
        inner = _inner(_outer("plain.example.test"))
        assert inner.scheme == "http"
        assert inner.build_absolute_uri("/x/") == "http://plain.example.test/x/"

    @override_settings(ALLOWED_HOSTS=["secure.example.test"])
    def test_https_on_a_non_default_port_keeps_that_port(self) -> None:
        """HTTPS plus an explicit port: the port survives and is not doubled."""
        inner = _inner(_outer("secure.example.test:8443", secure=True))
        assert inner.build_absolute_uri("/x/") == "https://secure.example.test:8443/x/"
