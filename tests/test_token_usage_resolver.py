"""Unit tests for the layered opt-in resolver (TUR-3, core of TUR-5).

The resolver is a pure function of ``(global setting, system policy, request)``
with system-``deny`` authoritative.  This module owns the durable proof of:

* the full 9-cell system x request truth table,
* the deny-authority invariant, incl. the ``deny x on x global-on -> OFF`` cell,
* header-wins-over-query precedence,
* off-by-default,
* malformed / garbage flags parsing to *unset* (never a silent enable).

All truth-table cells are driven through a real ``RequestFactory`` request so
the header (``HTTP_X_FRISIAN_MCP_USAGE``) and query (``?usage=``) code paths are
exercised, not just the parse helpers.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import RequestFactory, override_settings

from frisian_mcp.usage import (
    POLICY_ALLOW,
    POLICY_DENY,
    USAGE_HEADER_META,
    USAGE_POLICY_SETTING,
    USAGE_REPORTING_SETTING,
    parse_flag_value,
    parse_request_flag,
    resolve_system_policy,
    resolve_usage_reporting,
)

rf = RequestFactory()


def _request(*, header: str | None = None, query: str | None = None) -> Any:
    """Build a POST /mcp/ request carrying an optional usage header / query flag."""
    extra = {USAGE_HEADER_META: header} if header is not None else {}
    path = f"/mcp/?usage={query}" if query is not None else "/mcp/"
    return rf.post(path, content_type="application/json", **extra)


def _settings(*, global_default: bool = False, policy: str | None = None) -> Any:
    """Return an ``override_settings`` context for the given global + policy."""
    kwargs: dict[str, Any] = {USAGE_REPORTING_SETTING: global_default}
    if policy is not None:
        kwargs[USAGE_POLICY_SETTING] = policy
    return override_settings(**kwargs)


# ---------------------------------------------------------------------------
# parse_flag_value -- token parsing, malformed -> None
# ---------------------------------------------------------------------------


class TestParseFlagValue:
    """Single-token flag parsing: truthy / falsy / unset."""

    @pytest.mark.parametrize("raw", ["on", "ON", " On ", "1", "true", "TRUE", "yes", "YES"])
    def test_truthy_tokens(self, raw: str) -> None:
        """Recognised truthy tokens parse to True (case/space-insensitive)."""
        assert parse_flag_value(raw) is True

    @pytest.mark.parametrize("raw", ["off", "OFF", " off ", "0", "false", "no", "NO"])
    def test_falsy_tokens(self, raw: str) -> None:
        """Recognised falsy tokens parse to False."""
        assert parse_flag_value(raw) is False

    @pytest.mark.parametrize("raw", ["", "   ", "enable", "maybe", "2", "onoff", "y", "t"])
    def test_unrecognised_tokens_are_none(self, raw: str) -> None:
        """Garbage never enables -- it parses to unset (None)."""
        assert parse_flag_value(raw) is None

    @pytest.mark.parametrize("raw", [None, 1, True, ["on"], {"on": 1}])
    def test_non_string_is_none(self, raw: object) -> None:
        """A non-string flag value parses to None."""
        assert parse_flag_value(raw) is None


# ---------------------------------------------------------------------------
# parse_request_flag -- header wins over query
# ---------------------------------------------------------------------------


class TestParseRequestFlag:
    """Per-request flag extraction with header-wins-over-query precedence."""

    def test_header_only(self) -> None:
        """A header-only request resolves from the header."""
        assert parse_request_flag(_request(header="on")) is True

    def test_query_only(self) -> None:
        """A query-only request resolves from the query param."""
        assert parse_request_flag(_request(query="on")) is True

    def test_header_wins_over_query_when_conflicting(self) -> None:
        """When header and query disagree, the header value wins."""
        assert parse_request_flag(_request(header="off", query="on")) is False
        assert parse_request_flag(_request(header="on", query="off")) is True

    def test_garbage_header_falls_through_to_query(self) -> None:
        """A malformed header is unset, so a valid query value still applies."""
        assert parse_request_flag(_request(header="garbage", query="on")) is True

    def test_neither_is_none(self) -> None:
        """No header and no query resolves to None."""
        assert parse_request_flag(_request()) is None

    def test_missing_meta_and_get_is_none(self) -> None:
        """An object without META/GET resolves to None rather than raising."""
        assert parse_request_flag(object()) is None


# ---------------------------------------------------------------------------
# resolve_system_policy -- only exact allow/deny honoured
# ---------------------------------------------------------------------------


class TestResolveSystemPolicy:
    """L1 policy normalisation: only exact allow/deny honoured, else defer."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("deny", POLICY_DENY),
            ("DENY", POLICY_DENY),
            ("  deny ", POLICY_DENY),
            ("allow", POLICY_ALLOW),
            ("Allow", POLICY_ALLOW),
            ("block", None),
            ("disable", None),
            ("", None),
            ("true", None),
        ],
    )
    def test_normalisation(self, raw: str, expected: str | None) -> None:
        """The raw policy string is stripped/lower-cased; unknowns defer."""
        with override_settings(**{USAGE_POLICY_SETTING: raw}):
            assert resolve_system_policy() == expected

    def test_unset_setting_is_none(self) -> None:
        """No policy setting at all defers (None), never forcing on/off."""
        assert resolve_system_policy() in (None,)

    @pytest.mark.parametrize("raw", [None, 123, ["deny"], {"policy": "deny"}])
    def test_non_string_defers(self, raw: object) -> None:
        """A non-string policy value defers to None."""
        with override_settings(**{USAGE_POLICY_SETTING: raw}):
            assert resolve_system_policy() is None


# ---------------------------------------------------------------------------
# resolve_usage_reporting -- the 9-cell truth table (system x request)
# ---------------------------------------------------------------------------

# (policy, request_flag, expected) with global default OFF.  request_flag is the
# header value passed to the request ("on"/"off"/None).
_MATRIX_GLOBAL_OFF = [
    # deny row -- authoritative OFF regardless of request
    (POLICY_DENY, None, False),
    (POLICY_DENY, "on", False),
    (POLICY_DENY, "off", False),
    # allow row -- ON unless the request opts out
    (POLICY_ALLOW, None, True),
    (POLICY_ALLOW, "on", True),
    (POLICY_ALLOW, "off", False),
    # unset row -- defers to request, else global default (OFF here)
    (None, None, False),
    (None, "on", True),
    (None, "off", False),
]


class TestResolveUsageMatrixGlobalOff:
    """The 9-cell truth table with the global default OFF."""

    @pytest.mark.parametrize("policy, flag, expected", _MATRIX_GLOBAL_OFF)
    def test_cell(self, policy: str | None, flag: str | None, expected: bool) -> None:
        """Each (policy, request) cell resolves to its expected boolean."""
        with _settings(global_default=False, policy=policy):
            assert resolve_usage_reporting(_request(header=flag)) is expected


class TestResolveUsageMatrixGlobalOn:
    """With global default ON, only the ``unset`` row's default flips to ON."""

    @pytest.mark.parametrize(
        "policy, flag, expected",
        [
            # deny still authoritative even with global ON and request ON.
            (POLICY_DENY, None, False),
            (POLICY_DENY, "on", False),
            # allow unchanged.
            (POLICY_ALLOW, "off", False),
            (POLICY_ALLOW, None, True),
            # unset row: default is now ON; explicit off still opts out.
            (None, None, True),
            (None, "off", False),
            (None, "on", True),
        ],
    )
    def test_cell(self, policy: str | None, flag: str | None, expected: bool) -> None:
        """Each cell resolves correctly when the global default is ON."""
        with _settings(global_default=True, policy=policy):
            assert resolve_usage_reporting(_request(header=flag)) is expected


class TestDenyAuthority:
    """Deny is authoritative from every angle -- the security-critical invariant."""

    def test_deny_beats_request_on_and_global_on(self) -> None:
        """The single most important cell: deny x on x global-on -> OFF."""
        with _settings(global_default=True, policy=POLICY_DENY):
            assert resolve_usage_reporting(_request(header="on", query="on")) is False

    def test_deny_beats_query_on(self) -> None:
        """A query flag cannot re-enable a denied system."""
        with _settings(global_default=True, policy=POLICY_DENY):
            assert resolve_usage_reporting(_request(query="on")) is False

    def test_deny_returns_false_before_request_is_parsed(self, monkeypatch: Any) -> None:
        """Under deny, the request flag must never even be consulted."""
        import frisian_mcp.usage.resolver as resolver_mod

        def _boom(_request: Any) -> bool:
            raise AssertionError("request flag parsed under a deny policy")

        monkeypatch.setattr(resolver_mod, "parse_request_flag", _boom)
        with _settings(global_default=True, policy=POLICY_DENY):
            assert resolver_mod.resolve_usage_reporting(_request(header="on")) is False


class TestOffByDefault:
    """The pristine default must resolve OFF."""

    def test_no_settings_no_flags_is_off(self) -> None:
        """Feature ships OFF for every existing consumer."""
        with _settings():
            assert resolve_usage_reporting(_request()) is False
