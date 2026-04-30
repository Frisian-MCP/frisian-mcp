"""Tests for the mcp_doctor management command."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.test import override_settings

from friese_mcp.management.commands.mcp_doctor import Command


def _run(**kwargs: Any) -> tuple[str, str]:
    """
    Run mcp_doctor and return (stdout, stderr).

    Calls handle() directly to avoid Django's INSTALLED_APPS-based command
    discovery (which fails when INSTALLED_APPS is empty) and to avoid the
    BaseCommand.execute() options dict requirement.
    """
    out, err = StringIO(), StringIO()
    cmd = Command(stdout=out, stderr=err)
    cmd.handle()
    return out.getvalue(), err.getvalue()


class TestMcpDoctorInstalledApps:
    """INSTALLED_APPS checks."""

    @override_settings(INSTALLED_APPS=["friese_mcp"])
    def test_ok_when_friese_mcp_present(self) -> None:
        """No error emitted when friese_mcp is in INSTALLED_APPS."""
        out, _ = _run()
        assert "friese_mcp in INSTALLED_APPS" in out

    @override_settings(INSTALLED_APPS=[])
    def test_error_when_friese_mcp_missing(self) -> None:
        """An error is emitted and exit 1 is raised when friese_mcp is absent."""
        with pytest.raises(SystemExit) as exc_info:
            _run()
        assert exc_info.value.code == 1

    @override_settings(
        INSTALLED_APPS=[
            "friese_mcp",
            "friese_mcp.contrib.tokens",
            "friese_mcp.contrib.oauth",
            "friese_mcp.contrib.agents",
        ]
    )
    def test_ok_for_all_contrib_apps(self) -> None:
        """All three contrib apps are reported as present."""
        out, _ = _run()
        assert "contrib.tokens in INSTALLED_APPS" in out
        assert "contrib.oauth in INSTALLED_APPS" in out
        assert "contrib.agents in INSTALLED_APPS" in out

    @override_settings(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "friese_mcp",
            "friese_mcp.contrib.agents",
        ]
    )
    def test_error_agents_without_tokens(self) -> None:
        """Error emitted when contrib.agents is present without contrib.tokens."""
        with pytest.raises(SystemExit) as exc_info:
            _run()
        assert exc_info.value.code == 1


class TestMcpDoctorSecurity:
    """Security setting checks."""

    @override_settings(FRIESE_MCP_HMAC_KEY="test-dedicated-key")
    def test_ok_when_hmac_key_set(self) -> None:
        """No HMAC warning when FRIESE_MCP_HMAC_KEY is set."""
        out, _ = _run()
        assert "FRIESE_MCP_HMAC_KEY set" in out

    def test_warn_when_hmac_key_missing(self) -> None:
        """Warning emitted when FRIESE_MCP_HMAC_KEY is unset."""
        out, _ = _run()
        assert "FRIESE_MCP_HMAC_KEY not set" in out

    @override_settings(DEBUG=True)
    def test_warn_when_debug_true(self) -> None:
        """Warning emitted when DEBUG=True."""
        out, _ = _run()
        assert "DEBUG=True" in out

    @override_settings(DEBUG=False)
    def test_ok_when_debug_false(self) -> None:
        """OK message when DEBUG=False."""
        out, _ = _run()
        assert "DEBUG=False" in out


class TestMcpDoctorOAuth:
    """OAuth-specific checks."""

    @override_settings(
        INSTALLED_APPS=["friese_mcp", "friese_mcp.contrib.tokens", "friese_mcp.contrib.oauth"],
        FRIESE_MCP_OAUTH_REGISTRATION_OPEN=True,
    )
    def test_ok_when_registration_open(self) -> None:
        """OK message when FRIESE_MCP_OAUTH_REGISTRATION_OPEN=True."""
        out, _ = _run()
        assert "FRIESE_MCP_OAUTH_REGISTRATION_OPEN=True" in out

    @override_settings(
        INSTALLED_APPS=["friese_mcp", "friese_mcp.contrib.tokens", "friese_mcp.contrib.oauth"],
        FRIESE_MCP_OAUTH_REGISTRATION_OPEN=False,
    )
    def test_warn_when_registration_closed(self) -> None:
        """Warning emitted when FRIESE_MCP_OAUTH_REGISTRATION_OPEN=False."""
        out, _ = _run()
        assert "FRIESE_MCP_OAUTH_REGISTRATION_OPEN=False" in out


class TestMcpDoctorExitCodes:
    """Exit code behaviour."""

    @override_settings(INSTALLED_APPS=[])
    def test_exits_nonzero_on_error(self) -> None:
        """SystemExit(1) is raised when there are errors."""
        with pytest.raises(SystemExit) as exc_info:
            _run()
        assert exc_info.value.code == 1

    def test_exits_zero_on_warnings_only(self) -> None:
        """No SystemExit (implicit 0) when there are only warnings, no errors."""
        out, _ = _run()
        assert "No errors." in out


class TestMcpDoctorUnauthTier:
    """FRIESE_MCP_UNAUTHENTICATED_TIER checks."""

    def test_ok_when_not_set(self) -> None:
        """When FRIESE_MCP_UNAUTHENTICATED_TIER is not configured, default 'read' is explicit."""
        out, _ = _run()
        assert "not set — defaulting to 'read'" in out

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="read")
    def test_ok_when_explicitly_read(self) -> None:
        """Explicit read tier reports as OK."""
        out, _ = _run()
        assert "FRIESE_MCP_UNAUTHENTICATED_TIER='read'" in out

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="read_write")
    def test_warn_when_read_write(self) -> None:
        """Warning emitted when FRIESE_MCP_UNAUTHENTICATED_TIER=read_write."""
        out, _ = _run()
        assert "FRIESE_MCP_UNAUTHENTICATED_TIER='read_write'" in out

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="admin")
    def test_warn_when_admin(self) -> None:
        """Warning emitted when FRIESE_MCP_UNAUTHENTICATED_TIER=admin."""
        out, _ = _run()
        assert "FRIESE_MCP_UNAUTHENTICATED_TIER='admin'" in out

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="superuser")
    def test_warn_when_unrecognised(self) -> None:
        """Warning emitted when FRIESE_MCP_UNAUTHENTICATED_TIER is an unknown value."""
        out, _ = _run()
        assert "is not a recognised tier" in out


class TestMcpDoctorOAuthAuthorizeUrl:
    """FRIESE_MCP_OAUTH_AUTHORIZE_URL reachability checks."""

    def test_skipped_when_not_set(self) -> None:
        """No output about authorize URL when FRIESE_MCP_OAUTH_AUTHORIZE_URL is not set."""
        out, _ = _run()
        assert "FRIESE_MCP_OAUTH_AUTHORIZE_URL" not in out

    @override_settings(FRIESE_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
    def test_ok_when_reachable_200(self) -> None:
        """OK message when authorize URL returns HTTP 200."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            out, _ = _run()
        assert "FRIESE_MCP_OAUTH_AUTHORIZE_URL reachable (HTTP 200)" in out

    @override_settings(FRIESE_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
    def test_warn_when_http_error(self) -> None:
        """Warning emitted when authorize URL returns a non-200 HTTP status."""
        import urllib.error
        from unittest.mock import patch

        exc = urllib.error.HTTPError(
            url="http://localhost:9999/oauth/authorize/",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            out, _ = _run()
        assert "returned HTTP 404" in out

    @override_settings(FRIESE_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
    def test_warn_when_unreachable(self) -> None:
        """Warning emitted when authorize URL cannot be reached (network error)."""
        import urllib.error
        from unittest.mock import patch

        exc = urllib.error.URLError(reason="Connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            out, _ = _run()
        assert "could not be reached" in out
