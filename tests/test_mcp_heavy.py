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


def _build_call_tool_request(
    rf: RequestFactory, name: str, arguments: dict[str, Any]
) -> Any:
    """
    Build the same request that ``_call_tool`` would dispatch — but without firing the view.

    SEC-3 tests need to compute the owner_key for the call-2 request so the
    mocked cache can return a payload with a matching binding.  Calling
    ``_call_tool`` consumes the request; this helper exposes it for inspection.
    """
    request = _post(rf, _jsonrpc("tools/call", {"name": name, "arguments": arguments}))
    request.user = AnonymousUser()
    return request


def _call_tool(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    return _view(_build_call_tool_request(rf, name, arguments))


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
        """Required array from original schema is preserved unchanged."""
        base = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        merged = _merge_negotiation_schema(base)
        assert merged.get("required") == ["q"]

    def test_mode_enum_contains_all_modes(self) -> None:
        """Mode property includes all four negotiation modes."""
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

            @mcp_heavy(
                name="heavy.test",
                description="Heavy test",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"data": "result"}

        entry = isolated.get_entry("heavy.test")
        assert entry is not None
        assert entry.is_heavy is True

    def test_returns_original_callable_unchanged(self) -> None:
        """@mcp_heavy returns the original function unmodified."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(
                name="heavy.ret",
                description="Return test",
                input_schema={"type": "object", "properties": {}},
            )
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
        """Preview is at most 200 chars."""
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
        """Full mode returns the entire cached result."""
        result = {"a": 1, "b": 2}
        assert _serve_heavy_mode(result, "full", {}) == result

    def test_unknown_mode_defaults_to_full(self) -> None:
        """An unrecognised mode falls back to full."""
        result = {"a": 1}
        assert _serve_heavy_mode(result, "bogus", {}) == result

    def test_summary_dict_truncates_values(self) -> None:
        """Summary mode truncates dict values to 100 chars."""
        result = {"key": "x" * 200}
        served = _serve_heavy_mode(result, "summary", {})
        assert isinstance(served, dict)
        assert len(served["key"]) <= 100

    def test_summary_list_returns_first_five(self) -> None:
        """Summary mode returns at most the first 5 list items."""
        result = list(range(50))
        served = _serve_heavy_mode(result, "summary", {})
        assert served == list(range(5))

    def test_summary_string_result(self) -> None:
        """Summary mode wraps a string result in a dict."""
        served = _serve_heavy_mode("hello world", "summary", {})
        assert isinstance(served, dict)
        assert "summary" in served

    def test_paginated_list_first_page(self) -> None:
        """Paginated mode returns the first page of a list."""
        result = list(range(100))
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 10})
        assert served["items"] == list(range(10))
        assert served["page"] == 1
        assert served["total"] == 100
        assert served["has_more"] is True

    def test_paginated_list_last_page(self) -> None:
        """Paginated mode marks has_more=False on the final page."""
        result = list(range(15))
        served = _serve_heavy_mode(result, "paginated", {"page": 2, "page_size": 10})
        assert served["items"] == list(range(10, 15))
        assert served["has_more"] is False

    def test_paginated_non_list_chunks_json(self) -> None:
        """Paginated mode chunks a non-list result by JSON string."""
        result = {"data": "x" * 500}
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 5})
        assert "chunk" in served
        assert "page" in served

    def test_filtered_dict_keeps_only_requested_keys(self) -> None:
        """Filtered mode retains only the keys in filter_keys."""
        result = {"a": 1, "b": 2, "c": 3}
        served = _serve_heavy_mode(result, "filtered", {"filter_keys": ["a", "c"]})
        assert served == {"a": 1, "c": 3}

    def test_filtered_list_of_dicts(self) -> None:
        """Filtered mode applies filter_keys to each dict item in a list."""
        result = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        served = _serve_heavy_mode(result, "filtered", {"filter_keys": ["a"]})
        assert served == [{"a": 1}, {"a": 3}]

    def test_filtered_no_keys_returns_full(self) -> None:
        """Filtered mode with no filter_keys returns the original result."""
        result = {"a": 1}
        assert _serve_heavy_mode(result, "filtered", {}) == result


# ---------------------------------------------------------------------------
# Integration: @mcp_heavy via views
# ---------------------------------------------------------------------------


class TestMcpHeavyIntegration:
    """End-to-end tests for @mcp_heavy via the McpView endpoint."""

    @pytest.fixture()
    def rf(self) -> RequestFactory:
        """Return a Django RequestFactory."""
        return RequestFactory()

    @pytest.fixture()
    def heavy_registry(self) -> ToolRegistry:
        """Return an isolated ToolRegistry for test isolation."""
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
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

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
                # SEC-3: cache entries are now {result, owner_key, tool_name}.
                # The test request is anonymous so the owner_key for call 1
                # and call 2 are identical — derive it from the same request
                # the view will see.
                expected_owner = _heavy_owner_key(
                    _build_call_tool_request(rf, "int.heavy2", {}), "int.heavy2"
                )
                mock_cache.get.return_value = {
                    "result": stored,
                    "owner_key": expected_owner,
                    "tool_name": "int.heavy2",
                }
                response = _call_tool(
                    rf, "int.heavy2", {"continuation_token": token, "mode": "full"}
                )

        result = _tool_result(response)
        assert result == stored

    def test_call2_summary_mode(self, rf: RequestFactory) -> None:
        """Call 2 with mode=summary returns a condensed result."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

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
                expected_owner = _heavy_owner_key(
                    _build_call_tool_request(rf, "int.heavy3", {}), "int.heavy3"
                )
                mock_cache.get.return_value = {
                    "result": stored,
                    "owner_key": expected_owner,
                    "tool_name": "int.heavy3",
                }
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


# ---------------------------------------------------------------------------
# SEC-3 — continuation tokens bound to caller / tool / session
# ---------------------------------------------------------------------------


class TestHeavyContinuationOwnerBinding:
    """Continuation tokens must not be replayable across callers or tools."""

    @staticmethod
    def _isolated_registry_with_heavy(name: str, payload: Any) -> ToolRegistry:
        """Register a single ``@mcp_heavy`` tool that returns *payload*."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(
                name=name,
                description="SEC-3 binding test",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(  # pylint: disable=unused-variable
                _arguments: dict[str, Any], _request: Any
            ) -> Any:
                return payload

        return isolated

    def test_call1_writes_owner_bound_cache_entry(self, rf: RequestFactory) -> None:
        """Call 1 stores ``{result, owner_key, tool_name}`` (not the raw result)."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        payload = {"big": list(range(20))}
        reg = self._isolated_registry_with_heavy("sec3.heavy1", payload)

        with patch("friese_mcp.views.tool_registry", reg), patch(
            "friese_mcp.views.django_cache"
        ) as mock_cache:
            mock_cache.get.return_value = None
            _call_tool(rf, "sec3.heavy1", {})

        # The view called cache.set exactly once with the wrapped payload.
        assert mock_cache.set.call_count == 1
        _key, written, _ttl = mock_cache.set.call_args.args
        assert written["result"] == payload
        assert written["tool_name"] == "sec3.heavy1"
        # The owner key matches the canonical helper for the same request shape.
        expected = _heavy_owner_key(
            _build_call_tool_request(rf, "sec3.heavy1", {}), "sec3.heavy1"
        )
        assert written["owner_key"] == expected

    def test_call2_owner_mismatch_returns_is_error(self, rf: RequestFactory) -> None:
        """
        A continuation token issued for caller A is refused for caller B.

        Simulated by mocking the cache to return a wrapped entry whose
        ``owner_key`` does not match what the current request produces.
        """
        reg = self._isolated_registry_with_heavy("sec3.heavy2", {"x": 1})

        with patch("friese_mcp.views.tool_registry", reg), patch(
            "friese_mcp.views.django_cache"
        ) as mock_cache:
            mock_cache.get.return_value = {
                "result": {"x": 1},
                # Deliberately-foreign owner — different tier.
                "owner_key": "tool=sec3.heavy2:auth=anon:tier=admin",
                "tool_name": "sec3.heavy2",
            }
            response = _call_tool(
                rf,
                "sec3.heavy2",
                {"continuation_token": "stolen-token", "mode": "full"},
            )

        result = _tool_result(response)
        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    def test_call2_tool_name_mismatch_returns_is_error(
        self, rf: RequestFactory
    ) -> None:
        """
        A token issued for tool A cannot be replayed against tool B.

        Tool name is part of the owner key; computing it for the call-2
        tool yields a different key than the one stored at issuance, so
        the gate refuses.
        """
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_heavy(
                name="sec3.heavy3",
                description="x",
                input_schema={"type": "object", "properties": {}},
            )
            def _heavy3(  # pylint: disable=unused-variable
                _arguments: dict[str, Any], _request: Any
            ) -> Any:
                return {}

            @mcp_heavy(
                name="sec3.evil",
                description="y",
                input_schema={"type": "object", "properties": {}},
            )
            def _evil(  # pylint: disable=unused-variable
                _arguments: dict[str, Any], _request: Any
            ) -> Any:
                return {}

        # Token was issued under sec3.heavy3 …
        owner_for_heavy3 = _heavy_owner_key(
            _build_call_tool_request(rf, "sec3.heavy3", {}), "sec3.heavy3"
        )

        with patch("friese_mcp.views.tool_registry", reg), patch(
            "friese_mcp.views.django_cache"
        ) as mock_cache:
            mock_cache.get.return_value = {
                "result": {"sensitive": "data"},
                "owner_key": owner_for_heavy3,
                "tool_name": "sec3.heavy3",
            }
            # … but the call-2 names sec3.evil.
            response = _call_tool(
                rf,
                "sec3.evil",
                {"continuation_token": "x", "mode": "full"},
            )

        result = _tool_result(response)
        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    def test_call2_legacy_raw_entry_treated_as_expired(
        self, rf: RequestFactory
    ) -> None:
        """
        A pre-fix raw cache entry (no owner_key) is treated as expired.

        Existing cached entries from before the SEC-3 deploy have the legacy
        shape — bare result, no binding.  Serving them would defeat the
        whole fix; rejecting them as expired forces re-issuance under the
        new owner-bound format.
        """
        reg = self._isolated_registry_with_heavy("sec3.heavy4", {"legacy": True})

        with patch("friese_mcp.views.tool_registry", reg), patch(
            "friese_mcp.views.django_cache"
        ) as mock_cache:
            # Legacy shape: raw result, no wrapper.
            mock_cache.get.return_value = {"legacy": True}
            response = _call_tool(
                rf,
                "sec3.heavy4",
                {"continuation_token": "x", "mode": "full"},
            )

        result = _tool_result(response)
        assert "error" in result
        assert "expired or not found" in result["error"]

    def test_call2_owner_match_serves_cached_result(
        self, rf: RequestFactory
    ) -> None:
        """The happy path: matching owner_key → cached result is served."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        reg = self._isolated_registry_with_heavy("sec3.heavy5", {"ok": True})
        owner = _heavy_owner_key(
            _build_call_tool_request(rf, "sec3.heavy5", {}), "sec3.heavy5"
        )

        with patch("friese_mcp.views.tool_registry", reg), patch(
            "friese_mcp.views.django_cache"
        ) as mock_cache:
            mock_cache.get.return_value = {
                "result": {"ok": True},
                "owner_key": owner,
                "tool_name": "sec3.heavy5",
            }
            response = _call_tool(
                rf,
                "sec3.heavy5",
                {"continuation_token": "x", "mode": "full"},
            )

        result = _tool_result(response)
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# SEC-3 — _heavy_owner_key composition unit probes
# ---------------------------------------------------------------------------


class TestHeavyOwnerKey:
    """Unit-level probes for _heavy_owner_key composition."""

    @staticmethod
    def _request(rf: RequestFactory) -> Any:
        return _build_call_tool_request(rf, "x.list", {})

    def test_anonymous_request_includes_anon_marker(self, rf: RequestFactory) -> None:
        """An unauthenticated request renders auth=anon in the owner key."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        key = _heavy_owner_key(self._request(rf), "x.list")
        assert "auth=anon" in key
        assert "tool=x.list" in key

    def test_different_tools_produce_different_keys(self, rf: RequestFactory) -> None:
        """Tool name is part of the key; two tools yield different bindings."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        a = _heavy_owner_key(self._request(rf), "tool.a")
        b = _heavy_owner_key(self._request(rf), "tool.b")
        assert a != b

    def test_session_id_header_appears_in_key(self, rf: RequestFactory) -> None:
        """A request carrying MCP-Session-ID has the session bound into the key."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        request = self._request(rf)
        request.META["HTTP_MCP_SESSION_ID"] = "session-xyz-123"
        key = _heavy_owner_key(request, "x.list")
        assert "session=session-xyz-123" in key

    def test_tier_change_changes_the_key(self, rf: RequestFactory) -> None:
        """A token whose tier later downgrades produces a different owner key."""
        from friese_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        request = self._request(rf)
        # First snapshot under tier=read_write.
        auth = MagicMock()
        auth.permission = "read_write"
        request.auth = auth
        key_rw = _heavy_owner_key(request, "x.list")

        # Then the same auth object, downgraded tier.
        auth.permission = "read"
        key_r = _heavy_owner_key(request, "x.list")

        assert key_rw != key_r
