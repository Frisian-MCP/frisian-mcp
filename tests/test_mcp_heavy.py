"""Tests for @mcp_heavy decorator and the two-call response-negotiation protocol."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from friese_mcp.decorators import _merge_negotiation_schema, mcp_heavy
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import (
    McpEndpointView,
    _HEAVY_CACHE_PREFIX,
    _build_probe_envelope,
    _serve_heavy_mode,
)

_view = McpEndpointView.as_view()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(rf: RequestFactory, payload: Any) -> Any:
    return rf.post("/mcp/", data=json.dumps(payload), content_type="application/json")


def _jsonrpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _call_tool(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    request = _post(rf, _jsonrpc("tools/call", {"name": name, "arguments": arguments}))
    request.user = AnonymousUser()
    return _view(request)


def _response_data(response: Any) -> dict[str, Any]:
    return json.loads(response.content)  # type: ignore[no-any-return]


def _tool_result(response: Any) -> Any:
    data = _response_data(response)
    return json.loads(data["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# _merge_negotiation_schema
# ---------------------------------------------------------------------------


class TestMergeNegotiationSchema:
    """Unit tests for _merge_negotiation_schema."""

    def test_adds_negotiation_fields_to_object_schema(self) -> None:
        """Negotiation fields are merged into an object schema."""
        base = {"type": "object", "properties": {"query": {"type": "string"}}}
        merged = _merge_negotiation_schema(base)
        assert "continuation_token" in merged["properties"]
        assert "mode" in merged["properties"]
        assert "page" in merged["properties"]
        assert "page_size" in merged["properties"]
        assert "filter_keys" in merged["properties"]

    def test_preserves_original_properties(self) -> None:
        """Original schema properties survive the merge."""
        base = {"type": "object", "properties": {"query": {"type": "string"}}}
        merged = _merge_negotiation_schema(base)
        assert "query" in merged["properties"]

    def test_non_object_schema_returned_unchanged(self) -> None:
        """Non-object schemas are returned without modification."""
        base: dict[str, Any] = {"type": "string"}
        assert _merge_negotiation_schema(base) is base

    def test_removes_additional_properties_false(self) -> None:
        """additionalProperties: false is removed to allow negotiation fields."""
        base = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        merged = _merge_negotiation_schema(base)
        assert "additionalProperties" not in merged

    def test_preserves_required_array(self) -> None:
        """required array from original schema is preserved unchanged."""
        base = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        merged = _merge_negotiation_schema(base)
        assert merged.get("required") == ["q"]

    def test_mode_enum_contains_all_modes(self) -> None:
        """mode property includes all four negotiation modes."""
        merged = _merge_negotiation_schema({"type": "object", "properties": {}})
        modes = merged["properties"]["mode"]["enum"]
        assert set(modes) == {"summary", "paginated", "filtered", "full"}


# ---------------------------------------------------------------------------
# @mcp_heavy decorator — registration
# ---------------------------------------------------------------------------


class TestMcpHeavyDecorator:
    """Tests for the @mcp_heavy decorator itself."""

    def test_registers_with_is_heavy_true(self) -> None:
        """@mcp_heavy registers the tool with is_heavy=True."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(name="heavy.test", description="Heavy test", input_schema={"type": "object", "properties": {}})
            def _fn(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"data": "result"}

        entry = isolated.get_entry("heavy.test")
        assert entry is not None
        assert entry.is_heavy is True

    def test_returns_original_callable_unchanged(self) -> None:
        """@mcp_heavy returns the original function unmodified."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(name="heavy.ret", description="Return test", input_schema={"type": "object", "properties": {}})
            def _fn(_arguments: dict[str, Any], _request: Any) -> str:
                return "original"

            assert _fn({}, None) == "original"

    def test_schema_has_negotiation_fields(self) -> None:
        """The registered schema includes the merged negotiation fields."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(
                name="heavy.schema",
                description="Schema test",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> None:
                pass

        entry = isolated.get_entry("heavy.schema")
        assert entry is not None
        assert "continuation_token" in entry.input_schema.get("properties", {})
        assert "mode" in entry.input_schema.get("properties", {})

    def test_non_heavy_tool_has_is_heavy_false(self) -> None:
        """@mcp_tool registers with is_heavy=False by default."""
        from friese_mcp.decorators import mcp_tool

        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="light.test", description="Light test", input_schema={})
            def _fn(_arguments: dict[str, Any], _request: Any) -> None:
                pass

        entry = isolated.get_entry("light.test")
        assert entry is not None
        assert entry.is_heavy is False


# ---------------------------------------------------------------------------
# _build_probe_envelope
# ---------------------------------------------------------------------------


class TestBuildProbeEnvelope:
    """Unit tests for _build_probe_envelope."""

    def test_structure(self) -> None:
        """Probe envelope has all required fields."""
        env = _build_probe_envelope({"key": "value"}, "tok123")
        assert set(env.keys()) == {"preview", "total_size", "available_modes", "continuation_token"}

    def test_continuation_token(self) -> None:
        """Probe envelope contains the supplied token."""
        env = _build_probe_envelope({}, "mytoken")
        assert env["continuation_token"] == "mytoken"

    def test_available_modes(self) -> None:
        """available_modes lists all four modes."""
        env = _build_probe_envelope({}, "tok")
        assert set(env["available_modes"]) == {"summary", "paginated", "filtered", "full"}

    def test_total_size_for_dict(self) -> None:
        """total_size reflects the serialised byte count."""
        result = {"k": "v" * 100}
        env = _build_probe_envelope(result, "tok")
        assert env["total_size"] == len(json.dumps(result).encode())

    def test_preview_truncated_to_200(self) -> None:
        """preview is at most 200 chars."""
        env = _build_probe_envelope({"key": "x" * 1000}, "tok")
        assert len(env["preview"]) <= 200

    def test_list_result_preview(self) -> None:
        """List results use first 3 items as preview."""
        result = list(range(100))
        env = _build_probe_envelope(result, "tok")
        assert env["preview"] == json.dumps(result[:3])[:200]


# ---------------------------------------------------------------------------
# _serve_heavy_mode
# ---------------------------------------------------------------------------


class TestServeHeavyMode:
    """Unit tests for _serve_heavy_mode."""

    def test_full_mode_returns_complete_result(self) -> None:
        """full mode returns the entire cached result."""
        result = {"a": 1, "b": 2}
        assert _serve_heavy_mode(result, "full", {}) == result

    def test_unknown_mode_defaults_to_full(self) -> None:
        """An unrecognised mode falls back to full."""
        result = {"a": 1}
        assert _serve_heavy_mode(result, "bogus", {}) == result

    def test_summary_dict_truncates_values(self) -> None:
        """summary mode truncates dict values to 100 chars."""
        result = {"key": "x" * 200}
        served = _serve_heavy_mode(result, "summary", {})
        assert isinstance(served, dict)
        assert len(served["key"]) <= 100

    def test_summary_list_returns_first_five(self) -> None:
        """summary mode returns at most the first 5 list items."""
        result = list(range(50))
        served = _serve_heavy_mode(result, "summary", {})
        assert served == list(range(5))

    def test_summary_string_result(self) -> None:
        """summary mode wraps a string result in a dict."""
        served = _serve_heavy_mode("hello world", "summary", {})
        assert isinstance(served, dict)
        assert "summary" in served

    def test_paginated_list_first_page(self) -> None:
        """paginated mode returns the first page of a list."""
        result = list(range(100))
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 10})
        assert served["items"] == list(range(10))
        assert served["page"] == 1
        assert served["total"] == 100
        assert served["has_more"] is True

    def test_paginated_list_last_page(self) -> None:
        """paginated mode marks has_more=False on the final page."""
        result = list(range(15))
        served = _serve_heavy_mode(result, "paginated", {"page": 2, "page_size": 10})
        assert served["items"] == list(range(10, 15))
        assert served["has_more"] is False

    def test_paginated_non_list_chunks_json(self) -> None:
        """paginated mode chunks a non-list result by JSON string."""
        result = {"data": "x" * 500}
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 5})
        assert "chunk" in served
        assert "page" in served

    def test_filtered_dict_keeps_only_requested_keys(self) -> None:
        """filtered mode retains only the keys in filter_keys."""
        result = {"a": 1, "b": 2, "c": 3}
        served = _serve_heavy_mode(result, "filtered", {"filter_keys": ["a", "c"]})
        assert served == {"a": 1, "c": 3}

    def test_filtered_list_of_dicts(self) -> None:
        """filtered mode applies filter_keys to each dict item in a list."""
        result = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        served = _serve_heavy_mode(result, "filtered", {"filter_keys": ["a"]})
        assert served == [{"a": 1}, {"a": 3}]

    def test_filtered_no_keys_returns_full(self) -> None:
        """filtered mode with no filter_keys returns the original result."""
        result = {"a": 1}
        assert _serve_heavy_mode(result, "filtered", {}) == result


# ---------------------------------------------------------------------------
# Integration: @mcp_heavy via views
# ---------------------------------------------------------------------------


class TestMcpHeavyIntegration:
    """End-to-end tests for @mcp_heavy via the McpView endpoint."""

    @pytest.fixture()
    def rf(self) -> RequestFactory:
        return RequestFactory()

    @pytest.fixture()
    def heavy_registry(self) -> ToolRegistry:
        isolated = ToolRegistry()
        return isolated

    def test_call1_returns_probe_envelope(self, rf: RequestFactory) -> None:
        """Call 1 (no continuation_token) returns a probe envelope."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_heavy(
                name="int.heavy1",
                description="Heavy integration",
                input_schema={"type": "object", "properties": {}},
            )
            def _big_tool(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"data": list(range(50))}

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                response = _call_tool(rf, "int.heavy1", {})

        result = _tool_result(response)
        assert "continuation_token" in result
        assert "preview" in result
        assert "total_size" in result
        assert result["available_modes"] == ["summary", "paginated", "filtered", "full"]

    def test_call2_full_mode_returns_original(self, rf: RequestFactory) -> None:
        """Call 2 with mode=full returns the complete cached result."""
        stored = {"big": "payload", "items": list(range(20))}
        token = "testtoken123"

        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_heavy(
                name="int.heavy2",
                description="Heavy call2",
                input_schema={"type": "object", "properties": {}},
            )
            def _big2(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return stored

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = stored
                response = _call_tool(
                    rf, "int.heavy2", {"continuation_token": token, "mode": "full"}
                )

        result = _tool_result(response)
        assert result == stored

    def test_call2_summary_mode(self, rf: RequestFactory) -> None:
        """Call 2 with mode=summary returns a condensed result."""
        stored = {f"key{i}": "x" * 200 for i in range(20)}
        token = "sumtoken"

        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_heavy(
                name="int.heavy3",
                description="Heavy summary",
                input_schema={"type": "object", "properties": {}},
            )
            def _big3(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return stored

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = stored
                response = _call_tool(
                    rf, "int.heavy3", {"continuation_token": token, "mode": "summary"}
                )

        result = _tool_result(response)
        assert isinstance(result, dict)
        for v in result.values():
            assert len(str(v)) <= 100

    def test_expired_token_returns_error(self, rf: RequestFactory) -> None:
        """An expired or unknown continuation_token returns isError=True."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_heavy(
                name="int.heavy4",
                description="Heavy expired",
                input_schema={"type": "object", "properties": {}},
            )
            def _big4(_arguments: dict[str, Any], _request: Any) -> None:
                pass

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None  # cache miss — token expired
                response = _call_tool(
                    rf, "int.heavy4", {"continuation_token": "deadtoken", "mode": "full"}
                )

        data = _response_data(response)
        assert data["result"]["isError"] is True
        text = json.loads(data["result"]["content"][0]["text"])
        assert "expired" in text["error"].lower() or "not found" in text["error"].lower()

    @override_settings(FRIESE_MCP_AUTO_NEGOTIATE_THRESHOLD=50)
    def test_threshold_backstop_wraps_large_response(self, rf: RequestFactory) -> None:
        """FRIESE_MCP_AUTO_NEGOTIATE_THRESHOLD wraps large non-heavy tool responses."""
        from friese_mcp.decorators import mcp_tool

        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_tool(
                name="int.light",
                description="Light tool with big response",
                input_schema={"type": "object", "properties": {}},
            )
            def _light(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"data": "x" * 1000}  # ~1 KB > 50-byte threshold

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                mock_cache.set = MagicMock()
                response = _call_tool(rf, "int.light", {})

        result = _tool_result(response)
        assert "continuation_token" in result, "Expected probe envelope from threshold backstop"

    @override_settings(FRIESE_MCP_AUTO_NEGOTIATE_THRESHOLD=100000)
    def test_threshold_backstop_passthrough_for_small_response(self, rf: RequestFactory) -> None:
        """A small response below the threshold passes through unchanged."""
        from friese_mcp.decorators import mcp_tool

        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated), patch(
            "friese_mcp.views.tool_registry", isolated
        ):

            @mcp_tool(
                name="int.small",
                description="Small response tool",
                input_schema={"type": "object", "properties": {}},
            )
            def _small(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"ok": True}

            with patch("friese_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                response = _call_tool(rf, "int.small", {})

        result = _tool_result(response)
        assert result == {"ok": True}
        assert "continuation_token" not in result
