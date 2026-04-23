"""Tests for the system @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from friese_mcp.contrib.coordination.tools.system import SystemDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request() -> MagicMock:
    """Return a bare mock request."""
    return MagicMock()


def _dispatch(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Invoke the SystemDispatcher directly."""
    dispatcher = SystemDispatcher()
    method = getattr(dispatcher, action)
    return method(_request(), params)


# ---------------------------------------------------------------------------
# echo
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSystemEcho:
    """Tests for the echo action."""

    def test_echo_returns_message(self) -> None:
        """Echo returns the message in the response."""
        result = _dispatch("echo", {"message": "hello"})
        assert result["echo"] == "hello"

    def test_echo_returns_params(self) -> None:
        """Echo returns all params in the response."""
        result = _dispatch("echo", {"message": "hi", "extra": "data"})
        assert result["params"]["extra"] == "data"

    def test_echo_empty_message(self) -> None:
        """Echo with no message returns empty string."""
        result = _dispatch("echo", {})
        assert result["echo"] == ""

    def test_echo_params_key_present(self) -> None:
        """Echo always includes the params key."""
        result = _dispatch("echo", {"x": 1})
        assert "params" in result


# ---------------------------------------------------------------------------
# role_list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSystemRoleList:
    """Tests for the role_list action."""

    def test_role_list_returns_list(self) -> None:
        """role_list returns a list of role strings."""
        result = _dispatch("role_list", {})
        assert isinstance(result["roles"], list)
        assert len(result["roles"]) > 0

    def test_role_list_contains_expected_defaults(self) -> None:
        """role_list includes known default roles."""
        result = _dispatch("role_list", {})
        assert "orchestrator" in result["roles"]

    def test_role_list_uses_settings_override(self) -> None:
        """role_list uses FRIESE_MCP_COORDINATION_ROLES when set."""
        custom = ["alpha", "beta"]
        with patch(
            "friese_mcp.contrib.coordination.tools.system.settings",
            FRIESE_MCP_COORDINATION_ROLES=custom,
        ):
            result = _dispatch("role_list", {})
        assert result["roles"] == custom


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSystemHelp:
    """Tests for the help action."""

    def test_help_returns_tools_list(self) -> None:
        """Help returns a list of tool descriptors."""
        result = _dispatch("help", {})
        assert isinstance(result["tools"], list)
        assert len(result["tools"]) > 0

    def test_help_tools_have_tool_key(self) -> None:
        """Each tool descriptor has a 'tool' key."""
        result = _dispatch("help", {})
        assert all("tool" in t for t in result["tools"])

    def test_help_tools_have_description_key(self) -> None:
        """Each tool descriptor has a 'description' key."""
        result = _dispatch("help", {})
        assert all("description" in t for t in result["tools"])

    def test_help_includes_rooms(self) -> None:
        """Help surface includes the rooms tool."""
        result = _dispatch("help", {})
        names = [t["tool"] for t in result["tools"]]
        assert "rooms" in names

    def test_help_includes_escalate_to_human(self) -> None:
        """Help surface includes the escalate_to_human tool."""
        result = _dispatch("help", {})
        names = [t["tool"] for t in result["tools"]]
        assert "escalate_to_human" in names
