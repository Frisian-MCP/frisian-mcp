"""Tests for frisian_mcp.contrib.agents — AgentConnection model, admin, and per-agent filtering."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from frisian_mcp.contrib.agents.models import AgentConnection, validate_tool_name_list
from frisian_mcp.contrib.oauth.models import OAuthClient
from frisian_mcp.contrib.tokens.models import FrisianMcpToken
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

_view = McpView.as_view()

_TOKEN_AUTH = "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication"
_OAUTH_AUTH = "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_mcp(rf: RequestFactory, payload: Any, bearer: str | None = None) -> Any:
    """Build a POST request to the MCP endpoint with an optional Bearer token."""
    kwargs: dict[str, Any] = {"content_type": "application/json"}
    if bearer is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return rf.post("/mcp/", data=json.dumps(payload), **kwargs)


def _isolated_registry(*tool_names: str) -> ToolRegistry:
    """Return a fresh ToolRegistry with one no-op tool per name."""
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(name, lambda a, r: {"ok": True}, f"Tool {name}", {})
    return reg


def _use_token_auth(settings: Any) -> None:
    """Configure FrisianMcpTokenAuthentication with no gateway permission check."""
    settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [_TOKEN_AUTH]
    settings.FRISIAN_MCP_PERMISSION_CLASSES = []


def _use_oauth_auth(settings: Any) -> None:
    """Configure OAuthTokenAuthentication with no gateway permission check."""
    settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [_OAUTH_AUTH]
    settings.FRISIAN_MCP_PERMISSION_CLASSES = []


# ---------------------------------------------------------------------------
# AgentConnection model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAgentConnectionModel:
    """Unit tests for the AgentConnection model."""

    def test_str_active_generic(self) -> None:
        """__str__ includes name and 'active' for an active generic connection."""
        conn = AgentConnection(name="my-agent", agent_type="generic", is_active=True)
        assert "my-agent" in str(conn)
        assert "active" in str(conn)

    def test_str_inactive(self) -> None:
        """__str__ includes 'inactive' for a deactivated connection."""
        conn = AgentConnection(name="old-agent", is_active=False)
        assert "inactive" in str(conn)

    def test_str_shows_agent_type_display(self) -> None:
        """__str__ uses the verbose agent_type display value."""
        conn = AgentConnection(name="x", agent_type="claude-code", is_active=True)
        assert "Claude Code" in str(conn)

    def test_allowed_tools_null_by_default(self) -> None:
        """allowed_tools defaults to None (unrestricted)."""
        conn = AgentConnection.objects.create(name="unrestricted")
        assert conn.allowed_tools is None

    def test_allowed_tools_json_list(self) -> None:
        """allowed_tools stores and retrieves a JSON list of tool names."""
        conn = AgentConnection.objects.create(
            name="restricted",
            allowed_tools=["users.list", "workouts.create"],
        )
        conn.refresh_from_db()
        assert conn.allowed_tools == ["users.list", "workouts.create"]

    def test_token_fk(self) -> None:
        """AgentConnection can be linked to a FrisianMcpToken."""
        token = FrisianMcpToken.objects.create(name="claude-token")
        conn = AgentConnection.objects.create(name="claude", token=token)
        conn.refresh_from_db()
        assert conn.token == token

    def test_oauth_client_fk(self) -> None:
        """AgentConnection can be linked to an OAuthClient."""
        client = OAuthClient.objects.create(name="gpt-client")
        conn = AgentConnection.objects.create(name="gpt", oauth_client=client)
        conn.refresh_from_db()
        assert conn.oauth_client == client

    def test_token_deleted_nullifies_fk(self) -> None:
        """Deleting a FrisianMcpToken sets AgentConnection.token to NULL (SET_NULL)."""
        token = FrisianMcpToken.objects.create(name="temp-token")
        conn = AgentConnection.objects.create(name="agent", token=token)
        token.delete()
        conn.refresh_from_db()
        assert conn.token is None

    def test_oauth_client_deleted_nullifies_fk(self) -> None:
        """Deleting an OAuthClient sets AgentConnection.oauth_client to NULL (SET_NULL)."""
        client = OAuthClient.objects.create(name="temp-client")
        conn = AgentConnection.objects.create(name="agent", oauth_client=client)
        client.delete()
        conn.refresh_from_db()
        assert conn.oauth_client is None

    def test_last_seen_at_null_by_default(self) -> None:
        """last_seen_at starts as None."""
        conn = AgentConnection.objects.create(name="new-agent")
        assert conn.last_seen_at is None

    def test_ordering_newest_first(self) -> None:
        """Connections are ordered by most recently created first."""
        AgentConnection.objects.create(name="first")
        AgentConnection.objects.create(name="second")
        names = list(AgentConnection.objects.values_list("name", flat=True))
        assert names[0] == "second"

    def test_clean_raises_when_both_credentials_set(self) -> None:
        """clean() raises ValidationError when both token and oauth_client are set."""
        from django.core.exceptions import ValidationError as DjangoValidationError  # noqa: PLC0415

        token = FrisianMcpToken.objects.create(name="dual-tok")
        client = OAuthClient.objects.create(name="dual-client")
        conn = AgentConnection(name="dual", token=token, oauth_client=client)
        with pytest.raises(DjangoValidationError, match="but not both"):
            conn.clean()

    def test_clean_passes_with_only_token(self) -> None:
        """clean() raises no exception when only token is set."""
        token = FrisianMcpToken.objects.create(name="tok-only")
        conn = AgentConnection(name="tok-agent", token=token)
        conn.clean()  # must not raise

    def test_clean_passes_with_only_oauth_client(self) -> None:
        """clean() raises no exception when only oauth_client is set."""
        client = OAuthClient.objects.create(name="oauth-only")
        conn = AgentConnection(name="oauth-agent", oauth_client=client)
        conn.clean()  # must not raise

    def test_clean_passes_with_no_credential(self) -> None:
        """clean() raises no exception when neither credential is set (profile-only)."""
        conn = AgentConnection(name="profile-only")
        conn.clean()  # must not raise

    def test_get_agent_connection_token_select_related_no_extra_query(
        self, django_assert_num_queries: Any
    ) -> None:
        """_get_agent_connection() via token uses select_related — FK attrs need no extra query."""
        from unittest.mock import MagicMock  # pylint: disable=import-outside-toplevel

        from frisian_mcp.views import (
            _get_agent_connection,  # pylint: disable=import-outside-toplevel
        )

        token = FrisianMcpToken.objects.create(name="sr-tok")
        AgentConnection.objects.create(name="sr-agent", token=token)
        req = MagicMock()
        req.auth = token
        conn = _get_agent_connection(req)
        assert conn is not None
        with django_assert_num_queries(0):
            _ = conn.token
            _ = conn.oauth_client


# ---------------------------------------------------------------------------
# validate_tool_name_list validator
# ---------------------------------------------------------------------------


class TestValidateToolNameList:
    """Unit tests for the validate_tool_name_list standalone validator."""

    def test_valid_list_passes(self) -> None:
        """A list of non-empty strings raises no exception."""
        validate_tool_name_list(["users.list", "workouts.create"])

    def test_empty_list_validates_but_filters_to_no_tools(self) -> None:
        """An empty list is a valid value but means zero tools are allowed (not 'unrestricted')."""
        validate_tool_name_list([])

    def test_dict_raises(self) -> None:
        """A dict raises ValidationError — not a list."""
        with pytest.raises(ValidationError, match="JSON array"):
            validate_tool_name_list({"users.list": True})

    def test_integer_raises(self) -> None:
        """An integer raises ValidationError — not a list."""
        with pytest.raises(ValidationError):
            validate_tool_name_list(42)

    def test_non_string_element_raises(self) -> None:
        """A list containing a non-string element raises ValidationError."""
        with pytest.raises(ValidationError, match=r"\[0\]"):
            validate_tool_name_list([123])

    def test_empty_string_element_raises(self) -> None:
        """A list containing an empty string raises ValidationError."""
        with pytest.raises(ValidationError, match=r"\[0\]"):
            validate_tool_name_list([""])

    def test_whitespace_only_string_raises(self) -> None:
        """A list containing a whitespace-only string raises ValidationError."""
        with pytest.raises(ValidationError, match=r"\[0\]"):
            validate_tool_name_list(["   "])

    def test_mixed_valid_then_invalid_raises(self) -> None:
        """Validation fails on the first invalid element, not silently skipped."""
        with pytest.raises(ValidationError, match=r"\[1\]"):
            validate_tool_name_list(["valid.tool", ""])


@pytest.mark.django_db
class TestAgentConnectionAllowedToolsValidation:
    """AgentConnection.full_clean() rejects non-list allowed_tools values."""

    def test_full_clean_accepts_null(self) -> None:
        """full_clean() passes when allowed_tools is None."""
        conn = AgentConnection(name="agent")
        conn.full_clean(exclude=["token", "oauth_client"])

    def test_full_clean_accepts_valid_list(self) -> None:
        """full_clean() passes for a valid list of tool names."""
        conn = AgentConnection(name="agent", allowed_tools=["users.list"])
        conn.full_clean(exclude=["token", "oauth_client"])

    def test_full_clean_rejects_dict(self) -> None:
        """full_clean() raises ValidationError when allowed_tools is a dict."""
        conn = AgentConnection(name="agent", allowed_tools={"users.list": True})
        with pytest.raises(ValidationError):
            conn.full_clean(exclude=["token", "oauth_client"])

    def test_full_clean_rejects_non_string_element(self) -> None:
        """full_clean() raises ValidationError when an element is not a string."""
        conn = AgentConnection(name="agent", allowed_tools=[42])
        with pytest.raises(ValidationError):
            conn.full_clean(exclude=["token", "oauth_client"])


# ---------------------------------------------------------------------------
# AgentConnectionAdmin
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAgentConnectionAdmin:
    """Unit tests for AgentConnectionAdmin helper methods."""

    def _admin(self) -> Any:
        from django.contrib.admin import site  # pylint: disable=import-outside-toplevel

        from frisian_mcp.contrib.agents.admin import (  # pylint: disable=import-outside-toplevel
            AgentConnectionAdmin,
        )

        return AgentConnectionAdmin(AgentConnection, site)

    def test_credential_summary_no_credential(self) -> None:
        """credential_summary returns '—' when neither token nor oauth_client is set."""
        conn = AgentConnection(name="bare")
        assert self._admin().credential_summary(conn) == "—"

    def test_credential_summary_with_token(self) -> None:
        """credential_summary shows the token label when token is linked."""
        token = FrisianMcpToken.objects.create(name="my-token")
        conn = AgentConnection.objects.create(name="agent", token=token)
        summary = self._admin().credential_summary(conn)
        assert "Token" in summary
        assert "my-token" in summary

    def test_credential_summary_with_oauth_client(self) -> None:
        """credential_summary shows the OAuth client label when oauth_client is linked."""
        client = OAuthClient.objects.create(name="my-client")
        conn = AgentConnection.objects.create(name="agent", oauth_client=client)
        summary = self._admin().credential_summary(conn)
        assert "OAuth" in summary
        assert "my-client" in summary

    def test_deactivate_action_updates_db(self) -> None:
        """deactivate_agents action marks all selected connections as inactive."""
        conn1 = AgentConnection.objects.create(name="a", is_active=True)
        conn2 = AgentConnection.objects.create(name="b", is_active=True)
        admin_instance = self._admin()
        qs = AgentConnection.objects.filter(pk__in=[conn1.pk, conn2.pk])
        # Use a MagicMock so message_user can run without a real request/session.
        admin_instance.deactivate_agents(MagicMock(), qs)
        conn1.refresh_from_db()
        conn2.refresh_from_db()
        assert not conn1.is_active
        assert not conn2.is_active


# ---------------------------------------------------------------------------
# Per-agent tools/list filtering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPerAgentToolsList:
    """tools/list response is filtered by AgentConnection.allowed_tools."""

    def _call_tools_list(
        self,
        rf: RequestFactory,
        registry: ToolRegistry,
        bearer: str | None = None,
    ) -> list[dict[str, Any]]:
        """POST tools/list, return the tools array from the JSON-RPC result."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        request = _post_mcp(rf, payload, bearer=bearer)
        with patch("frisian_mcp.views.tool_registry", registry):
            response = _view(request)
        data = json.loads(response.content)
        return data["result"]["tools"]

    def test_no_connection_returns_all_tools(self, rf: RequestFactory) -> None:
        """No agent connection → all registered tools returned."""
        reg = _isolated_registry("users.list", "users.create")
        tools = self._call_tools_list(rf, reg)
        names = {t["name"] for t in tools}
        assert names == {"users.list", "users.create"}

    def test_null_allowed_tools_returns_all(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """AgentConnection.allowed_tools = None → all tools returned (unrestricted)."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="unrestricted", token=token, allowed_tools=None
        )
        reg = _isolated_registry("users.list", "users.create")
        tools = self._call_tools_list(rf, reg, bearer=token.plaintext_token)
        names = {t["name"] for t in tools}
        assert names == {"users.list", "users.create"}

    def test_allowed_tools_filters_list(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """AgentConnection.allowed_tools = [...] → only those tools returned."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="restricted",
            token=token,
            allowed_tools=["users.list"],
        )
        reg = _isolated_registry("users.list", "users.create", "workouts.list")
        tools = self._call_tools_list(rf, reg, bearer=token.plaintext_token)
        names = {t["name"] for t in tools}
        assert names == {"users.list"}

    def test_inactive_only_connection_fails_closed(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        SEC-5: when every linked AgentConnection is inactive, tools/list is empty.

        Pre-fix, an inactive AgentConnection silently dropped filtering and
        the credential continued to see every registered tool.  Operators
        who deactivate an agent expect access to stop, so the gateway now
        fails closed: the credential is treated as not currently authorised
        and tools/list returns no tools.
        """
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="disabled",
            token=token,
            is_active=False,
            allowed_tools=["users.list"],
        )
        reg = _isolated_registry("users.list", "users.create")
        tools = self._call_tools_list(rf, reg, bearer=token.plaintext_token)
        # Fail-closed: the credential sees nothing because the only
        # AgentConnection bound to it is inactive.
        assert tools == []

    def test_active_connection_still_filters_when_inactive_sibling_exists(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        An active AgentConnection wins over an inactive sibling on the same credential.

        Confirms the SEC-5 fail-closed gate ONLY fires when every linked
        connection is inactive — an active sibling re-opens the credential
        with its own allowed_tools applied.
        """
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="legacy-disabled",
            token=token,
            is_active=False,
            allowed_tools=["users.create"],
        )
        AgentConnection.objects.create(
            name="current-active",
            token=token,
            is_active=True,
            allowed_tools=["users.list"],
        )
        reg = _isolated_registry("users.list", "users.create")
        tools = self._call_tools_list(rf, reg, bearer=token.plaintext_token)
        names = {t["name"] for t in tools}
        # The active connection's allowed_tools applies; inactive sibling ignored.
        assert names == {"users.list"}

    def test_oauth_auth_filters_list(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Per-agent filtering works when request.auth is an OAuthAccessToken."""
        from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
            OAuthAccessToken,
        )

        _use_oauth_auth(settings)
        client = OAuthClient.objects.create(name="gpt-client")
        access_token = OAuthAccessToken.objects.create(client=client)
        AgentConnection.objects.create(
            name="gpt-agent",
            oauth_client=client,
            allowed_tools=["workouts.list"],
        )
        reg = _isolated_registry("users.list", "workouts.list")
        # SEC-1: send the raw plaintext_token; storage now holds the HMAC digest.
        tools = self._call_tools_list(rf, reg, bearer=access_token.plaintext_token)
        names = {t["name"] for t in tools}
        assert names == {"workouts.list"}


# ---------------------------------------------------------------------------
# Per-agent tools/call blocking
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPerAgentToolsCall:
    """tools/call is blocked for tools outside AgentConnection.allowed_tools."""

    def _call_tool(
        self,
        rf: RequestFactory,
        registry: ToolRegistry,
        tool_name: str,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        """POST tools/call, return the parsed JSON-RPC response."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        }
        request = _post_mcp(rf, payload, bearer=bearer)
        with patch("frisian_mcp.views.tool_registry", registry):
            response = _view(request)
        return json.loads(response.content)

    def test_tool_blocked_when_not_in_allowlist(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Calling a tool not in allowed_tools returns isError=True."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="restricted", token=token, allowed_tools=["users.list"]
        )
        reg = _isolated_registry("users.list", "users.create")
        data = self._call_tool(rf, reg, "users.create", bearer=token.plaintext_token)
        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert "not permitted" in content["error"]

    def test_tool_allowed_when_in_allowlist(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Calling a tool in allowed_tools succeeds normally."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="restricted", token=token, allowed_tools=["users.list"]
        )
        reg = _isolated_registry("users.list", "users.create")
        data = self._call_tool(rf, reg, "users.list", bearer=token.plaintext_token)
        assert data["result"]["isError"] is False

    def test_no_connection_allows_all_calls(self, rf: RequestFactory) -> None:
        """Without an AgentConnection, all tools are callable."""
        reg = _isolated_registry("users.list", "users.create")
        data = self._call_tool(rf, reg, "users.create")
        assert data["result"]["isError"] is False

    def test_null_allowed_tools_allows_all_calls(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """allowed_tools=None means no restriction; all tools are callable."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="unrestricted", token=token, allowed_tools=None
        )
        reg = _isolated_registry("users.list", "users.create")
        data = self._call_tool(rf, reg, "users.create", bearer=token.plaintext_token)
        assert data["result"]["isError"] is False

    def test_inactive_only_connection_fails_closed_on_call(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        SEC-5: tools/call returns isError=True when every linked AgentConnection is inactive.

        Mirrors the tools/list fail-closed behaviour at the call layer:
        deactivating the agent must stop the credential from invoking
        tools, not just hide them in the listing.
        """
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="agent-token")
        AgentConnection.objects.create(
            name="disabled",
            token=token,
            is_active=False,
            allowed_tools=["users.list"],
        )
        reg = _isolated_registry("users.list", "users.create")
        # Even calling a tool the inactive connection would have allowed
        # must fail closed.
        data = self._call_tool(rf, reg, "users.list", bearer=token.plaintext_token)
        assert data["result"]["isError"] is True
        content = json.loads(data["result"]["content"][0]["text"])
        assert "inactive" in content["error"].lower()


# ---------------------------------------------------------------------------
# last_seen_at
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLastSeenAt:
    """last_seen_at is stamped on tools/call for a matched AgentConnection."""

    def test_last_seen_at_updated_on_tools_call(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """AgentConnection.last_seen_at is set after a tools/call request."""
        _use_token_auth(settings)
        token = FrisianMcpToken.objects.create(name="tracking-token")
        conn = AgentConnection.objects.create(
            name="agent",
            token=token,
            allowed_tools=["users.list"],
        )
        assert conn.last_seen_at is None

        reg = _isolated_registry("users.list")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "users.list", "arguments": {}},
        }
        request = _post_mcp(rf, payload, bearer=token.plaintext_token)
        with patch("frisian_mcp.views.tool_registry", reg):
            _view(request)

        conn.refresh_from_db()
        assert conn.last_seen_at is not None

    def test_last_seen_at_not_set_without_connection(
        self, rf: RequestFactory
    ) -> None:
        """No AgentConnection → no last_seen_at side-effect (no DB rows to update)."""
        reg = _isolated_registry("users.list")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "users.list", "arguments": {}},
        }
        request = _post_mcp(rf, payload)
        with patch("frisian_mcp.views.tool_registry", reg):
            response = _view(request)
        # No exception means this path is safe even with no connection
        data = json.loads(response.content)
        assert "result" in data
