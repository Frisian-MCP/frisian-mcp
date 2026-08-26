"""Tests for the mcp_doctor management command."""

from __future__ import annotations

import re
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings

from frisian_mcp.management.commands.mcp_doctor import _NOTE, _WARN, Command


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


class TestMcpDoctorGatewayMounting:
    """V11-19: the gateway-mount check must understand FRISIAN_MCP_ROUTES."""

    _ROUTES = {
        "default": {"path": "prodisbroken", "highest_tier": "read", "allow_list": ["*"]},
        "elevated": {"path": "fixbrokenprod", "highest_tier": "read_write", "allow_list": ["*"]},
        "admin": {"path": "breakingprod", "highest_tier": "admin", "allow_list": ["*"]},
    }

    def test_routes_host_does_not_emit_gateway_false_positive(self) -> None:
        """A ROUTES deployment must not warn that frisian_mcp:gateway is unresolvable.

        The routes ARE the gateway (PR-7 JC1); the legacy reverse-URL name is
        deliberately never registered, so probing for it is a guaranteed false
        positive on every per-route host.
        """
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=self._ROUTES):
            out, _ = _run()
        assert "Could not resolve frisian_mcp:gateway" not in out

    def test_routes_host_never_advises_the_fail_open_legacy_include(self) -> None:
        """The remedy must not be advertised on a ROUTES host: it re-exposes everything.

        ``include('frisian_mcp.urls')`` mounts the full unfiltered registry beside
        the deny-carved routes — the exact fail-open JC1 skips.  The doctor must
        never advise an operator to undo their carve-outs.
        """
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=self._ROUTES):
            out, _ = _run()
        assert "include('frisian_mcp.urls')" not in out

    def test_routes_host_reports_the_route_mounts_as_the_gateway(self) -> None:
        """The check should affirm the real surface, naming the configured paths."""
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=self._ROUTES):
            out, _ = _run()
        assert "mounted per-route" in out
        for path in ("prodisbroken", "fixbrokenprod", "breakingprod"):
            assert path in out

    def test_malformed_routes_degrade_without_a_second_error(self) -> None:
        """A bad ROUTES mapping is the surface audit's to grade, not this check's.

        This check must not raise or emit its own differently-worded error on a
        parse failure — it acknowledges ROUTES mode and defers to the audit.
        """
        with override_settings(
            FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES={"bad": "not-a-mapping"}
        ):
            out, _ = _run()
        assert "Could not resolve frisian_mcp:gateway" not in out
        assert "FRISIAN_MCP_ROUTES is configured" in out

    def test_legacy_host_still_probes_and_advises(self) -> None:
        """No ROUTES + no legacy mount → the reverse probe and its advice still fire."""
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=None):
            out, _ = _run()
        assert "Could not resolve frisian_mcp:gateway" in out
        assert "include('frisian_mcp.urls')" in out
        assert "mounted per-route" not in out


class TestMcpDoctorWarningTally:
    """V11-19 bug 2: the ⚠ lines shown must equal the count in the summary."""

    @staticmethod
    def _shown_warnings(out: str) -> list[str]:
        return [line for line in out.splitlines() if _WARN in line]

    @staticmethod
    def _summary_count(out: str) -> int:
        for line in out.splitlines():
            match = re.search(r"(\d+) warning\(s\) to review", line)
            if match:
                return int(match.group(1))
        return 0

    def test_optional_app_notice_is_a_note_not_a_counted_warning(self) -> None:
        """An absent optional app is informational: a distinct glyph, not a ⚠.

        Regression: it was rendered with the ⚠ glyph but never added to the
        review tally, so the doctor showed more warnings than it counted.
        """
        apps = [a for a in settings.INSTALLED_APPS if a != "frisian_mcp.contrib.agents"]
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, INSTALLED_APPS=apps):
            out, _ = _run()

        note_line = next(
            line for line in out.splitlines() if "contrib.agents not installed" in line
        )
        assert _NOTE in note_line
        assert _WARN not in note_line

    def test_shown_warning_count_equals_summary_count(self) -> None:
        """Shown ⚠ count equals the summary count, where it used to be off by one."""
        apps = [a for a in settings.INSTALLED_APPS if a != "frisian_mcp.contrib.agents"]
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, INSTALLED_APPS=apps):
            out, _ = _run()

        assert len(self._shown_warnings(out)) == self._summary_count(out)


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
    def test_error_when_unrecognised(self) -> None:
        """
        An unrecognised value is an ERROR that says DENIED, not a warning.

        **Updated on purpose: H13 changed this.**  It previously asserted a
        warning reading "defaulting to 'read' at runtime".  H7 made an
        unrecognised value DENY, so that sentence became false — and the same
        boot emitted E007 saying it denies, leaving an operator reading both
        with no way to tell which was true.  A diagnostic that contradicts the
        runtime is worse than no diagnostic, because it is the tool you reach
        for to check.
        """
        with pytest.raises(SystemExit):
            _run()

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="superuser")
    def test_unrecognised_message_says_denied_and_points_at_e007(self) -> None:
        """The message must state the consequence and name the startup check."""
        out = StringIO()
        cmd = Command(stdout=out, stderr=StringIO())
        with pytest.raises(SystemExit):
            cmd.handle()
        text = out.getvalue()
        assert "is not a recognised tier" in text
        assert "DENIED" in text
        assert "frisian_mcp.E007" in text
        assert "defaulting to 'read'" not in text, "pre-H7 wording resurfaced"

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER=None)
    def test_explicit_none_reported_as_deliberate_lockdown(self) -> None:
        """
        \U0001f534 The reported defect: an explicit None was reported as "not set".

        ``getattr(settings, NAME, None)`` cannot distinguish an absent setting
        from one explicitly set to ``None``, so a host deliberately locked down
        got a green tick reading "not set — anonymous callers see only read-tier
        tools" while the server denied everything.  The diagnostic told the
        operator the opposite of what was happening.
        """
        out, _ = _run()
        assert "DENIED all access" in out
        assert "deliberate lockdown" in out
        # Scope to the tier line: "not set" legitimately appears in unrelated
        # findings (e.g. the HMAC-key warning), so a whole-output check would
        # pass or fail for the wrong reason.
        tier_line = next(
            line for line in out.splitlines() if "FRISIAN_MCP_UNAUTHENTICATED_TIER" in line
        )
        assert "not set" not in tier_line, "an explicit lockdown was reported as absent"

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="none")
    def test_canonical_none_string_matches_explicit_none(self) -> None:
        """The canonical string reports identically to ``None`` — one classifier."""
        out, _ = _run()
        assert "DENIED all access" in out
        assert "deliberate lockdown" in out

    def test_doctor_and_runtime_cannot_disagree(self) -> None:
        """
        The doctor reads the SAME classifier the runtime and E007 read.

        Asserted structurally rather than by comparing message strings: three
        consumers previously derived this independently and the doctor's copy
        drifted.  If a fourth consumer appears, it inherits the answer instead
        of inventing one.
        """
        from frisian_mcp.registry import classify_unauthenticated_tier

        for value, expected_case in (
            ("read", "valid"),
            ("none", "explicit_none"),
            ("readwrite", "invalid"),
        ):
            with override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER=value):
                assert classify_unauthenticated_tier()[0] == expected_case


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


class TestMcpDoctorRouteSurface:
    """
    PR-18: mcp_doctor surfaces the per-route findings ``manage.py check`` cannot.

    These need a populated tool registry (empty at check time), so the command
    forces discovery and runs the *same* surface-audit pass PR-9a built.  The
    ``--strict`` flag escalates LOUD findings to errors so CI can gate here.
    Each test seeds a controlled registry and restores global state, so the
    forced discovery cannot bleed into other tests.
    """

    @staticmethod
    def _seed_and_check(routes: Any, *, strict: bool) -> tuple[list[str], list[str]]:
        """Seed a neutral group surface, run only the route-surface check, restore state."""
        from django.apps import apps as django_apps

        from frisian_mcp.backends.group_dispatcher import (
            build_group_input_schema,
            make_group_invoke,
        )
        from frisian_mcp.registry import tool_registry

        app_config = django_apps.get_app_config("frisian_mcp")
        saved_tools = dict(tool_registry._tools)  # noqa: SLF001
        saved_discovered = getattr(app_config, "_mcp_discovered", False)
        try:
            tool_registry._tools.clear()  # noqa: SLF001
            members = ["item_list", "item_create", "order_list"]
            for name in members:
                tool_registry.register(
                    name=name,
                    fn=lambda _a, _r, _n=name: {"tool": _n},
                    description=name,
                    input_schema={"type": "object", "properties": {}},
                    permission_classes=[],
                    permission_tier="read_write" if name.endswith("create") else "read",
                    is_write=name.endswith("create"),
                )
            tool_registry.register(
                name="catalog",
                fn=make_group_invoke(
                    "catalog", frozenset(members), tool_registry, frozenset({"item", "order"})
                ),
                description="group",
                input_schema=build_group_input_schema(),
                permission_classes=[],
                permission_tier="read",
                is_dispatcher=True,
                group_tool_names=frozenset(members),
            )
            # Pretend discovery already ran so force_tool_discovery keeps our seed.
            app_config._mcp_discovered = True  # noqa: SLF001

            warnings: list[str] = []
            errors: list[str] = []
            with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=routes):
                Command(stdout=StringIO())._check_route_surface(warnings, errors, strict=strict)
            return warnings, errors
        finally:
            tool_registry._tools.clear()  # noqa: SLF001
            tool_registry._tools.update(saved_tools)  # noqa: SLF001
            app_config._mcp_discovered = saved_discovered  # noqa: SLF001

    def test_not_applicable_when_routes_unset(self) -> None:
        """A legacy (no FRISIAN_MCP_ROUTES) host reports cleanly, not an error."""
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False):
            warnings: list[str] = []
            errors: list[str] = []
            with override_settings(FRISIAN_MCP_ROUTES=None):
                Command(stdout=StringIO())._check_route_surface(warnings, errors, strict=True)
        assert not warnings and not errors

    def test_audit_failure_is_error_under_strict_but_warning_otherwise(self) -> None:
        """A discovery/audit failure must fail --strict, not exit zero as if clean.

        Regression: under --strict an audit that could not run was downgraded to
        a warning, so a strict CI gate passed despite auditing nothing.

        Driven through ``handle()`` rather than ``_check_route_surface`` directly,
        because the property under test is the *exit code* a CI pipeline gates on.
        Discovery now runs in ``handle()``, so a unit call to the check would not
        exercise the failure path at all.
        """
        routes = {"default": {"path": "mcp", "allow_list": ["*"]}}
        with (
            override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=routes),
            patch(
                "frisian_mcp.route_audit.force_tool_discovery",
                side_effect=RuntimeError("discovery blew up"),
            ),
        ):
            # A gate that cannot evaluate must not pass: non-zero exit.
            with pytest.raises(SystemExit) as excinfo:
                _run(strict=True)
            assert excinfo.value.code == 1

            # Without --strict the command is a diagnostic, not a gate: it
            # reports the failure and still exits zero.
            out, _ = _run(strict=False)

        assert "could not run" in out

    def test_strict_exit_is_the_only_difference_on_a_healthy_host(self) -> None:
        """--strict must not invent failures: a clean surface still exits zero."""
        routes = {"default": {"path": "mcp", "allow_list": ["*"]}}
        with override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=routes):
            out, _ = _run(strict=True)
        assert "could not run" not in out

    def test_discovery_runs_before_registry_reading_checks(self) -> None:
        """Bug 2: the performance check must not measure an empty registry.

        ``_run_deferred_discovery`` is wired to ``request_started``, which never
        fires under a management command — so without an explicit force, every
        registry-reading check sees zero tools and the page-size / cache warnings
        it exists to raise can never fire.  Pin the ordering: discovery is forced
        before the performance hints read the registry.
        """
        calls: list[str] = []

        real_force = Command._force_discovery
        real_perf = Command._check_performance_hints

        def spy_force(self: Command) -> Exception | None:
            calls.append("discovery")
            return real_force(self)

        def spy_perf(self: Command, warnings: list[str], **kwargs: Any) -> None:
            calls.append("performance")
            return real_perf(self, warnings, **kwargs)

        with (
            override_settings(FRISIAN_MCP_STARTUP_PRINT=False),
            patch.object(Command, "_force_discovery", spy_force),
            patch.object(Command, "_check_performance_hints", spy_perf),
        ):
            _run()

        assert calls.index("discovery") < calls.index("performance")

    def test_discovery_is_forced_on_a_legacy_host_with_no_routes(self) -> None:
        """Discovery is unconditional — it is not a per-route concern.

        This is the half a mere reorder would miss.  ``_check_route_surface``
        early-returns when ``FRISIAN_MCP_ROUTES`` is unset, so while discovery
        lived inside it, a legacy single-mount host never populated its registry
        and its performance hints measured zero tools forever.  Hoisting the force
        into ``handle()`` is what fixes those hosts, not the ordering alone.
        """
        with (
            override_settings(FRISIAN_MCP_STARTUP_PRINT=False, FRISIAN_MCP_ROUTES=None),
            patch("frisian_mcp.route_audit.force_tool_discovery") as forced,
        ):
            _run()

        assert forced.called

    def test_performance_hints_say_so_when_discovery_failed(self) -> None:
        """An unmeasurable tool count is reported, never silently rendered as zero.

        Reporting the empty registry as a real measurement would retire the
        page-size and cache warnings without anyone noticing — the same
        silence-as-success shape as the strict gate above, one check over.
        """
        with (
            override_settings(FRISIAN_MCP_STARTUP_PRINT=False),
            patch(
                "frisian_mcp.route_audit.force_tool_discovery",
                side_effect=RuntimeError("discovery blew up"),
            ),
        ):
            out, _ = _run()

        assert "could not be evaluated" in out

    def test_clean_route_reports_nothing(self) -> None:
        """A route that exposes tools with no dead entries is clean."""
        warnings, errors = self._seed_and_check(
            {"default": {"path": "mcp", "allow_list": ["*"]}}, strict=True
        )
        assert not warnings and not errors

    def test_net_empty_is_warning_without_strict(self) -> None:
        """W008 (deny zeroes allow) is a warning by default — no error, no exit."""
        warnings, errors = self._seed_and_check(
            {"default": {"path": "mcp", "allow_list": ["catalog"], "deny_list": ["catalog"]}},
            strict=False,
        )
        assert any("W008" in w for w in warnings)
        assert not errors

    def test_net_empty_is_error_under_strict(self) -> None:
        """W008 escalates to an error under --strict, so CI exits non-zero."""
        warnings, errors = self._seed_and_check(
            {"default": {"path": "mcp", "allow_list": ["catalog"], "deny_list": ["catalog"]}},
            strict=True,
        )
        assert any("W008" in e for e in errors)

    def test_soft_carve_out_stays_warning_under_strict(self) -> None:
        """W009 (a working carve-out) is SOFT — --strict must NOT escalate it."""
        warnings, errors = self._seed_and_check(
            {"default": {"path": "mcp", "allow_list": ["*"], "deny_list": ["catalog:item"]}},
            strict=True,
        )
        assert any("W009" in w for w in warnings)
        assert not errors

    def test_route_attributed_exactly_once(self) -> None:
        """The rendered finding names its route once, never doubled."""
        warnings, _ = self._seed_and_check(
            {"default": {"path": "mcp", "allow_list": ["catalog"], "deny_list": ["catalog"]}},
            strict=False,
        )
        assert warnings
        assert warnings[0].count("route 'default'") == 1


class TestMcpDoctorCiGateFixture:
    """V11-21: the CI ``mcp_doctor --strict`` gate must stay green on its fixture.

    The pipeline runs ``django-admin mcp_doctor --strict`` against
    ``frisian_mcp._ci_doctor_settings``.  For the gate to be stable rather than
    flaky, that representative config must produce no error-level or LOUD
    finding.  This pins it as a unit test so a fixture regression is caught here,
    not only in CI.
    """

    def test_ci_doctor_config_passes_strict(self) -> None:
        """The gate's representative config exits 0 under --strict."""
        from frisian_mcp import _ci_doctor_settings as cfg

        with override_settings(
            FRISIAN_MCP_STARTUP_PRINT=False,
            FRISIAN_MCP_ROUTES=cfg.FRISIAN_MCP_ROUTES,
            FRISIAN_MCP_ALLOW_UNAUTHENTICATED=cfg.FRISIAN_MCP_ALLOW_UNAUTHENTICATED,
        ):
            # No SystemExit: --strict found nothing error-level or LOUD.
            out, _ = _run(strict=True)

        assert "error(s) found" not in out
        assert "mounted per-route" in out


THREE_DOORS: dict[str, Any] = {
    "default": {"path": "openread", "highest_tier": "read"},
    "elevated": {"path": "scopedwrite", "highest_tier": "read_write"},
    "admin": {"path": "mySuperSecureAdminPath", "highest_tier": "admin"},
}


@pytest.mark.django_db
class TestDiscoveryReachability:
    """
    PRA-4 — the doctor must report what a client can reach, not what is configured.

    The precedent this check exists to avoid: a documented
    ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` lockdown was a no-op, and mcp_doctor
    greenlit the locked-down host anyway, because the check read the *setting*
    instead of the *effect*.

    So the load-bearing test here is
    :meth:`test_routes_mounted_but_discovery_closed_is_reported`: a host with
    three routes mounted and ``FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False`` is
    fully and correctly configured by every other measure the doctor applies,
    and no client can discover a thing.  A check that passes there is the wrong
    check.
    """

    @override_settings(
        FRISIAN_MCP_ROUTES=THREE_DOORS,
        FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False,
        ROOT_URLCONF="tests.urls_wellknown",
    )
    def test_routes_mounted_but_discovery_closed_is_reported(self) -> None:
        """The exact configuration that makes the release's fix inert."""
        out, _ = _run()
        assert "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False" in out
        assert "no OAuth client can discover" in out
        assert _WARN in out

    @override_settings(
        FRISIAN_MCP_ROUTES=THREE_DOORS,
        FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False,
        ROOT_URLCONF="tests.urls_wellknown",
    )
    def test_the_finding_names_both_settings_and_the_consequence(self) -> None:
        """The message is the documentation — an operator needs nothing else."""
        out, _ = _run()
        line = next(ln for ln in out.splitlines() if "no OAuth client can discover" in ln)
        assert "FRISIAN_MCP_ROUTES" in line
        assert "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY" in line
        # Names the way out, not just the problem.
        assert "Set FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=True" in line

    @override_settings(FRISIAN_MCP_ROUTES=THREE_DOORS, ROOT_URLCONF="tests.urls_wellknown")
    def test_the_same_host_with_discovery_open_passes(self) -> None:
        """
        The other half of proving it is a real check rather than a green light.

        Identical routes; only ``PUBLIC_DISCOVERY`` differs.  A guard that has
        never been observed to *stop* refusing is as untrustworthy as one never
        observed to refuse.
        """
        out, _ = _run()
        assert "no OAuth client can discover" not in out
        assert "OAuth discovery reachable" in out
        assert "3 authenticated route(s)" not in out  # the open door is not one
        assert "2 authenticated route(s) mounted" in out
        # The bare document carries ONE resource, so the line must name the one
        # door it resolves to rather than implying it describes both (PRA-8).
        assert "resolves to /scopedwrite" in out

    @override_settings(
        FRISIAN_MCP_ROUTES={"default": {"path": "openread", "highest_tier": "read"}},
        FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False,
        ROOT_URLCONF="tests.urls_wellknown",
    )
    def test_an_all_open_host_is_not_warned(self) -> None:
        """
        Scoped to hosts that have something to advertise.

        No route requires authentication, so there is genuinely no protected
        resource and the endpoint's 404 is the honest answer.  Warning here would
        fire on the open-demo posture the package explicitly supports — a false
        positive on a working deployment.
        """
        out, _ = _run()
        assert "no OAuth client can discover" not in out
        assert "OAuth discovery reachable" not in out

    @override_settings(FRISIAN_MCP_ROUTES=None, ROOT_URLCONF="tests.urls_wellknown")
    def test_a_legacy_single_door_host_is_not_warned(self) -> None:
        """Per-route discovery is not applicable without per-route mounting."""
        out, _ = _run()
        assert "no OAuth client can discover" not in out
        assert "OAuth discovery reachable" not in out

    @override_settings(
        FRISIAN_MCP_ROUTES=THREE_DOORS,
        FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False,
        ROOT_URLCONF="tests.urls_wellknown",
    )
    def test_strict_does_not_escalate_it_to_an_error(self) -> None:
        """
        Deliberate: closed discovery is a posture, not a defect.

        ``--strict`` exists so CI can gate on the route *surface*.  A host that
        issues pre-provisioned credentials only — a posture that gets *more*
        attractive now that the bare endpoint names the most privileged route —
        would otherwise fail a gate for a setting it chose on purpose.  Contrast
        an audit that could not run, which strict mode does escalate, because
        that has proved nothing.
        """
        out, _ = _run(strict=True)
        assert "no OAuth client can discover" in out
        assert "error(s) found" not in out

    @override_settings(
        FRISIAN_MCP_ROUTES=THREE_DOORS,
        ROOT_URLCONF="frisian_mcp._ci_doctor_urls",
    )
    def test_unmounted_wellknown_urls_are_not_reported_as_reachable(self) -> None:
        """
        CodeRabbit: the probe calls the view, which cannot see URL resolution.

        With the ``.well-known`` URLs absent from the URLconf a client gets a
        404 the view never sees, so a "discovery reachable" tick would be the
        mechanism-not-effect error this check exists to avoid.

        It stays *silent* rather than warning: ``_check_url_mounting`` already
        reports this cause and names the fix, and two messages for one root
        cause is the pattern E011 deliberately avoids.  The assertion on that
        existing warning is what makes silence safe rather than a new blind
        spot — if it ever stops firing, this test fails too.
        """
        out, _ = _run()
        assert "OAuth discovery reachable" not in out
        assert "OAuth .well-known URLs not mounted" in out

    @override_settings(
        FRISIAN_MCP_ROUTES=THREE_DOORS,
        ROOT_URLCONF="tests.urls_wellknown_partial",
    )
    def test_a_partially_mounted_wellknown_is_not_reported_as_reachable(self) -> None:
        """
        CodeRabbit: the two well-known URLs are separately mountable.

        This URLconf mounts the authorization-server endpoint and omits the
        protected-resource one.  Reversing the neighbour would report discovery
        reachable while a client GET on ``/.well-known/oauth-protected-resource``
        takes a URLconf 404 the view never sees — so the gate must reverse the
        endpoint it actually probes.

        ``_check_url_mounting`` still ticks here, because it asks a different and
        looser question ("is the include present at all?").  That is pre-existing
        behaviour and is asserted, not silently tolerated: if it ever tightens,
        this test says so rather than quietly passing for a new reason.
        """
        out, _ = _run()
        assert "OAuth discovery reachable" not in out
        assert "OAuth .well-known URLs mounted" in out
