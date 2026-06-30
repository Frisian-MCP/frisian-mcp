"""Tests for the mcp_doctor management command."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings

from frisian_mcp.management.commands.mcp_doctor import Command


def _run(**kwargs: Any) -> tuple[str, str]:
    """
    Run mcp_doctor and return (stdout, stderr).

    Calls handle() directly to avoid Django's INSTALLED_APPS-based command
    discovery (which fails when INSTALLED_APPS is empty) and to avoid the
    BaseCommand.execute() options dict requirement.  Extra kwargs (e.g.
    ``security=True``) are forwarded to handle() so tests can exercise the
    extended audit path.
    """
    out, err = StringIO(), StringIO()
    cmd = Command(stdout=out, stderr=err)
    cmd.handle(**kwargs)
    return out.getvalue(), err.getvalue()


class TestMcpDoctorInstalledApps:
    """INSTALLED_APPS checks."""

    @override_settings(INSTALLED_APPS=["frisian_mcp"])
    def test_ok_when_frisian_mcp_present(self) -> None:
        """No error emitted when frisian_mcp is in INSTALLED_APPS."""
        out, _ = _run()
        assert "frisian_mcp in INSTALLED_APPS" in out

    @override_settings(INSTALLED_APPS=[])
    def test_error_when_frisian_mcp_missing(self) -> None:
        """An error is emitted and exit 1 is raised when frisian_mcp is absent."""
        with pytest.raises(SystemExit) as exc_info:
            _run()
        assert exc_info.value.code == 1

    @override_settings(
        INSTALLED_APPS=[
            "frisian_mcp",
            "frisian_mcp.contrib.tokens",
            "frisian_mcp.contrib.oauth",
            "frisian_mcp.contrib.agents",
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
            "frisian_mcp",
            "frisian_mcp.contrib.agents",
        ]
    )
    def test_error_agents_without_tokens(self) -> None:
        """Error emitted when contrib.agents is present without contrib.tokens."""
        with pytest.raises(SystemExit) as exc_info:
            _run()
        assert exc_info.value.code == 1


class TestMcpDoctorSecurity:
    """Security setting checks."""

    @override_settings(FRISIAN_MCP_HMAC_KEY="test-dedicated-key")
    def test_ok_when_hmac_key_set(self) -> None:
        """No HMAC warning when FRISIAN_MCP_HMAC_KEY is set."""
        out, _ = _run()
        assert "FRISIAN_MCP_HMAC_KEY set" in out

    def test_warn_when_hmac_key_missing(self) -> None:
        """Warning emitted when FRISIAN_MCP_HMAC_KEY is unset."""
        out, _ = _run()
        assert "FRISIAN_MCP_HMAC_KEY not set" in out

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
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=True,
    )
    def test_ok_when_registration_open(self) -> None:
        """OK message when FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=True."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=True" in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False,
    )
    def test_warn_when_registration_closed(self) -> None:
        """Warning emitted when FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False" in out


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
    """FRISIAN_MCP_UNAUTHENTICATED_TIER checks."""

    def test_ok_when_not_set(self) -> None:
        """When FRISIAN_MCP_UNAUTHENTICATED_TIER is not configured, default 'read' is explicit."""
        out, _ = _run()
        assert "not set — defaulting to 'read'" in out

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="read")
    def test_ok_when_explicitly_read(self) -> None:
        """Explicit read tier reports as OK."""
        out, _ = _run()
        assert "FRISIAN_MCP_UNAUTHENTICATED_TIER='read'" in out

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="read_write")
    def test_warn_when_read_write(self) -> None:
        """Warning emitted when FRISIAN_MCP_UNAUTHENTICATED_TIER=read_write."""
        out, _ = _run()
        assert "FRISIAN_MCP_UNAUTHENTICATED_TIER='read_write'" in out

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="admin")
    def test_warn_when_admin(self) -> None:
        """Warning emitted when FRISIAN_MCP_UNAUTHENTICATED_TIER=admin."""
        out, _ = _run()
        assert "FRISIAN_MCP_UNAUTHENTICATED_TIER='admin'" in out

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="superuser")
    def test_warn_when_unrecognised(self) -> None:
        """Warning emitted when FRISIAN_MCP_UNAUTHENTICATED_TIER is an unknown value."""
        out, _ = _run()
        assert "is not a recognised tier" in out


class TestMcpDoctorOAuthAuthorizeUrl:
    """FRISIAN_MCP_OAUTH_AUTHORIZE_URL reachability checks."""

    def test_skipped_when_not_set(self) -> None:
        """No output about authorize URL when FRISIAN_MCP_OAUTH_AUTHORIZE_URL is not set."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_AUTHORIZE_URL" not in out

    @override_settings(FRISIAN_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
    def test_ok_when_reachable_200(self) -> None:
        """OK message when authorize URL returns HTTP 200."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_AUTHORIZE_URL reachable (HTTP 200)" in out

    @override_settings(FRISIAN_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
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

    @override_settings(FRISIAN_MCP_OAUTH_AUTHORIZE_URL="http://localhost:9999/oauth/authorize/")
    def test_warn_when_unreachable(self) -> None:
        """Warning emitted when authorize URL cannot be reached (network error)."""
        import urllib.error
        from unittest.mock import patch

        exc = urllib.error.URLError(reason="Connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            out, _ = _run()
        assert "could not be reached" in out


class TestMcpDoctorPkceAutoRegister:
    """T1: AUTO_REGISTER + host-allowlist matrix (extended security audit)."""

    @override_settings(FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=False)
    def test_ok_when_disabled(self) -> None:
        """AUTO_REGISTER=False reports OK."""
        out, _ = _run(security=True)
        assert "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=False" in out

    @override_settings(
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST=[],
        DEBUG=False,
    )
    def test_error_when_enabled_with_empty_allowlist_outside_debug(self) -> None:
        """AUTO_REGISTER=True + empty allowlist + DEBUG=False raises an error."""
        with pytest.raises(SystemExit) as exc_info:
            _run(security=True)
        assert exc_info.value.code == 1

    @override_settings(
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST=[],
        DEBUG=True,
    )
    def test_warn_when_enabled_with_empty_allowlist_under_debug(self) -> None:
        """AUTO_REGISTER=True + empty allowlist + DEBUG=True warns rather than errors."""
        out, _ = _run(security=True)
        assert "no host allowlist (DEBUG=True)" in out

    @override_settings(
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST=["claude.ai", "*.anthropic.com"],
        DEBUG=False,
    )
    def test_ok_when_enabled_with_allowlist_outside_debug(self) -> None:
        """AUTO_REGISTER=True + non-empty allowlist + DEBUG=False reports size, never contents."""
        out, _ = _run(security=True)
        assert "restricted to 2 host pattern(s)" in out
        # Allowlist values must never echo into the doctor output.
        assert "claude.ai" not in out
        assert "anthropic.com" not in out

    @override_settings(
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST=["claude.ai"],
        DEBUG=True,
    )
    def test_warn_when_enabled_with_allowlist_under_debug(self) -> None:
        """AUTO_REGISTER=True + allowlist + DEBUG=True warns operator to verify before prod."""
        out, _ = _run(security=True)
        assert "restricted to 1 host pattern(s)" in out
        assert "DEBUG=True" in out
        assert "claude.ai" not in out

    @override_settings(
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST="claude.ai",
        DEBUG=False,
    )
    def test_warn_when_allowlist_is_string_not_list(self) -> None:
        """Misconfigured-as-string allowlist is rejected (no false 'N pattern(s)' OK)."""
        # Without the isinstance guard, ``list("claude.ai")`` would silently
        # explode the string into 9 single-char "patterns" and the doctor
        # would report "restricted to 9 host pattern(s)" — falsely OK on a
        # malformed security setting.  The shape guard catches it, coerces
        # to empty, and the empty-allowlist + non-DEBUG matrix then ERRORs.
        out_buf, err_buf = StringIO(), StringIO()
        cmd = Command(stdout=out_buf, stderr=err_buf)
        with pytest.raises(SystemExit) as exc:
            cmd.handle(security=True)
        assert exc.value.code == 1
        out = out_buf.getvalue()
        assert "is not a list" in out
        # The exploded-into-chars fallback would emit this string; verify
        # the shape guard suppresses it.
        assert "restricted to 9 host pattern(s)" not in out


class TestMcpDoctorPkceRedirectTierMap:
    """T7: legacy PKCE_REDIRECT_TIER_MAP stale-setting warning."""

    def test_silent_when_setting_absent(self) -> None:
        """No mention of the removed setting when operators have removed it."""
        # Explicit precondition: if a future test_settings.py drift defines
        # the setting, surface a clear "precondition broken" failure rather
        # than letting the absent-case assertion pass / fail for the wrong
        # reason.  ``hasattr`` triggers the LazySettings ``_setup`` so the
        # check works whether or not the setting was defined at import time.
        assert not hasattr(settings, "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP"), (
            "Test precondition violated: FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP"
            " is defined in the test settings module. This test asserts the"
            " absent-case behavior; remove the setting from test_settings.py"
            " or override it for this test."
        )
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP" not in out

    @override_settings(FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP={"https://example.com/": "read"})
    def test_warn_when_legacy_setting_present(self) -> None:
        """Operator left the removed setting in settings.py → warn to clean up."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP is set" in out
        assert "no longer read" in out


class TestMcpDoctorAutoApprove:
    """T9: AUTO_APPROVE matrix + interaction with AUTO_REGISTER."""

    def test_ok_when_unset(self) -> None:
        """AUTO_APPROVE absent → OK."""
        # Explicit precondition: see TestMcpDoctorPkceRedirectTierMap for
        # the same hermeticity guard.
        assert not hasattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE"), (
            "Test precondition violated: FRISIAN_MCP_OAUTH_AUTO_APPROVE is"
            " defined in the test settings module. Remove it from"
            " test_settings.py or override it for this test."
        )
        out, _ = _run(security=True)
        assert "FRISIAN_MCP_OAUTH_AUTO_APPROVE unset or False" in out

    @override_settings(FRISIAN_MCP_OAUTH_AUTO_APPROVE=False)
    def test_ok_when_false(self) -> None:
        """AUTO_APPROVE=False → OK."""
        out, _ = _run(security=True)
        assert "FRISIAN_MCP_OAUTH_AUTO_APPROVE unset or False" in out

    @override_settings(FRISIAN_MCP_OAUTH_AUTO_APPROVE=True, DEBUG=True)
    def test_ok_when_true_in_debug(self) -> None:
        """AUTO_APPROVE=True under DEBUG is acceptable but called out."""
        out, _ = _run(security=True)
        assert "FRISIAN_MCP_OAUTH_AUTO_APPROVE=True (DEBUG=True)" in out

    @override_settings(FRISIAN_MCP_OAUTH_AUTO_APPROVE=True, DEBUG=False)
    def test_warn_when_true_outside_debug(self) -> None:
        """AUTO_APPROVE=True outside DEBUG warns operator to confirm consent posture."""
        out, _ = _run(security=True)
        assert "repeat-grant fast path active" in out

    @override_settings(
        FRISIAN_MCP_OAUTH_AUTO_APPROVE=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True,
        FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST=["claude.ai"],
        DEBUG=False,
    )
    def test_warn_when_combined_with_auto_register(self) -> None:
        """AUTO_APPROVE=True + AUTO_REGISTER=True raises an additional combined-warning."""
        out, _ = _run(security=True)
        assert "combined with" in out


class TestMcpDoctorTierPermissions:
    """T10: FRISIAN_MCP_OAUTH_TIER_PERMISSIONS audit."""

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"]
    )
    def test_ok_when_unset(self) -> None:
        """TIER_PERMISSIONS unset → default-deny is the safe default."""
        out, _ = _run()
        assert "TIER_PERMISSIONS unset or empty" in out
        assert "default-deny" in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        FRISIAN_MCP_OAUTH_TIER_PERMISSIONS={"read": ["app.view_thing"], "admin": ["app.add_thing"]},
    )
    def test_ok_with_size_when_populated(self) -> None:
        """Populated TIER_PERMISSIONS reports tier count, never perm strings."""
        out, _ = _run()
        assert "set for 2 tier(s)" in out
        # Perm strings must never leak into the doctor output.
        assert "app.view_thing" not in out
        assert "app.add_thing" not in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        FRISIAN_MCP_OAUTH_TIER_PERMISSIONS="not a dict",
    )
    def test_warn_when_misconfigured_type(self) -> None:
        """Non-dict TIER_PERMISSIONS warns operator about shape."""
        out, _ = _run()
        assert "is not a dict" in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        # Use a perm string that does NOT appear in the doctor's own warning
        # text (which uses ``app.view_thing`` as an illustrative example),
        # so the leak assertion isn't fooled by the help-text occurrence.
        FRISIAN_MCP_OAUTH_TIER_PERMISSIONS={"read": "secret.special_perm"},
    )
    def test_warn_when_value_is_string_not_list(self) -> None:
        """Per-tier value of str (instead of list[str]) is flagged."""
        out, _ = _run()
        assert "unexpected" in out
        # Perm strings must never leak into the doctor output.
        assert "secret.special_perm" not in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        FRISIAN_MCP_OAUTH_TIER_PERMISSIONS={"read": ["secret.special_perm", 123]},
    )
    def test_warn_when_perm_entry_is_non_string(self) -> None:
        """Non-string entries inside the per-tier list are flagged."""
        out, _ = _run()
        assert "unexpected" in out
        assert "secret.special_perm" not in out

    @override_settings(
        INSTALLED_APPS=["frisian_mcp", "frisian_mcp.contrib.tokens", "frisian_mcp.contrib.oauth"],
        # ``"redd"`` is a typo; no runtime tier will ever consult it.
        FRISIAN_MCP_OAUTH_TIER_PERMISSIONS={"redd": ["secret.special_perm"]},
    )
    def test_warn_when_tier_key_is_not_canonical(self) -> None:
        """A typo'd tier key (``redd``) is flagged, not silently OK'd."""
        out, _ = _run()
        assert "unexpected" in out

    @override_settings(INSTALLED_APPS=["frisian_mcp"])
    def test_silent_when_oauth_not_installed(self) -> None:
        """No TIER_PERMISSIONS signal when contrib.oauth is absent."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS" not in out


@pytest.mark.django_db
class TestMcpDoctorAutoApproveConsentRecords:
    """T9: AUTO_APPROVE=True with no OAuthAuthorizeConsent rows → operator drift warning."""

    @override_settings(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "frisian_mcp",
            "frisian_mcp.contrib.tokens",
            "frisian_mcp.contrib.oauth",
        ],
        FRISIAN_MCP_OAUTH_AUTO_APPROVE=True,
    )
    def test_warn_when_auto_approve_and_no_consent_rows(self) -> None:
        """AUTO_APPROVE=True with empty consent table → drift warning."""
        out, _ = _run(security=True)
        assert "no OAuthAuthorizeConsent rows exist" in out

    @override_settings(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "frisian_mcp",
            "frisian_mcp.contrib.tokens",
            "frisian_mcp.contrib.oauth",
        ],
        FRISIAN_MCP_OAUTH_AUTO_APPROVE=False,
    )
    def test_silent_when_auto_approve_false(self) -> None:
        """No drift warning when AUTO_APPROVE is False."""
        out, _ = _run(security=True)
        assert "no OAuthAuthorizeConsent rows exist" not in out
