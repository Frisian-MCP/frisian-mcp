"""Contract tests for the ``_usage`` envelope (TUR-1 / TUR-5).

These are the versioning / backwards-compatibility guarantees that downstream
agents parse against:

* the ``_usage`` block's exact key set and value types,
* **off-by-default = byte-identical** to the pre-feature response,
* **additive-only** when on (``content``/``isError`` untouched),
* the advertised ``tools/list`` ``inputSchema`` is unchanged by the feature
  (usage is response-side only -- the small-schema dispatcher economy is
  preserved),
* the PM-required **schema-token parity**: ``_caller_visible_schema`` (the
  source of ``schema_tokens``) equals the ``inputSchema`` ``tools/list``
  surfaces for the same tool, across a flat tool, a tier-capped dispatcher, and
  a permission-filtered regime -- the CI tripwire that keeps ``schema_tokens``
  honest if ``list_tools`` visibility logic ever drifts.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.apps import _install_dispatch_groups
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.usage import FALLBACK_ENCODING, TOKENIZER_ENCODING, USAGE_REPORTING_SETTING
from frisian_mcp.views import McpView, _caller_visible_schema

pytestmark = pytest.mark.contract

_view = McpView.as_view()
rf = RequestFactory()

_USAGE_KEYS = {"schema_tokens", "request_tokens", "result_tokens", "total_tokens", "encoding"}


def _stub(payload: Any) -> Any:
    """Return a read handler that echoes a fixed payload."""

    def _fn(arguments: dict[str, Any], request: Any) -> Any:
        return payload

    return _fn


def _flat_registry() -> ToolRegistry:
    """Return an isolated registry with a single flat read tool."""
    reg = ToolRegistry()
    reg.register(
        name="device_list",
        fn=_stub({"devices": []}),
        description="stub read tool",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        permission_classes=[],
        is_write=False,
        permission_tier="read",
    )
    return reg


def _call_tool(arguments: dict[str, Any]) -> Any:
    """Build a tools/call request for the flat ``device_list`` tool."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "device_list", "arguments": arguments},
    }
    request = rf.post("/mcp/", data=json.dumps(body), content_type="application/json")
    request.user = AnonymousUser()
    return request


def _result(response: Any) -> dict[str, Any]:
    """Extract the JSON-RPC ``result`` object from a view response."""
    return json.loads(response.content)["result"]


# ---------------------------------------------------------------------------
# _usage shape / type contract
# ---------------------------------------------------------------------------


class TestUsageShapeContract:
    """The ``_usage`` block's key set and value types are stable."""

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_exact_key_set(self) -> None:
        """``_usage`` carries exactly the five documented keys."""
        reg = _flat_registry()
        with patch("frisian_mcp.views.tool_registry", reg):
            usage = _result(_view(_call_tool({"q": "x"})))["_usage"]
        assert set(usage) == _USAGE_KEYS

    @override_settings(**{USAGE_REPORTING_SETTING: True})
    def test_value_types(self) -> None:
        """The four counts are ints and ``encoding`` is a known string."""
        reg = _flat_registry()
        with patch("frisian_mcp.views.tool_registry", reg):
            usage = _result(_view(_call_tool({"q": "x"})))["_usage"]
        for key in ("schema_tokens", "request_tokens", "result_tokens", "total_tokens"):
            assert isinstance(usage[key], int)
        assert isinstance(usage["encoding"], str)
        assert usage["encoding"] in {TOKENIZER_ENCODING, FALLBACK_ENCODING}


# ---------------------------------------------------------------------------
# off-by-default byte-identity + additive-only
# ---------------------------------------------------------------------------


class TestBackwardsCompatContract:
    """The feature is invisible when off and additive-only when on."""

    def test_off_is_pre_feature_shape(self) -> None:
        """Disabled, the result is exactly ``{content, isError}``."""
        reg = _flat_registry()
        with patch("frisian_mcp.views.tool_registry", reg):
            off = _result(_view(_call_tool({"q": "x"})))
        assert "_usage" not in off
        assert set(off) == {"content", "isError"}

    def test_on_is_additive_only(self) -> None:
        """Enabling adds only the ``_usage`` sibling; content/isError unchanged."""
        reg = _flat_registry()
        with patch("frisian_mcp.views.tool_registry", reg):
            off = _result(_view(_call_tool({"q": "x"})))
            with override_settings(**{USAGE_REPORTING_SETTING: True}):
                on = _result(_view(_call_tool({"q": "x"})))
        assert on["content"] == off["content"]
        assert on["isError"] == off["isError"]
        assert set(on) - set(off) == {"_usage"}

    def test_input_schema_is_unchanged_by_the_feature(self) -> None:
        """Advertised tools/list inputSchema is identical whether usage on or off."""
        reg = _flat_registry()

        def _list_schema() -> Any:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            req = rf.post("/mcp/", data=json.dumps(body), content_type="application/json")
            req.user = AnonymousUser()
            tools = json.loads(_view(req).content)["result"]["tools"]
            return {t["name"]: t["inputSchema"] for t in tools}

        with patch("frisian_mcp.views.tool_registry", reg):
            off = _list_schema()
            with override_settings(**{USAGE_REPORTING_SETTING: True}):
                on = _list_schema()
        assert off == on


# ---------------------------------------------------------------------------
# schema_tokens parity: _caller_visible_schema == tools/list inputSchema
# ---------------------------------------------------------------------------


def _grouped_registry() -> ToolRegistry:
    """Registry with a flat tool + tiered tools destined for a group dispatcher."""
    reg = ToolRegistry()
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    reg.register(
        "user_list", _stub({}), "flat", schema, permission_classes=[], permission_tier="read"
    )
    reg.register(
        "device_list", _stub({}), "grp", schema, permission_classes=[], permission_tier="read"
    )
    reg.register(
        "device_retrieve", _stub({}), "grp", schema, permission_classes=[], permission_tier="read"
    )
    reg.register(
        "device_create",
        _stub({}),
        "grp",
        schema,
        permission_classes=[],
        permission_tier="read_write",
    )
    return reg


def _tools_list() -> tuple[Any, list[dict[str, Any]]]:
    """Return (request, tools[]) from a real tools/list through the view."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    request = rf.post("/mcp/", data=json.dumps(body), content_type="application/json")
    request.user = AnonymousUser()
    response = _view(request)
    tools = json.loads(response.content)["result"]["tools"]
    return request, tools


class TestSchemaTokenParity:
    """``_caller_visible_schema`` must equal what ``tools/list`` surfaces.

    Uses the SAME request object for both sides so the comparison is faithful to
    the wired state. ``_get_token_permission`` is patched so the flat-tool,
    tier-capped-dispatcher, and permission-filtered regimes are all exercised
    with a deterministic caller tier.
    """

    def _assert_parity_for_all_listed_tools(self, reg: ToolRegistry, tier: str) -> None:
        """For every tool tools/list surfaces, the count source must match it."""
        with (
            patch("frisian_mcp.apps.tool_registry", reg, create=True),
            patch("frisian_mcp.registry.tool_registry", reg),
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views._get_token_permission", return_value=tier),
            override_settings(FRISIAN_MCP_DISPATCH_GROUPS={"svc": ["device"]}),
        ):
            _install_dispatch_groups()  # builds the tier-mixed `svc` dispatcher
            request, tools = _tools_list()
            assert tools, "expected at least one visible tool"
            for tool in tools:
                name = tool["name"]
                assert _caller_visible_schema(request, name) == tool["inputSchema"], name

    def test_parity_read_tier_flat_and_tier_capped_dispatcher(self) -> None:
        """Read tier: a flat tool plus a tier-capped dispatcher schema."""
        self._assert_parity_for_all_listed_tools(_grouped_registry(), tier="read")

    def test_parity_admin_tier_full_dispatcher(self) -> None:
        """Admin tier: the un-capped dispatcher path, every action visible."""
        self._assert_parity_for_all_listed_tools(_grouped_registry(), tier="admin")

    def test_parity_under_permission_aware_discovery(self) -> None:
        """Permission-aware discovery: parity holds for whatever survives the filter."""
        with override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True):
            self._assert_parity_for_all_listed_tools(_grouped_registry(), tier="admin")
