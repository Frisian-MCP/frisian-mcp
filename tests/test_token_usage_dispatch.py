"""Integration tests: ``_usage`` through the live ``McpView`` (TUR-4 / TUR-5).

Drives real ``tools/call`` requests through ``McpView.as_view()`` against an
isolated registry (the ``test_write_path_filtering`` harness pattern) so the
whole seam is exercised: opt-in resolution from settings + transport flags, the
``_usage_success`` builder, and ``result_tokens`` binding to the exact emitted
``content[0].text``.

The generic ``_usage`` shape and I/O contracts live in
``test_token_usage_contract``; this module focuses on the *wired* enable/disable
behaviour end to end.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.registry import ToolRegistry
from frisian_mcp.usage import (
    POLICY_ALLOW,
    POLICY_DENY,
    USAGE_HEADER_META,
    USAGE_POLICY_SETTING,
    USAGE_QUERY_PARAM,
    USAGE_REPORTING_SETTING,
    count_tokens,
    encoding_name,
)
from frisian_mcp.views import McpView

_view = McpView.as_view()
rf = RequestFactory()

_USAGE_KEYS = {"schema_tokens", "request_tokens", "result_tokens", "total_tokens", "encoding"}


def _stub_read_tool(payload: Any) -> Any:
    """Return a read handler that echoes a fixed payload."""

    def _fn(arguments: dict[str, Any], request: Any) -> Any:
        return payload

    return _fn


def _registry(payload: Any) -> ToolRegistry:
    """Return an isolated registry with a single read tool that returns *payload*."""
    reg = ToolRegistry()
    reg.register(
        name="device_list",
        fn=_stub_read_tool(payload),
        description="stub read tool",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        permission_classes=[],
        is_write=False,
        permission_tier="read",
    )
    return reg


def _call(*, arguments: dict[str, Any], header: str | None = None, query: str | None = None) -> Any:
    """Build a tools/call request with optional usage header / query flag."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "device_list", "arguments": arguments},
    }
    extra = {USAGE_HEADER_META: header} if header is not None else {}
    path = f"/mcp/?{USAGE_QUERY_PARAM}={query}" if query is not None else "/mcp/"
    request = rf.post(path, data=json.dumps(body), content_type="application/json", **extra)
    request.user = AnonymousUser()
    return request


def _result(response: Any) -> dict[str, Any]:
    """Extract the JSON-RPC ``result`` object from a view response."""
    return json.loads(response.content)["result"]


def _run(payload: Any, **call_kwargs: Any) -> dict[str, Any]:
    """Run a tools/call against an isolated registry and return the result dict."""
    reg = _registry(payload)
    with patch("frisian_mcp.views.tool_registry", reg):
        return _result(_view(_call(**call_kwargs)))


# ---------------------------------------------------------------------------
# OFF by default
# ---------------------------------------------------------------------------


class TestDisabledPath:
    """With the feature disabled, no ``_usage`` block appears."""

    def test_off_by_default_has_no_usage(self) -> None:
        """The default (no settings, no flags) omits ``_usage`` entirely."""
        result = _run({"devices": []}, arguments={})
        assert "_usage" not in result
        assert set(result) == {"content", "isError"}

    def test_global_off_with_header_off_has_no_usage(self) -> None:
        """A global-on config with an explicit header=off suppresses ``_usage``."""
        with override_settings(**{USAGE_REPORTING_SETTING: True}):
            reg = _registry({"devices": []})
            with patch("frisian_mcp.views.tool_registry", reg):
                result = _result(_view(_call(arguments={}, header="off")))
        assert "_usage" not in result


# ---------------------------------------------------------------------------
# ON -- global / policy / transport
# ---------------------------------------------------------------------------


class TestEnabledPaths:
    """The feature attaches ``_usage`` when opt-in resolves ON."""

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_global_enable_attaches_usage(self) -> None:
        """The global setting enables ``_usage`` for every call."""
        result = _run({"devices": ["a", "b"]}, arguments={"q": "x"})
        assert set(result["_usage"]) == _USAGE_KEYS

    def test_header_opt_in_enables_when_global_off(self) -> None:
        """With global off and no policy, a request header alone enables."""
        result = _run({"ok": True}, arguments={}, header="on")
        assert "_usage" in result

    def test_query_opt_in_enables_when_global_off(self) -> None:
        """A ``?usage=on`` query param enables when global is off."""
        result = _run({"ok": True}, arguments={}, query="on")
        assert "_usage" in result

    @override_settings(**{USAGE_POLICY_SETTING: POLICY_ALLOW})
    def test_allow_policy_enables_without_request_flag(self) -> None:
        """An ``allow`` policy turns the feature on without a request flag."""
        result = _run({"ok": True}, arguments={})
        assert "_usage" in result


# ---------------------------------------------------------------------------
# Deny is authoritative end to end
# ---------------------------------------------------------------------------


class TestDenyAuthorityWired:
    """A system ``deny`` suppresses ``_usage`` through the full request path."""

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_POLICY_SETTING: POLICY_DENY})
    def test_system_deny_beats_header_on(self) -> None:
        """A header=on cannot re-enable a denied system end to end."""
        reg = _registry({"ok": True})
        with patch("frisian_mcp.views.tool_registry", reg):
            result = _result(_view(_call(arguments={}, header="on")))
        assert "_usage" not in result

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_POLICY_SETTING: POLICY_DENY})
    def test_system_deny_beats_query_on(self) -> None:
        """A ``?usage=on`` cannot re-enable a denied system end to end."""
        reg = _registry({"ok": True})
        with patch("frisian_mcp.views.tool_registry", reg):
            result = _result(_view(_call(arguments={}, query="on")))
        assert "_usage" not in result


# ---------------------------------------------------------------------------
# Counting fidelity on the wire
# ---------------------------------------------------------------------------


class TestCountingFidelity:
    """The wire counts bind to the exact emitted bytes."""

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_result_tokens_equal_count_of_emitted_text(self) -> None:
        """``result_tokens`` equals counting the emitted ``content[0].text``."""
        result = _run({"devices": [{"id": 1}, {"id": 2}]}, arguments={"q": "search"})
        emitted = result["content"][0]["text"]
        assert result["_usage"]["result_tokens"] == count_tokens(emitted)

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_total_equals_sum_on_the_wire(self) -> None:
        """``total_tokens`` equals the sum of the three component counts."""
        result = _run({"devices": [1, 2, 3]}, arguments={"q": "y"})
        u = result["_usage"]
        assert u["total_tokens"] == u["schema_tokens"] + u["request_tokens"] + u["result_tokens"]

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_encoding_matches_active_provenance(self) -> None:
        """The ``encoding`` field matches the active counter provenance."""
        result = _run({"ok": True}, arguments={})
        assert result["_usage"]["encoding"] == encoding_name()

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_schema_tokens_nonzero_for_a_tool_with_schema(self) -> None:
        """A tool advertising a non-empty inputSchema yields positive schema_tokens."""
        result = _run({"ok": True}, arguments={})
        assert result["_usage"]["schema_tokens"] > 0
