"""Tests for the content-visible usage line (TUR-11 / TUR-12).

Covers the model-self-report surface: a SECOND ``content`` item, appended last,
emitted only when the master gate is ON *and* the subordinate content opt-in is
ON.  Exercises the opt-in (setting + header/query), the interaction matrix
(nothing / sibling-only / sibling+line), deny-suppresses-both, the
master-first ordering (content-on cannot resurrect a disabled master), and the
``resolve_usage_in_content`` precedence + string-coercion footgun guard.

The generic ``_usage`` shape lives in ``test_token_usage_contract``; the wired
enable/disable of the sibling in ``test_token_usage_dispatch``.  This module is
the content-line analogue of the latter.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.registry import ToolRegistry
from frisian_mcp.usage import (
    POLICY_DENY,
    USAGE_CONTENT_HEADER_META,
    USAGE_CONTENT_QUERY_PARAM,
    USAGE_HEADER_META,
    USAGE_IN_CONTENT_SETTING,
    USAGE_POLICY_SETTING,
    USAGE_REPORTING_SETTING,
    parse_content_request_flag,
    resolve_usage_in_content,
)
from frisian_mcp.views import McpView

_view = McpView.as_view()
rf = RequestFactory()

_LINE_PREFIX = "_usage: "
_PAYLOAD = {"items": [1, 2, 3], "name": "hello"}


def _registry() -> ToolRegistry:
    """Return an isolated registry with a single read tool returning a fixed payload."""
    reg = ToolRegistry()
    reg.register(
        name="device_list",
        fn=lambda arguments, request: _PAYLOAD,
        description="stub read tool",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        permission_classes=[],
        is_write=False,
        permission_tier="read",
    )
    return reg


def _call(
    *,
    usage_header: str | None = None,
    content_header: str | None = None,
    content_query: str | None = None,
) -> Any:
    """Build a tools/call request with optional master + content transport flags."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "device_list", "arguments": {"q": "x"}},
    }
    extra: dict[str, str] = {}
    if usage_header is not None:
        extra[USAGE_HEADER_META] = usage_header
    if content_header is not None:
        extra[USAGE_CONTENT_HEADER_META] = content_header
    if content_query is not None:
        path = f"/mcp/?{USAGE_CONTENT_QUERY_PARAM}={content_query}"
    else:
        path = "/mcp/"
    request = rf.post(path, data=json.dumps(body), content_type="application/json", **extra)
    request.user = AnonymousUser()
    return request


def _run(**call_kwargs: Any) -> dict[str, Any]:
    """Run a tools/call against the isolated registry and return the result dict."""
    reg = _registry()
    with patch("frisian_mcp.views.tool_registry", reg):
        return json.loads(_view(_call(**call_kwargs)).content)["result"]


def _content_line(result: dict[str, Any]) -> str | None:
    """Return the appended usage line's text if a second content item is present."""
    content = result["content"]
    return content[1]["text"] if len(content) == 2 else None


class _FakeRequest:
    """Minimal request stub with the ``META``/``GET`` surface the resolver reads."""

    def __init__(
        self, meta: dict[str, str] | None = None, get: dict[str, str] | None = None
    ) -> None:
        self.META = meta or {}
        self.GET = get or {}


class TestContentDefaultOff:
    """With the master ON, the content line stays OFF until explicitly enabled."""

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_master_on_content_unset_sibling_only(self) -> None:
        """Unset content opt-in → sibling present, no second content item."""
        result = _run()
        assert "_usage" in result
        assert len(result["content"]) == 1
        assert _content_line(result) is None

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: False})
    def test_master_on_content_explicit_false_sibling_only(self) -> None:
        """Explicit content=False → sibling only."""
        result = _run()
        assert len(result["content"]) == 1

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: "false"})
    def test_content_setting_truthy_string_false_stays_off(self) -> None:
        """Footgun guard: the string ``"false"`` is truthy but must resolve OFF."""
        result = _run()
        assert len(result["content"]) == 1


class TestBothOn:
    """Master ON + content ON emits a second content item mirroring the sibling."""

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True})
    def test_two_content_items(self) -> None:
        """Both on → exactly two content items plus the sibling."""
        result = _run()
        assert len(result["content"]) == 2
        assert "_usage" in result

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True})
    def test_line_prefix_and_numbers_match_sibling(self) -> None:
        """The line is ``_usage: <json>`` with numbers identical to the sibling."""
        result = _run()
        line = _content_line(result)
        assert line is not None
        assert line.startswith(_LINE_PREFIX)
        assert json.loads(line[len(_LINE_PREFIX) :]) == result["_usage"]

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True})
    def test_content0_is_untouched_tool_payload(self) -> None:
        """content[0] remains the raw tool payload, never mutated by the line."""
        result = _run()
        assert json.loads(result["content"][0]["text"]) == _PAYLOAD

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True})
    def test_content0_identical_to_content_off(self) -> None:
        """content[0] is byte-identical whether or not the line is appended."""
        with_line = _run()
        with override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: False}):
            without_line = _run()
        assert with_line["content"][0] == without_line["content"][0]


class TestContentRequestOptIn:
    """The content line can be toggled per-request, header winning over query."""

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_content_header_on(self) -> None:
        """A content header alone enables the line when the master is on."""
        result = _run(content_header="on")
        assert len(result["content"]) == 2

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_content_query_on(self) -> None:
        """A ``?usage_content=on`` query enables the line."""
        result = _run(content_query="on")
        assert len(result["content"]) == 2

    @override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True})
    def test_request_off_opts_out_of_setting_on(self) -> None:
        """A request content=off opts out even when the setting is on."""
        result = _run(content_header="off")
        assert len(result["content"]) == 1

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_content_header_beats_query(self) -> None:
        """Content header=off wins over query=on."""
        result = _run(content_header="off", content_query="on")
        assert len(result["content"]) == 1


class TestMasterAuthoritative:
    """The content surface can never resurrect a disabled or denied master."""

    def test_content_on_but_master_off_emits_nothing(self) -> None:
        """Content-on with the master off yields neither sibling nor line."""
        with override_settings(**{USAGE_IN_CONTENT_SETTING: True}):
            result = _run(content_header="on")
        assert "_usage" not in result
        assert len(result["content"]) == 1

    @override_settings(
        **{
            USAGE_REPORTING_SETTING: True,
            USAGE_POLICY_SETTING: POLICY_DENY,
            USAGE_IN_CONTENT_SETTING: True,
        }
    )
    def test_system_deny_suppresses_sibling_and_line(self) -> None:
        """System deny suppresses BOTH surfaces even with every opt-in on."""
        result = _run(usage_header="on", content_header="on", content_query="on")
        assert "_usage" not in result
        assert len(result["content"]) == 1


class TestResolveUsageInContent:
    """Unit coverage for the subordinate content resolver (no master coupling)."""

    def test_default_off_when_unset(self) -> None:
        """Unset → False (caller-only default)."""
        assert resolve_usage_in_content(_FakeRequest()) is False

    @override_settings(**{USAGE_IN_CONTENT_SETTING: True})
    def test_setting_on(self) -> None:
        """The L0 setting on → True."""
        assert resolve_usage_in_content(_FakeRequest()) is True

    @override_settings(**{USAGE_IN_CONTENT_SETTING: "false"})
    def test_setting_truthy_string_false_is_off(self) -> None:
        """The truthy string ``"false"`` coerces to OFF."""
        assert resolve_usage_in_content(_FakeRequest()) is False

    def test_header_beats_query(self) -> None:
        """Content header=off wins over query=on at the resolver level."""
        req = _FakeRequest(
            meta={USAGE_CONTENT_HEADER_META: "off"},
            get={USAGE_CONTENT_QUERY_PARAM: "on"},
        )
        assert resolve_usage_in_content(req) is False

    def test_query_used_when_header_absent(self) -> None:
        """The query flag applies when no content header is present."""
        req = _FakeRequest(get={USAGE_CONTENT_QUERY_PARAM: "on"})
        assert resolve_usage_in_content(req) is True

    def test_parse_flag_garbage_is_none(self) -> None:
        """A garbage content flag parses to None (never enables)."""
        req = _FakeRequest(meta={USAGE_CONTENT_HEADER_META: "maybe"})
        assert parse_content_request_flag(req) is None
