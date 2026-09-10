"""Regression coverage for RFC 8707 route-bound OAuth tokens (issue #69)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from rest_framework.exceptions import AuthenticationFailed

from frisian_mcp.contrib.oauth.authentication import OAuthTokenAuthentication
from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthAuthorizeConsent, OAuthClient
from frisian_mcp.contrib.oauth.views import AuthorizeView, TokenView

_AUTHORIZE = AuthorizeView.as_view()
_TOKEN = TokenView.as_view()
_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_ROUTES: dict[str, Any] = {
    "elevated": {"path": "scopedwrite", "highest_tier": "read_write"},
    "admin": {"path": "fulladmin", "highest_tier": "admin"},
}
_BASE = "https://oauth.example.test"
_RESOURCE_A = f"{_BASE}/scopedwrite"
_RESOURCE_B = f"{_BASE}/fulladmin"


def _authorize_params(client: OAuthClient, resource: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": "https://client.example.test/callback",
        "code_challenge": _CHALLENGE,
        "code_challenge_method": "S256",
        "resource": resource,
    }


def _user() -> Any:
    return get_user_model().objects.create_user(username="resource-consent-user", password="x")


def _rendered_consent_context(response: Any) -> str:
    """Extract the signed context from a GET-rendered consent form."""
    match = re.search(
        rb'name="consent_context" value="([^"]+)"', response.content
    )
    assert match is not None
    return match.group(1).decode()


@pytest.mark.django_db
class TestRouteBoundOAuthTokens:
    """Issue #69: a code-flow token is valid only for its requested route."""

    @pytest.fixture(autouse=True)
    def _configure_routes(self, settings: Any) -> None:
        settings.FRISIAN_MCP_ROUTES = _ROUTES
        settings.FRISIAN_MCP_OAUTH_ISSUER = _BASE
        settings.FRISIAN_MCP_OAUTH_AUTO_APPROVE = True

    def _client(self) -> OAuthClient:
        return OAuthClient.objects.create(
            name="route-bound-client",
            permission="read_write",
            redirect_uris=["https://client.example.test/callback"],
        )

    def _request(self, rf: RequestFactory, path: str, token: str) -> Any:
        return rf.post(path, HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_resource_specific_consent_is_persisted_and_auto_approved_only_for_same_resource(
        self, rf: RequestFactory
    ) -> None:
        """A consent for one protected route neither covers nor overwrites another."""
        client = self._client()
        user = _user()

        rendered_request = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        rendered_request.user = user
        rendered = _AUTHORIZE(rendered_request)
        assert rendered.status_code == 200
        first = rf.post(
            "/oauth/authorize/",
            data={"consent_context": _rendered_consent_context(rendered), "allow": "true"},
        )
        first.user = user
        accepted = _AUTHORIZE(first)
        assert accepted.status_code == 302
        consent = OAuthAuthorizeConsent.objects.get(user=user)
        assert consent.resource == _RESOURCE_A

        same_resource = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        same_resource.user = user
        assert _AUTHORIZE(same_resource).status_code == 302

        other_resource = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_B))
        other_resource.user = user
        assert _AUTHORIZE(other_resource).status_code == 200
        assert OAuthAuthorizeConsent.objects.filter(user=user).count() == 1

    @pytest.mark.parametrize(
        ("field", "tampered_value"),
        [
            ("resource", _RESOURCE_B),
            ("client_id", "different-client"),
            ("redirect_uri", "https://attacker.example.test/callback"),
            ("code_challenge", "a" * 43),
            ("state", "attacker-state"),
        ],
    )
    def test_consent_post_rejects_every_browser_controlled_request_tuple_change(
        self, rf: RequestFactory, field: str, tampered_value: str
    ) -> None:
        """A rejected form mutation must not record consent or issue a code."""
        client = self._client()
        user = _user()
        params = _authorize_params(client, _RESOURCE_A)
        params["state"] = "original-state"
        rendered_request = rf.get("/oauth/authorize/", params)
        rendered_request.user = user
        rendered = _AUTHORIZE(rendered_request)

        tampered = rf.post(
            "/oauth/authorize/",
            data={
                "consent_context": _rendered_consent_context(rendered),
                "allow": "true",
                field: tampered_value,
            },
        )
        tampered.user = user
        rejected = _AUTHORIZE(tampered)

        assert rejected.status_code == 400
        assert json.loads(rejected.content)["error"] == "invalid_request"
        assert not rejected.has_header("Location")
        assert not OAuthAuthorizeConsent.objects.filter(user=user).exists()
        assert not OAuthAccessToken.objects.filter(client=client).exists()

    def test_consent_context_is_principal_bound_and_one_time(self, rf: RequestFactory) -> None:
        """A context cannot survive a user switch, auth transition, or replay."""
        client = self._client()
        owner = _user()
        other = get_user_model().objects.create_user(username="other-user", password="x")
        rendered_request = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        rendered_request.user = owner
        context = _rendered_consent_context(_AUTHORIZE(rendered_request))

        switched = rf.post("/oauth/authorize/", {"consent_context": context, "allow": "true"})
        switched.user = other
        assert _AUTHORIZE(switched).status_code == 400
        assert not OAuthAuthorizeConsent.objects.exists()

        # A context generated while anonymous is also invalid after login.
        anonymous_get = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        anonymous_context = _rendered_consent_context(_AUTHORIZE(anonymous_get))
        logged_in_post = rf.post(
            "/oauth/authorize/", {"consent_context": anonymous_context, "allow": "true"}
        )
        logged_in_post.user = owner
        assert _AUTHORIZE(logged_in_post).status_code == 400

        # Logging out between GET and POST is rejected too.
        authenticated_get = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        authenticated_get.user = owner
        authenticated_context = _rendered_consent_context(_AUTHORIZE(authenticated_get))
        logged_out_post = rf.post(
            "/oauth/authorize/", {"consent_context": authenticated_context, "allow": "true"}
        )
        assert _AUTHORIZE(logged_out_post).status_code == 400

        fresh_get = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        fresh_get.user = owner
        fresh_context = _rendered_consent_context(_AUTHORIZE(fresh_get))
        first = rf.post("/oauth/authorize/", {"consent_context": fresh_context, "allow": "true"})
        first.user = owner
        assert _AUTHORIZE(first).status_code == 302
        replay = rf.post("/oauth/authorize/", {"consent_context": fresh_context, "allow": "true"})
        replay.user = owner
        assert _AUTHORIZE(replay).status_code == 400
        assert OAuthAuthorizeConsent.objects.filter(user=owner).count() == 1

    def test_empty_resource_indicator_is_not_treated_as_legacy_token_request(
        self, rf: RequestFactory
    ) -> None:
        """An explicit empty RFC 8707 indicator must not downgrade the audience."""
        client = self._client()
        user = _user()
        params = _authorize_params(client, _RESOURCE_A)
        params["resource"] = ""
        request = rf.get("/oauth/authorize/", params)
        request.user = user
        response = _AUTHORIZE(request)
        assert response.status_code == 302
        assert parse_qs(urlparse(response["Location"]).query)["error"] == ["invalid_target"]

    def test_duplicate_resource_indicators_are_rejected(self, rf: RequestFactory) -> None:
        """Do not silently select one of multiple RFC 8707 resource values."""
        client = self._client()
        user = _user()
        params = _authorize_params(client, _RESOURCE_A)
        query = "&".join(f"{key}={value}" for key, value in params.items())
        request = rf.get(f"/oauth/authorize/?{query}&resource={_RESOURCE_B}")
        request.user = user
        response = _AUTHORIZE(request)
        assert response.status_code == 302
        assert parse_qs(urlparse(response["Location"]).query)["error"] == ["invalid_request"]

    def test_code_exchange_persists_resource_and_token_works_at_its_route(
        self, rf: RequestFactory
    ) -> None:
        """The resource survives authorize → code cache → token exchange in-route."""
        client = self._client()
        user = _user()
        rendered_request = rf.get("/oauth/authorize/", _authorize_params(client, _RESOURCE_A))
        rendered_request.user = user
        rendered = _AUTHORIZE(rendered_request)
        authorize = rf.post(
            "/oauth/authorize/",
            data={"consent_context": _rendered_consent_context(rendered), "allow": "true"},
        )
        authorize.user = user
        redirect = _AUTHORIZE(authorize)
        code = parse_qs(urlparse(redirect["Location"]).query)["code"][0]

        exchange = rf.post(
            "/oauth/token/",
            data={
                "grant_type": "authorization_code",
                "client_id": client.client_id,
                "redirect_uri": "https://client.example.test/callback",
                "code": code,
                "code_verifier": _VERIFIER,
            },
        )
        response = _TOKEN(exchange)
        assert response.status_code == 200
        raw_token = json.loads(response.content)["access_token"]
        access_token = OAuthAccessToken.objects.get(client=client)
        assert access_token.resource == _RESOURCE_A
        assert OAuthTokenAuthentication().authenticate(
            self._request(rf, "/scopedwrite/", raw_token)
        ) is not None

    def test_cross_route_token_is_401_with_token_resource_challenge(
        self, rf: RequestFactory
    ) -> None:
        """A token for route A is rejected at B and advertises B's metadata."""
        client = self._client()
        token = OAuthAccessToken.objects.create(client=client, resource=_RESOURCE_A)
        request = self._request(rf, "/fulladmin/", token.plaintext_token)

        with pytest.raises(AuthenticationFailed) as raised:
            OAuthTokenAuthentication().authenticate(request)

        assert raised.value.status_code == 401
        challenge = OAuthTokenAuthentication().authenticate_header(request)
        expected = f'{_BASE}/.well-known/oauth-protected-resource/fulladmin'
        assert f'resource_metadata="{expected}"' in challenge

    def test_route_bound_token_fails_closed_outside_protected_routes(
        self, rf: RequestFactory
    ) -> None:
        """A bound audience credential cannot authenticate an unrecognised path."""
        client = self._client()
        token = OAuthAccessToken.objects.create(client=client, resource=_RESOURCE_A)

        with pytest.raises(AuthenticationFailed):
            OAuthTokenAuthentication().authenticate(
                self._request(rf, "/other/", token.plaintext_token)
            )

    def test_existing_token_without_resource_remains_usable_on_other_routes(
        self, rf: RequestFactory
    ) -> None:
        """Pre-RFC-8707 null-resource tokens remain compatible with all routes."""
        client = self._client()
        legacy = OAuthAccessToken.objects.create(client=client, resource=None)
        assert OAuthTokenAuthentication().authenticate(
            self._request(rf, "/fulladmin/", legacy.plaintext_token)
        ) is not None
