"""
V11-16 — RFC 9728 protected-resource metadata must be route-aware.

The defect: the advertised ``resource`` was derived from ``FRISIAN_MCP_PATH``,
which is **not mounted** when ``FRISIAN_MCP_ROUTES`` is set.  So the metadata
could name a URL that maps to no route — or, the real hazard, to the *open*
``default`` route, telling an OAuth-discovering client that the unauthenticated
door is the protected resource.

The load-bearing test in this module is
``test_advertised_resource_is_never_anonymously_reachable``.  It is written as a
property over every route the server will advertise, not as an assertion about
one hard-coded path, so it keeps holding when the route set changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import RequestFactory
from rest_framework.permissions import IsAuthenticated

from frisian_mcp.contrib.oauth.views import OAuthProtectedResourceView
from frisian_mcp.route_config import parse_route_configs
from frisian_mcp.route_resources import (
    challenge_metadata_url,
    default_protected_resource,
    protected_resources,
    resource_for_path,
)
from frisian_mcp.route_views import route_is_anonymous_reachable

_view = OAuthProtectedResourceView.as_view()

# The live Nautobot posture: an open read-only door, plus two authenticated ones.
THREE_DOORS: dict[str, Any] = {
    "default": {"path": "openread", "highest_tier": "read"},
    "elevated": {"path": "scopedwrite", "highest_tier": "read_write"},
    "admin": {"path": "fulladmin", "highest_tier": "admin"},
}


def _metadata(rf: RequestFactory, suffix: str | None = None) -> tuple[int, dict[str, Any]]:
    """GET the protected-resource endpoint, bare or with a resource suffix."""
    url = "/.well-known/oauth-protected-resource"
    if suffix is None:
        response = _view(rf.get(url))
    else:
        response = _view(rf.get(f"{url}/{suffix}"), resource=suffix)
    body = json.loads(response.content) if response.content else {}
    return response.status_code, body


@pytest.mark.django_db
class TestNeverAdvertiseTheOpenDoor:
    """Criterion 1 — the advertised resource must require authentication."""

    def test_advertised_resource_is_never_anonymously_reachable(self, settings: Any) -> None:
        """
        THE security property, stated over the whole advertised set.

        Every resource this server will name in OAuth metadata must be a route an
        anonymous caller cannot reach.  Asserted against the same predicate the
        startup audit's E004 uses, so the two can never disagree about what
        "authenticated" means.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        routes = parse_route_configs(THREE_DOORS)

        advertised = protected_resources()
        assert advertised, "expected at least one protected resource"

        for resource in advertised:
            assert not route_is_anonymous_reachable(routes[resource.name]), (
                f"route {resource.name!r} is advertised as an OAuth-protected "
                f"resource but an anonymous caller can reach it"
            )

    def test_open_default_route_is_not_a_protected_resource(self, settings: Any) -> None:
        """The open door is excluded from the advertised set entirely."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        assert "default" not in {r.name for r in protected_resources()}

    def test_asking_for_the_open_door_by_name_is_a_404(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        Naming the open route in the metadata URL yields absence, not a document.

        The pre-V11-16 view returned the same (wrong) document for every suffix,
        so a client asking about the public door was told it was protected.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        status, body = _metadata(rf, "openread")
        assert status == 404
        assert body == {"error": "not_found"}

    def test_unknown_and_open_suffixes_are_indistinguishable(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """A prober cannot tell "no such route" from "that route is open"."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        open_door = _metadata(rf, "openread")
        nonexistent = _metadata(rf, "no-such-route")
        assert open_door == nonexistent == (404, {"error": "not_found"})


@pytest.mark.django_db
class TestPerResourceMetadata:
    """Criterion 2 — the client is told about the door it is standing at."""

    def test_suffix_resolves_to_that_route(self, rf: RequestFactory, settings: Any) -> None:
        """/…/fulladmin describes fulladmin — not some other route."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        status, body = _metadata(rf, "fulladmin")
        assert status == 200
        assert body["resource"].endswith("/fulladmin")

    def test_each_authenticated_route_gets_its_own_document(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """The two authenticated doors advertise *different* resources."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        _, elevated = _metadata(rf, "scopedwrite")
        _, admin = _metadata(rf, "fulladmin")
        assert elevated["resource"] != admin["resource"]

    def test_scopes_reflect_the_route_ceiling(self, rf: RequestFactory, settings: Any) -> None:
        """
        A read_write door must not advertise mcp:admin.

        The old code advertised a static ``["mcp:read", "mcp:write", "mcp:admin"]``
        on every route regardless of its tier ceiling.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        _, elevated = _metadata(rf, "scopedwrite")
        _, admin = _metadata(rf, "fulladmin")

        assert elevated["scopes_supported"] == ["mcp:read", "mcp:write"]
        assert "mcp:admin" not in elevated["scopes_supported"]
        assert "mcp:admin" in admin["scopes_supported"]

    def test_challenge_points_at_the_route_that_issued_it(self, settings: Any) -> None:
        """
        The 401's ``resource_metadata`` must name the route being challenged.

        Without this the per-resource documents are unreachable: a client only
        fetches the URL we hand it, and the old challenge hard-coded the bare
        endpoint on every route.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        url = challenge_metadata_url("https://host", "/fulladmin/")
        assert url == "https://host/.well-known/oauth-protected-resource/fulladmin"

    def test_challenge_from_an_unrelated_path_falls_back_to_bare(self, settings: Any) -> None:
        """A 401 from a host view (not a route) keeps the server-wide endpoint."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        url = challenge_metadata_url("https://host", "/some/host/view")
        assert url == "https://host/.well-known/oauth-protected-resource"


@pytest.mark.django_db
class TestDeterministicSelection:
    """Criterion 2 — no ambiguity about which resource the bare endpoint names."""

    def test_bare_endpoint_selects_lowest_privilege_authenticated_route(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        A client that will not say which door it wants gets the least dangerous one.

        Steering an unspecific client at ``admin`` would have it mint
        admin-audience tokens by default — the same failure shape as the
        ``PKCE_DEFAULT_PERMISSION="read_write"`` default that leaked writes.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        status, body = _metadata(rf)
        assert status == 200
        assert body["resource"].endswith("/scopedwrite")
        assert not body["resource"].endswith("/fulladmin")

    def test_selection_does_not_depend_on_config_order(self, settings: Any) -> None:
        """Reversing the mapping order must not change the answer."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        forward = default_protected_resource()

        settings.FRISIAN_MCP_ROUTES = dict(reversed(list(THREE_DOORS.items())))
        reverse = default_protected_resource()

        assert forward is not None and reverse is not None
        assert forward.path == reverse.path == "scopedwrite"

    def test_no_authenticated_route_means_no_protected_resource(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        An all-open, edge-gated posture advertises nothing rather than lying.

        There is no protected resource here.  Naming one anyway is precisely the
        defect V11-16 exists to close.
        """
        settings.FRISIAN_MCP_ROUTES = {"default": {"path": "openread", "highest_tier": "read"}}
        assert default_protected_resource() is None

        status, body = _metadata(rf)
        assert status == 404
        assert body == {"error": "not_found"}


@pytest.mark.django_db
class TestGlobalPermissionClassesInteraction:
    """BLOCKER-1 rule 1 — a global permission class gates every route, including default."""

    def test_global_auth_makes_the_default_route_a_protected_resource(self, settings: Any) -> None:
        """
        With a global ``IsAuthenticated``, ``default`` is no longer the open door.

        It then *is* a legitimate protected resource, and — being the
        lowest-privilege one — becomes what the bare endpoint advertises.  The
        rule that governs this is the same one E004/E006 read, so discovery and
        the audit cannot disagree.
        """
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAuthenticated]

        names = {r.name for r in protected_resources()}
        assert names == {"default", "elevated", "admin"}

        chosen = default_protected_resource()
        assert chosen is not None and chosen.path == "openread"


@pytest.mark.django_db
class TestLegacyHostsAreUnchanged:
    """Criterion 3 — a host with no FRISIAN_MCP_ROUTES sees byte-identical metadata."""

    def test_legacy_host_advertises_byte_identical_resource(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        The legacy resource URL is unchanged **byte for byte**, trailing slash included.

        ``resource`` is an audience identifier and is compared literally.  An
        earlier draft of V11-16 canonicalised ``/mcp/`` to ``/mcp``, which reads
        as tidying but silently re-audiences every deployed single-door host.
        This asserts the exact string the pre-V11-16 code emitted, so any future
        "cleanup" of the trailing slash fails here instead of in production.
        """
        settings.FRISIAN_MCP_ROUTES = {}
        settings.FRISIAN_MCP_PATH = "/mcp/"

        status, body = _metadata(rf)
        assert status == 200
        assert body["resource"] == "http://testserver/mcp/"
        assert body["scopes_supported"] == ["mcp:read", "mcp:write", "mcp:admin"]

    def test_legacy_protected_path_override_still_wins(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """FRISIAN_MCP_PROTECTED_PATH keeps precedence, and keeps its exact spelling."""
        settings.FRISIAN_MCP_ROUTES = {}
        settings.FRISIAN_MCP_PATH = "/mcp/"
        settings.FRISIAN_MCP_PROTECTED_PATH = "/gateway/"

        _, body = _metadata(rf)
        assert body["resource"] == "http://testserver/gateway/"

    def test_discovery_off_still_404s_with_routes_configured(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """PUBLIC_DISCOVERY=False hides per-resource metadata too."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        settings.FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False

        assert _metadata(rf) == (404, {"error": "not_found"})
        assert _metadata(rf, "fulladmin") == (404, {"error": "not_found"})


@pytest.mark.django_db
class TestResourceLookup:
    """resource_for_path canonicalises the many spellings of one path."""

    @pytest.mark.parametrize(
        "spelling", ["scopedwrite", "/scopedwrite", "/scopedwrite/", "//scopedwrite//"]
    )
    def test_path_spellings_canonicalise(self, settings: Any, spelling: str) -> None:
        """Request paths and metadata suffixes reach the same route."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        resource = resource_for_path(spelling)
        assert resource is not None and resource.name == "elevated"

    def test_empty_path_is_not_a_resource(self, settings: Any) -> None:
        """A root/empty path must not match a route (it would swallow everything)."""
        settings.FRISIAN_MCP_ROUTES = THREE_DOORS
        assert resource_for_path("/") is None
        assert resource_for_path("") is None
