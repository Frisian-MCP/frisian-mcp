"""Tests for @mcp_heavy decorator and the two-call response-negotiation protocol."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.decorators import _merge_negotiation_schema, mcp_heavy
from frisian_mcp.negotiation import schema_discloses_continuation
from frisian_mcp.registry import ToolInputError, ToolRegistry
from frisian_mcp.views import (
    _HEAVY_CACHE_PREFIX,
    McpView,
    _build_probe_envelope,
    _serve_heavy_mode,
)

_view = McpView.as_view()


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


def _build_call_tool_request(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
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

    def test_preserves_additional_properties_false(self) -> None:
        """
        H20 INVERTED: ``additionalProperties: false`` is now PRESERVED.

        This asserted the opposite, on the rationale that the restriction "is
        removed to allow negotiation fields".  The premise was wrong: this merge
        declares the five fields in the **same** ``properties`` object that
        ``additionalProperties`` is evaluated against, so they are permitted by
        construction and nothing needed removing.

        Deleting it was a real weakening, not a formality — ``dispatch``
        validates against the published schema, and ``@mcp_heavy`` carries the
        *host's* ``input_schema``, so a host that asked for strictness lost it.
        """
        base = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        merged = _merge_negotiation_schema(base)
        assert merged["additionalProperties"] is False
        # ...and the negotiation fields are still reachable alongside it.
        assert "continuation_token" in merged["properties"]
        assert "mode" in merged["properties"]

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
        with patch("frisian_mcp.decorators.tool_registry", isolated):

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
        with patch("frisian_mcp.decorators.tool_registry", isolated):

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
        with patch("frisian_mcp.decorators.tool_registry", isolated):

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
        from frisian_mcp.decorators import mcp_tool

        isolated = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", isolated):

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
        assert set(env.keys()) == {
            "preview",
            "total_size",
            "available_modes",
            "continuation_token",
            "usage",
        }

    def test_usage_discloses_placement_and_cost_of_omitting_mode(self) -> None:
        """
        T6: the envelope that advertises the modes must also teach their placement.

        An agent mid-negotiation is not re-reading ``tools/list``, so advertising
        ``available_modes`` without saying where the fields go is what made all
        four modes look unreachable.

        **Updated on purpose: B2 changed the default.**  This previously
        required the envelope to say that omitting ``mode`` returns the
        complete dataset, on the reasoning that disclosure was the only
        guardrail while ``full`` stayed reachable by omission.  Under B2 the
        default is bounded, so the guardrail is the behaviour itself and the
        envelope must instead teach that ``full`` has to be requested.
        """
        env = _build_probe_envelope({"key": "value" * 100}, "tok123")
        usage = env["usage"]
        assert "TOP LEVEL" in usage
        assert "'params'" in usage
        assert "ONE PAGE" in usage
        assert "mode='full'" in usage
        # Positive assertions alone would pass on an envelope containing BOTH
        # the current text and the obsolete pre-B2 default — a self-
        # contradicting envelope is worse than a stale one, because the caller
        # cannot tell which sentence to believe mid-negotiation.  The matrix
        # carries this negative guard; this file did not, so it is added here
        # rather than left to the other file to catch.
        assert "COMPLETE dataset" not in usage, "pre-B2 wording resurfaced"
        # The concrete byte cost of the full response is named, not just implied.
        assert str(env["total_size"]) in usage

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

    def test_unknown_mode_is_rejected(self) -> None:
        """
        An unrecognised mode raises rather than falling back.

        **Updated on purpose: B2 changed this.**  This previously asserted the
        opposite — that a bogus mode silently returned the complete result.
        Serving the most expensive response for a typo hides the mistake from
        the caller, which is precisely what the ADR-005 item (b) ruling
        rejected.  The error names the public enum so the caller can correct
        it, and nothing beyond it.
        """
        result = {"a": 1}
        with pytest.raises(ToolInputError) as exc:
            _serve_heavy_mode(result, "bogus", {})
        for mode in ("summary", "paginated", "filtered", "full"):
            assert mode in str(exc.value)

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

    def test_paginated_rejects_non_numeric_page_values_as_caller_error(self) -> None:
        """
        Bad ``page`` / ``page_size`` must be a caller error, not a 500.

        Redemption short-circuits schema validation by design — call 2 needs
        only the token and a mode — so these arrive unvalidated, and the
        redemption path catches only ``ToolInputError``.  A non-numeric value
        escaped ``int()`` as TypeError/ValueError and surfaced as a server
        error for what is plainly a malformed request.
        """
        for bad in ({"page": "abc"}, {"page_size": None}, {"page": [1]}):
            with pytest.raises(ToolInputError):
                _serve_heavy_mode([1, 2, 3], "paginated", bad)

    def test_paginated_honours_page_size_on_a_list_envelope(self) -> None:
        """
        H23, the reported bug: asked for 2 records, received the whole page.

        A paginated list envelope is a **dict**, so T18's "a non-list result is
        already bounded" guard sent the single most common heavy case — a large
        list endpoint — straight down the return-whole path with ``page_size``
        never read.  T18 enumerated the write result and the single-object
        retrieve and missed the envelope, which is the dominant shape in
        production.

        Worse than the behaviour it replaced: before T18 a dict was chunked into
        fixed-width slices — unusable, but *bounded*.  After, it was parseable
        and *unbounded*, on exactly the case the package exists to bound.
        """
        envelope = {
            "count": 114,
            "next": "http://host/api/thing/?page=2",
            "previous": None,
            "results": [{"id": i} for i in range(50)],
        }
        served = _serve_heavy_mode(envelope, "paginated", {"page": 1, "page_size": 2})
        assert len(served["items"]) == 2, "page_size was ignored on a list envelope"
        assert served["total"] == 50
        assert served["has_more"] is True

    def test_paginated_envelope_walks_pages(self) -> None:
        """``page`` selects a slice of the envelope's payload, not of the envelope."""
        envelope = {"count": 9, "results": [{"id": i} for i in range(9)]}
        served = _serve_heavy_mode(envelope, "paginated", {"page": 2, "page_size": 3})
        assert [item["id"] for item in served["items"]] == [3, 4, 5]

    def test_paginated_envelope_nests_the_hosts_own_numbers(self) -> None:
        """
        The host's pagination keys are preserved but NOT merged with ours.

        ``count`` describes the host's full result set, ``total`` describes the
        cached page we are slicing, and ``items`` is the slice — three numbers
        measuring three different things.  Side by side at one level they read
        as contradictory, so the host's are nested under ``envelope`` and the
        payload key is named rather than assumed.
        """
        envelope = {"count": 114, "next": "http://host/x?page=2", "results": [{"id": 1}]}
        served = _serve_heavy_mode(envelope, "paginated", {"page": 1, "page_size": 1})
        assert served["envelope"] == {"count": 114, "next": "http://host/x?page=2"}
        assert served["envelope_payload_key"] == "results"
        assert "count" not in served, "host and continuation numbers must not sit side by side"

    def test_paginated_ambiguous_envelope_returns_whole(self) -> None:
        """
        Two list-valued keys is not an envelope we can safely paginate.

        Guessing which list is the payload would silently return a slice of the
        wrong collection, which is worse than returning too much.  This is the
        pre-H23 behaviour, kept deliberately for the shape that cannot be
        resolved without naming a host field.
        """
        ambiguous = {"a": [1, 2, 3], "b": [4, 5, 6]}
        assert _serve_heavy_mode(ambiguous, "paginated", {"page": 1, "page_size": 1}) == ambiguous

    def test_paginated_envelope_detection_names_no_host_field(self) -> None:
        """
        Detection is structural, so a host using any payload key works.

        Asserted across three vocabularies to pin that no field name is
        hard-coded — ``results`` is DRF's convention, not this package's.
        """
        for key in ("results", "items", "records"):
            envelope = {"count": 3, key: [{"id": i} for i in range(3)]}
            served = _serve_heavy_mode(envelope, "paginated", {"page": 1, "page_size": 1})
            assert len(served["items"]) == 1, f"payload key {key!r} not detected"
            assert served["envelope_payload_key"] == key

    def test_paginated_non_list_returns_it_whole(self) -> None:
        """
        Paginated mode returns a non-list result whole, chunking nothing.

        **Updated on purpose: T18 changed this.**  This previously asserted the
        opposite — that a non-list result was sliced into fixed-width pieces of
        its JSON serialisation.  Those slices cut at an arbitrary offset,
        usually mid-token, so the caller got neither the object nor anything
        parseable as one.

        Harmless while ``paginated`` had to be requested explicitly; B2 made it
        the default for a bare token, and since read and write share one cache
        prefix and one redemption path, a bare write-token redemption began
        returning a truncated string for the object just created.  A single
        object is already bounded, so it is returned whole; ``summary`` and
        ``filtered`` remain available for bounding a large one.

        Explicit ``mode='paginated'`` on a non-list gets the same treatment as
        the bare default — deliberately, so the mode means one thing.
        """
        result = {"data": "x" * 500}
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 5})
        assert served == result
        assert "chunk" not in served

    # -- CL-6 / GH #66: single_object suppresses the envelope lookup ---------

    def test_single_object_with_one_list_field_is_not_paginated(self) -> None:
        """
        A created object's only list field must not be served as the payload.

        Without the mint site's statement, this dict satisfies the envelope
        test — exactly one list-valued key — and that field becomes the
        payload, empty on a fresh create.  The caller then reads ``total: 0``
        for an object that was successfully written.
        """
        created = {"id": "uuid-1", "name": "thing", "labels": []}

        assert _serve_heavy_mode(created, "paginated", {}, single_object=True) == created

    def test_same_shape_without_the_flag_keeps_todays_behaviour(self) -> None:
        """
        The default is ``False`` and must not change what an existing entry does.

        This is the pre-deploy entry read with ``.get()``: identical input,
        flag absent, envelope treatment retained.  Pinned so the default can
        never be flipped without a test going red — flipping it would stop
        in-flight READ entries paginating for the rest of their TTL.
        """
        created = {"id": "uuid-1", "name": "thing", "labels": []}

        served = _serve_heavy_mode(created, "paginated", {})

        assert served["envelope_payload_key"] == "labels"
        assert served["total"] == 0

    def test_list_envelope_still_paginates_the_h23_guard(self) -> None:
        """
        The H23 fix is untouched — this is the real regression risk.

        H23 was correct: a large list endpoint is a dict and must page.  CL-6
        must narrow only the single-object case, so a genuine envelope keeps
        paging exactly as before, flag absent AND flag present-but-false.
        """
        envelope = {"count": 114, "results": [{"id": i} for i in range(50)]}

        for kwargs in ({}, {"single_object": False}):
            served = _serve_heavy_mode(envelope, "paginated", {"page": 2, "page_size": 3}, **kwargs)
            assert [item["id"] for item in served["items"]] == [3, 4, 5]
            assert served["total"] == 50
            assert served["envelope_payload_key"] == "results"

    def test_single_object_flag_does_not_stop_a_bulk_write_paginating(self) -> None:
        """
        A bulk write caches a **list**, and paginating that is correct.

        ``single_object`` suppresses the envelope-key lookup for a dict; it is
        deliberately not a blanket "never paginate a write", or bulk creates
        would start returning every object at once.
        """
        created = [{"id": i} for i in range(10)]

        served = _serve_heavy_mode(
            created, "paginated", {"page": 1, "page_size": 4}, single_object=True
        )

        assert len(served["items"]) == 4
        assert served["total"] == 10
        assert served["has_more"] is True

    def test_single_object_leaves_the_other_modes_alone(self) -> None:
        """``single_object`` touches ``paginated`` only."""
        created = {"id": "uuid-1", "name": "thing", "labels": []}

        assert _serve_heavy_mode(created, "full", {}, single_object=True) == created
        assert _serve_heavy_mode(
            created, "filtered", {"filter_keys": ["id"]}, single_object=True
        ) == {"id": "uuid-1"}
        assert _serve_heavy_mode(created, "summary", {}, single_object=True) == {
            "id": "uuid-1",
            "name": "thing",
            "labels": "[]",
        }

    def test_paginated_page_size_clamped_to_max(self, settings: Any) -> None:
        """Agent-supplied page_size above FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE is clamped."""
        settings.FRISIAN_MCP_HEAVY_PAGE_SIZE = 20
        settings.FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE = 50
        result = list(range(200))
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 1000000})
        assert served["page_size"] == 50
        assert len(served["items"]) == 50

    def test_paginated_page_size_cap_defaults_to_heavy_page_size(self, settings: Any) -> None:
        """When MAX_PAGE_SIZE is absent, the cap defaults to FRISIAN_MCP_HEAVY_PAGE_SIZE."""
        settings.FRISIAN_MCP_HEAVY_PAGE_SIZE = 10
        if hasattr(settings, "FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE"):
            del settings.FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE
        result = list(range(200))
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 9999})
        assert served["page_size"] == 10
        assert len(served["items"]) == 10

    def test_paginated_page_size_within_cap_unchanged(self, settings: Any) -> None:
        """page_size below the cap is returned as-is."""
        settings.FRISIAN_MCP_HEAVY_MAX_PAGE_SIZE = 100
        result = list(range(200))
        served = _serve_heavy_mode(result, "paginated", {"page": 1, "page_size": 25})
        assert served["page_size"] == 25
        assert len(served["items"]) == 25

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
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_heavy(
                name="int.heavy1",
                description="Heavy integration",
                input_schema={"type": "object", "properties": {}},
            )
            def _big_tool(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"data": list(range(50))}

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                response = _call_tool(rf, "int.heavy1", {})

        result = _tool_result(response)
        assert "continuation_token" in result
        assert "preview" in result
        assert "total_size" in result
        assert result["available_modes"] == ["summary", "paginated", "filtered", "full"]

    def test_call2_full_mode_returns_original(self, rf: RequestFactory) -> None:
        """Call 2 with mode=full returns the complete cached result."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {"big": "payload", "items": list(range(20))}
        token = "testtoken123"

        isolated = ToolRegistry()
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_heavy(
                name="int.heavy2",
                description="Heavy call2",
                input_schema={"type": "object", "properties": {}},
            )
            def _big2(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return stored

            with patch("frisian_mcp.views.django_cache") as mock_cache:
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
                    "resolved_target": "int.heavy2",
                }
                response = _call_tool(
                    rf, "int.heavy2", {"continuation_token": token, "mode": "full"}
                )

        result = _tool_result(response)
        assert result == stored

    def test_call2_read_entry_without_the_flag_still_paginates(self, rf: RequestFactory) -> None:
        """
        CL-6: a READ entry carrying no ``single_object`` key must still paginate.

        This pins the **redemption call site's** ``.get(..., False)`` default,
        which the ``_serve_heavy_mode`` unit tests cannot reach — they pass the
        flag directly. Two populations depend on this default and both are
        represented by the entry below, which deliberately omits the key:

        * every entry minted before this shipped, still live for its TTL
        * every read entry, which never sets it

        Flipping that default to ``True`` would stop large list endpoints
        paginating — the dominant production path — which is a worse fault than
        the one CL-6 fixes. The stored result is a genuine list envelope with
        exactly one list-valued key, i.e. the shape that would be misread as a
        single object if the default went the other way.
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {"count": 114, "results": [{"id": i} for i in range(50)]}
        token = "readentrytoken"

        isolated = ToolRegistry()
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_heavy(
                name="int.heavy.readentry",
                description="Heavy read entry",
                input_schema={"type": "object", "properties": {}},
            )
            def _big_env(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return stored

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                expected_owner = _heavy_owner_key(
                    _build_call_tool_request(rf, "int.heavy.readentry", {}),
                    "int.heavy.readentry",
                )
                # No "single_object" key at all — a pre-deploy or read entry.
                mock_cache.get.return_value = {
                    "result": stored,
                    "owner_key": expected_owner,
                    "tool_name": "int.heavy.readentry",
                    "resolved_target": "int.heavy.readentry",
                }
                response = _call_tool(
                    rf,
                    "int.heavy.readentry",
                    {"continuation_token": token, "mode": "paginated", "page_size": 2},
                )

        result = _tool_result(response)
        assert result["envelope_payload_key"] == "results", "read entry stopped paginating"
        assert len(result["items"]) == 2, "page_size ignored on a read entry"
        assert result["total"] == 50

    def test_call2_summary_mode(self, rf: RequestFactory) -> None:
        """Call 2 with mode=summary returns a condensed result."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {f"key{i}": "x" * 200 for i in range(20)}
        token = "sumtoken"

        isolated = ToolRegistry()
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_heavy(
                name="int.heavy3",
                description="Heavy summary",
                input_schema={"type": "object", "properties": {}},
            )
            def _big3(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return stored

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                expected_owner = _heavy_owner_key(
                    _build_call_tool_request(rf, "int.heavy3", {}), "int.heavy3"
                )
                mock_cache.get.return_value = {
                    "result": stored,
                    "owner_key": expected_owner,
                    "tool_name": "int.heavy3",
                    "resolved_target": "int.heavy3",
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
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_heavy(
                name="int.heavy4",
                description="Heavy expired",
                input_schema={"type": "object", "properties": {}},
            )
            def _big4(_arguments: dict[str, Any], _request: Any) -> None:
                pass

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None  # cache miss — token expired
                response = _call_tool(
                    rf, "int.heavy4", {"continuation_token": "deadtoken", "mode": "full"}
                )

        data = _response_data(response)
        assert data["result"]["isError"] is True
        text = json.loads(data["result"]["content"][0]["text"])
        assert "expired" in text["error"].lower() or "not found" in text["error"].lower()

    @override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=50)
    def test_threshold_backstop_returns_a_non_disclosing_tool_whole(
        self, rf: RequestFactory
    ) -> None:
        """
        CR-2: an over-threshold ``@mcp_tool`` response comes back WHOLE, and pins nothing.

        This test asserted the opposite until CR-2 — that the backstop wrapped
        any large response, including an ordinary tool's.  That behaviour is
        what H2 then had to pay for by disclosing the continuation call on every
        schema.  Reversing it is the point of CR-2: ``@mcp_tool`` no longer
        discloses, ``schema_discloses_continuation()`` gates the mint, so the
        backstop declines rather than issuing a token the caller's published
        schema never mentioned.

        "Declines" has to mean the full payload, and this is the assertion that
        pins it.  A backstop that quietly truncated or dropped an over-threshold
        response it had decided not to negotiate would be a worse defect than
        the schema bloat CR-2 removes, and it would be invisible to any
        assertion that only checked for the absence of a token.  So both halves
        are checked: the exact bytes come back, and nothing is written to the
        heavy cache — measured off ``django_cache.set`` rather than
        reconstructed, because an entry pinned for 300s in the shared default
        cache is a cost even when no caller ever sees the token.

        The backstop is NOT disabled by this.  It still mints for every shape
        that discloses — ``@mcp_heavy`` and the dispatchers — which the tests
        around this one cover.
        """
        from frisian_mcp.decorators import mcp_tool

        payload = {"data": "x" * 1000}  # ~1 KB, far over the 50-byte threshold

        isolated = ToolRegistry()
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_tool(
                name="int.light",
                description="Light tool with big response",
                input_schema={"type": "object", "properties": {}},
            )
            def _light(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return dict(payload)

            entry = isolated.get_entry("int.light")
            assert entry is not None
            # The precondition the mint gate reads.  Asserted here so that if a
            # future change re-disclosed on this path, this test fails on the
            # cause rather than on the consequence.
            assert schema_discloses_continuation(entry.input_schema) is False

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                mock_cache.set = MagicMock()
                response = _call_tool(rf, "int.light", {})

                heavy_writes = [
                    call
                    for call in mock_cache.set.call_args_list
                    if str(call.args[0]).startswith(_HEAVY_CACHE_PREFIX)
                ]

        result = _tool_result(response)
        assert "continuation_token" not in result, "Undisclosed shape must not be handed a token"
        assert result == payload, "Over-threshold non-disclosing response must be returned WHOLE"
        assert heavy_writes == [], f"Nothing may be pinned in the heavy cache: {heavy_writes}"

    @override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=100000)
    def test_threshold_backstop_passthrough_for_small_response(self, rf: RequestFactory) -> None:
        """A small response below the threshold passes through unchanged."""
        from frisian_mcp.decorators import mcp_tool

        isolated = ToolRegistry()
        with (
            patch("frisian_mcp.decorators.tool_registry", isolated),
            patch("frisian_mcp.views.tool_registry", isolated),
        ):

            @mcp_tool(
                name="int.small",
                description="Small response tool",
                input_schema={"type": "object", "properties": {}},
            )
            def _small(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                return {"ok": True}

            with patch("frisian_mcp.views.django_cache") as mock_cache:
                mock_cache.get.return_value = None
                response = _call_tool(rf, "int.small", {})

        result = _tool_result(response)
        assert result == {"ok": True}
        assert "continuation_token" not in result


# ---------------------------------------------------------------------------
# SEC-3 — continuation tokens bound to caller / tier / tool
# ---------------------------------------------------------------------------


class TestHeavyContinuationOwnerBinding:
    """Continuation tokens must not be replayable across callers or tools."""

    @staticmethod
    def _isolated_registry_with_heavy(name: str, payload: Any) -> ToolRegistry:
        """Register a single ``@mcp_heavy`` tool that returns *payload*."""
        isolated = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", isolated):

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
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        payload = {"big": list(range(20))}
        reg = self._isolated_registry_with_heavy("sec3.heavy1", payload)

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _call_tool(rf, "sec3.heavy1", {})

        # The view called cache.set exactly once with the wrapped payload.
        assert mock_cache.set.call_count == 1
        _key, written, _ttl = mock_cache.set.call_args.args
        assert written["result"] == payload
        assert written["tool_name"] == "sec3.heavy1"
        # The owner key matches the canonical helper for the same request shape.
        expected = _heavy_owner_key(_build_call_tool_request(rf, "sec3.heavy1", {}), "sec3.heavy1")
        assert written["owner_key"] == expected

    def test_call2_owner_mismatch_returns_is_error(self, rf: RequestFactory) -> None:
        """
        A continuation token issued for caller A is refused for caller B.

        Simulated by mocking the cache to return a wrapped entry whose
        ``owner_key`` does not match what the current request produces.
        """
        reg = self._isolated_registry_with_heavy("sec3.heavy2", {"x": 1})

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": {"x": 1},
                # Deliberately-foreign owner — different tier.
                "owner_key": "tool=sec3.heavy2:auth=anon:tier=admin",
                "tool_name": "sec3.heavy2",
                "resolved_target": "sec3.heavy2",
            }
            response = _call_tool(
                rf,
                "sec3.heavy2",
                {"continuation_token": "stolen-token", "mode": "full"},
            )

        result = _tool_result(response)
        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    def test_call2_tool_name_mismatch_returns_is_error(self, rf: RequestFactory) -> None:
        """
        A token issued for tool A cannot be replayed against tool B.

        Tool name is part of the owner key; computing it for the call-2
        tool yields a different key than the one stored at issuance, so
        the gate refuses.
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

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

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": {"sensitive": "data"},
                "owner_key": owner_for_heavy3,
                "tool_name": "sec3.heavy3",
                "resolved_target": "sec3.heavy3",
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

    def test_call2_legacy_raw_entry_treated_as_expired(self, rf: RequestFactory) -> None:
        """
        A pre-fix raw cache entry (no owner_key) is treated as expired.

        Existing cached entries from before the SEC-3 deploy have the legacy
        shape — bare result, no binding.  Serving them would defeat the
        whole fix; rejecting them as expired forces re-issuance under the
        new owner-bound format.
        """
        reg = self._isolated_registry_with_heavy("sec3.heavy4", {"legacy": True})

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
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

    def test_call2_owner_match_serves_cached_result(self, rf: RequestFactory) -> None:
        """The happy path: matching owner_key → cached result is served."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        reg = self._isolated_registry_with_heavy("sec3.heavy5", {"ok": True})
        owner = _heavy_owner_key(_build_call_tool_request(rf, "sec3.heavy5", {}), "sec3.heavy5")

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": {"ok": True},
                "owner_key": owner,
                "tool_name": "sec3.heavy5",
                "resolved_target": "sec3.heavy5",
            }
            response = _call_tool(
                rf,
                "sec3.heavy5",
                {"continuation_token": "x", "mode": "full"},
            )

        result = _tool_result(response)
        assert result == {"ok": True}

    def test_call2_serves_across_session_id_drift(self, rf: RequestFactory) -> None:
        """TUR-16: a probe minted under one Mcp-Session-Id resumes under a different one.

        The Claude.ai connector mints a fresh session id per tool-call POST, so
        the probe (write) and redeem (read) legs carry different session
        headers.  Session is no longer part of the owner key, so the same
        caller's continuation must still be served.
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        reg = self._isolated_registry_with_heavy("sec3.heavy6", {"ok": True})
        # Owner key as computed at PROBE time, under session "write".
        write_req = _build_call_tool_request(rf, "sec3.heavy6", {})
        write_req.META["HTTP_MCP_SESSION_ID"] = "session-write"
        owner = _heavy_owner_key(write_req, "sec3.heavy6")

        # REDEEM under a DIFFERENT session id (fresh per-POST session).
        redeem_req = _post(
            rf,
            _jsonrpc(
                "tools/call",
                {
                    "name": "sec3.heavy6",
                    "arguments": {"continuation_token": "x", "mode": "full"},
                },
            ),
        )
        redeem_req.user = AnonymousUser()
        redeem_req.META["HTTP_MCP_SESSION_ID"] = "session-read-different"

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": {"ok": True},
                "owner_key": owner,
                "tool_name": "sec3.heavy6",
                "resolved_target": "sec3.heavy6",
            }
            response = _view(redeem_req)

        # Served despite the session-id drift (pre-TUR-16 this was owner_mismatch).
        assert _tool_result(response) == {"ok": True}


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
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        key = _heavy_owner_key(self._request(rf), "x.list")
        assert "auth=anon" in key
        assert "tool=x.list" in key

    def test_different_tools_produce_different_keys(self, rf: RequestFactory) -> None:
        """Tool name is part of the key; two tools yield different bindings."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        a = _heavy_owner_key(self._request(rf), "tool.a")
        b = _heavy_owner_key(self._request(rf), "tool.b")
        assert a != b

    def test_session_id_does_not_affect_key(self, rf: RequestFactory) -> None:
        """TUR-16: Mcp-Session-Id is NOT in the owner key, so it can't gate resume.

        Real MCP clients mint a fresh session id per tool-call POST; binding on
        it broke legitimate probe->redeem resume.  Two requests differing only
        in the session header must produce the SAME key (and neither carries a
        ``session=`` segment).
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        req_a = self._request(rf)
        req_a.META["HTTP_MCP_SESSION_ID"] = "session-A"
        req_b = self._request(rf)
        req_b.META["HTTP_MCP_SESSION_ID"] = "session-B"
        key_a = _heavy_owner_key(req_a, "x.list")
        key_b = _heavy_owner_key(req_b, "x.list")
        key_none = _heavy_owner_key(self._request(rf), "x.list")
        assert key_a == key_b == key_none
        assert "session=" not in key_a

    def test_same_type_different_auth_pk_changes_key(self, rf: RequestFactory) -> None:
        """Same type+tier+user, different token pk => different key (anti-replay anchor).

        Isolates the credential-pk dimension alone (TUR-16 kept ``auth={type}:{pk}``
        rather than keying on the OAuth client) so a distinct token issued to the
        same principal cannot redeem another token's continuation.
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        req_a = self._request(rf)
        auth_a = MagicMock()
        auth_a.pk = 7
        auth_a.permission = "admin"
        req_a.auth = auth_a

        req_b = self._request(rf)
        auth_b = MagicMock()
        auth_b.pk = 8
        auth_b.permission = "admin"
        req_b.auth = auth_b

        # Same auth type + tier + (anon) user; only the token pk differs.
        assert _heavy_owner_key(req_a, "x.list") != _heavy_owner_key(req_b, "x.list")

    def test_tier_change_changes_the_key(self, rf: RequestFactory) -> None:
        """A token whose tier later downgrades produces a different owner key."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
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


# ---------------------------------------------------------------------------
# T2 — Issue 53 acceptance matrix (end-to-end call1->call2 via McpView)
# ---------------------------------------------------------------------------


class TestIssue53AcceptanceMatrix:
    """
    End-to-end coverage of the Issue 53 / TUR-16 acceptance criteria.

    Gap-fills what :class:`TestHeavyContinuationOwnerBinding` and
    :class:`TestHeavyOwnerKey` exercise at the unit level: every
    ``available_modes`` entry redeeming through the real ``McpView``
    dispatch, and an explicit DENY per replay dimension (credential, user,
    tier) rather than only the generic mismatch case.
    """

    @staticmethod
    def _isolated_registry_with_heavy(name: str, payload: Any) -> ToolRegistry:
        """Register a single ``@mcp_heavy`` tool that returns *payload*."""
        isolated = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(
                name=name,
                description="Issue 53 acceptance matrix test",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(  # pylint: disable=unused-variable
                _arguments: dict[str, Any], _request: Any
            ) -> Any:
                return payload

        return isolated

    def test_call2_paginated_mode_redeems_with_matching_owner(self, rf: RequestFactory) -> None:
        """Paginated mode redeems through the SEC-3 owner-key gate, not just in isolation."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = list(range(50))
        reg = self._isolated_registry_with_heavy("matrix.paginated", stored)
        owner = _heavy_owner_key(
            _build_call_tool_request(rf, "matrix.paginated", {}), "matrix.paginated"
        )

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": stored,
                "owner_key": owner,
                "tool_name": "matrix.paginated",
                "resolved_target": "matrix.paginated",
            }
            response = _call_tool(
                rf,
                "matrix.paginated",
                {"continuation_token": "x", "mode": "paginated", "page": 1, "page_size": 10},
            )

        result = _tool_result(response)
        assert result["items"] == list(range(10))
        assert result["total"] == 50

    def test_call2_filtered_mode_redeems_with_matching_owner(self, rf: RequestFactory) -> None:
        """Filtered mode redeems through the SEC-3 owner-key gate, not just in isolation."""
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {"a": 1, "b": 2, "c": 3}
        reg = self._isolated_registry_with_heavy("matrix.filtered", stored)
        owner = _heavy_owner_key(
            _build_call_tool_request(rf, "matrix.filtered", {}), "matrix.filtered"
        )

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": stored,
                "owner_key": owner,
                "tool_name": "matrix.filtered",
                "resolved_target": "matrix.filtered",
            }
            response = _call_tool(
                rf,
                "matrix.filtered",
                {"continuation_token": "x", "mode": "filtered", "filter_keys": ["a", "c"]},
            )

        result = _tool_result(response)
        assert result == {"a": 1, "c": 3}

    @pytest.mark.parametrize(
        ("mode", "extra_args"),
        [
            ("full", {}),
            ("summary", {}),
            ("paginated", {"page": 1, "page_size": 10}),
            ("filtered", {"filter_keys": ["alpha"]}),
        ],
    )
    def test_call2_every_mode_serves_across_session_id_drift(
        self, rf: RequestFactory, mode: str, extra_args: dict[str, Any]
    ) -> None:
        """TUR-16 + Issue 53: every ``available_modes`` entry survives session-id drift.

        A real MCP client mints a fresh ``Mcp-Session-Id`` per POST, so the
        probe and redeem legs carry different session headers regardless of
        which mode the caller ultimately picks for call 2 — not just ``full``.
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {"alpha": "a" * 10, "beta": "b" * 10}
        tool_name = f"matrix.drift.{mode}"
        reg = self._isolated_registry_with_heavy(tool_name, stored)

        write_req = _build_call_tool_request(rf, tool_name, {})
        write_req.META["HTTP_MCP_SESSION_ID"] = "session-write"
        owner = _heavy_owner_key(write_req, tool_name)

        redeem_args = {"continuation_token": "x", "mode": mode, **extra_args}
        redeem_req = _post(
            rf, _jsonrpc("tools/call", {"name": tool_name, "arguments": redeem_args})
        )
        redeem_req.user = AnonymousUser()
        redeem_req.META["HTTP_MCP_SESSION_ID"] = "session-read-different"

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": stored,
                "owner_key": owner,
                "tool_name": tool_name,
                "resolved_target": tool_name,
            }
            response = _view(redeem_req)

        result = _tool_result(response)
        assert "error" not in result, (
            f"mode={mode!r} was refused despite session-id drift being the only change: "
            f"{result.get('error')!r}"
        )

    def test_anonymous_open_route_round_trip_survives_session_drift(
        self, rf: RequestFactory
    ) -> None:
        """
        Issue 53 regression: the anonymous/open-route flow is unaffected by TUR-16.

        Anonymous callers never had a session segment in their owner key, so
        removing ``Mcp-Session-Id`` from the key must be a strict no-op here.
        Covers both "different session id" and "absent session id on redeem".
        """
        from frisian_mcp.views import (  # pylint: disable=import-outside-toplevel
            _heavy_owner_key,
        )

        stored = {"open": "data"}
        reg = self._isolated_registry_with_heavy("matrix.anon", stored)

        probe_req = _build_call_tool_request(rf, "matrix.anon", {})
        probe_req.META["HTTP_MCP_SESSION_ID"] = "anon-session-1"
        owner = _heavy_owner_key(probe_req, "matrix.anon")
        assert "session=" not in owner

        # Redeem carries no Mcp-Session-Id at all (absent, not merely different).
        redeem_req = _post(
            rf,
            _jsonrpc(
                "tools/call",
                {
                    "name": "matrix.anon",
                    "arguments": {"continuation_token": "x", "mode": "full"},
                },
            ),
        )
        redeem_req.user = AnonymousUser()

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = {
                "result": stored,
                "owner_key": owner,
                "tool_name": "matrix.anon",
                "resolved_target": "matrix.anon",
            }
            response = _view(redeem_req)

        result = _tool_result(response)
        assert result == stored

    @staticmethod
    def _post_with_bearer(
        rf: RequestFactory, name: str, arguments: dict[str, Any], raw: str
    ) -> Any:
        """Build a real POST carrying ``Authorization: Bearer raw`` for DB-token auth tests."""
        return rf.post(
            "/mcp/",
            data=json.dumps(_jsonrpc("tools/call", {"name": name, "arguments": arguments})),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )

    def _configure_token_auth(self, settings: Any) -> None:
        """T7: point McpView at real FrisianMcpTokenAuthentication, no gateway permission gate."""
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []

    @pytest.mark.django_db
    def test_call2_different_user_denies(self, rf: RequestFactory, settings: Any) -> None:
        """
        Issue 53 matrix: different user, real end-to-end -> DENY.

        T7: rewritten from a mocked-request version that DRF's real
        authentication silently discarded (see room note 13bad783). Two
        distinct users each hold their own DB token at the SAME tier;
        probing as user A and redeeming as user B must be refused.
        """
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        user_model = get_user_model()
        self._configure_token_auth(settings)
        user_a = user_model.objects.create_user(username="matrix-user-a")
        user_b = user_model.objects.create_user(username="matrix-user-b")
        token_a = FrisianMcpToken.objects.create(name="a", user=user_a, permission="read")
        token_b = FrisianMcpToken.objects.create(name="b", user=user_b, permission="read")

        stored = {"secret": True}
        reg = self._isolated_registry_with_heavy("matrix.user", stored)

        with patch("frisian_mcp.views.tool_registry", reg):
            probe = self._post_with_bearer(rf, "matrix.user", {}, token_a.plaintext_token)
            token = _tool_result(_view(probe))["continuation_token"]

            redeem = self._post_with_bearer(
                rf,
                "matrix.user",
                {"continuation_token": token, "mode": "full"},
                token_b.plaintext_token,
            )
            result = _tool_result(_view(redeem))

        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    @pytest.mark.django_db
    def test_call2_different_credential_denies(self, rf: RequestFactory, settings: Any) -> None:
        """
        Issue 53 matrix: same user/tier, different credential, real end-to-end -> DENY.

        T7: rewritten per room note 13bad783. Same user holds TWO DB tokens
        at the same tier -- isolates the credential-pk dimension since the
        user and tier stay constant across probe and redeem.
        """
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        user_model = get_user_model()
        self._configure_token_auth(settings)
        user = user_model.objects.create_user(username="matrix-cred-user")
        token_a = FrisianMcpToken.objects.create(name="a", user=user, permission="admin")
        token_b = FrisianMcpToken.objects.create(name="b", user=user, permission="admin")

        stored = {"secret": True}
        reg = self._isolated_registry_with_heavy("matrix.cred", stored)

        with patch("frisian_mcp.views.tool_registry", reg):
            probe = self._post_with_bearer(rf, "matrix.cred", {}, token_a.plaintext_token)
            token = _tool_result(_view(probe))["continuation_token"]

            redeem = self._post_with_bearer(
                rf,
                "matrix.cred",
                {"continuation_token": token, "mode": "full"},
                token_b.plaintext_token,
            )
            result = _tool_result(_view(redeem))

        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    @pytest.mark.django_db
    def test_call2_tier_downgrade_denies(self, rf: RequestFactory, settings: Any) -> None:
        """
        Issue 53 matrix: same credential/user, tier downgraded, real end-to-end -> DENY.

        T7: rewritten per room note 13bad783. The SAME token is used for both
        legs; its ``permission`` is downgraded in the DB between probe and
        redeem, isolating the tier dimension while credential and user PKs
        stay identical.
        """
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            FrisianMcpToken,
        )

        user_model = get_user_model()
        self._configure_token_auth(settings)
        user = user_model.objects.create_user(username="matrix-tier-user")
        token = FrisianMcpToken.objects.create(name="t", user=user, permission="read_write")

        stored = {"secret": True}
        reg = self._isolated_registry_with_heavy("matrix.tier", stored)

        with patch("frisian_mcp.views.tool_registry", reg):
            probe = self._post_with_bearer(rf, "matrix.tier", {}, token.plaintext_token)
            cont_token = _tool_result(_view(probe))["continuation_token"]

            token.permission = "read"
            token.save(update_fields=["permission"])

            redeem = self._post_with_bearer(
                rf,
                "matrix.tier",
                {"continuation_token": cont_token, "mode": "full"},
                token.plaintext_token,
            )
            result = _tool_result(_view(redeem))

        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    def test_call2_different_static_api_key_same_tier_denies(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """
        T6 / T5 regression: two static API keys at the SAME tier no longer collide.

        Pre-T5, ``_ApiKeyAuth`` carried only the permission tier (no per-key
        identity), so any two same-tier static keys produced the identical
        owner key and could redeem each other's continuation tokens (T4
        finding). Post-T5, ``_ApiKeyAuth.key_id`` (the matched HMAC digest)
        distinguishes them: probe with key A, redeem with key B -> DENY.

        Drives REAL ``FrisianMcpApiKeyAuthentication`` end to end (real
        ``Authorization`` headers + real cache) rather than pre-setting
        ``request.auth`` before calling the view -- DRF's request wrapping
        re-runs authentication and discards any auth object set on the raw
        request beforehand, so a mocked ``request.auth`` never reaches
        ``_heavy_owner_key`` and would silently test nothing.
        """
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            _hmac_token,
        )

        key_a, key_b = "static-key-a", "static-key-b"
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token(key_a): "read", _hmac_token(key_b): "read"}

        stored = {"secret": True}
        reg = self._isolated_registry_with_heavy("matrix.apikey.real", stored)

        with patch("frisian_mcp.views.tool_registry", reg):
            probe_req = rf.post(
                "/mcp/",
                data=json.dumps(
                    _jsonrpc("tools/call", {"name": "matrix.apikey.real", "arguments": {}})
                ),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {key_a}",
            )
            token = _tool_result(_view(probe_req))["continuation_token"]

            redeem_req = rf.post(
                "/mcp/",
                data=json.dumps(
                    _jsonrpc(
                        "tools/call",
                        {
                            "name": "matrix.apikey.real",
                            "arguments": {"continuation_token": token, "mode": "full"},
                        },
                    )
                ),
                content_type="application/json",
                # Different raw secret, same tier.
                HTTP_AUTHORIZATION=f"Bearer {key_b}",
            )
            result = _tool_result(_view(redeem_req))

        assert "error" in result
        assert "does not belong to this caller" in result["error"]

    def test_call2_same_static_api_key_resumes_successfully(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """T6 positive control: the SAME static API key still redeems its own token."""
        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            _hmac_token,
        )

        key_a = "static-key-a-resume"
        settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
        ]
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_API_KEYS = {_hmac_token(key_a): "read"}

        stored = {"ok": True}
        reg = self._isolated_registry_with_heavy("matrix.apikey.real.same", stored)

        with patch("frisian_mcp.views.tool_registry", reg):
            probe_req = rf.post(
                "/mcp/",
                data=json.dumps(
                    _jsonrpc("tools/call", {"name": "matrix.apikey.real.same", "arguments": {}})
                ),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {key_a}",
            )
            token = _tool_result(_view(probe_req))["continuation_token"]

            redeem_req = rf.post(
                "/mcp/",
                data=json.dumps(
                    _jsonrpc(
                        "tools/call",
                        {
                            "name": "matrix.apikey.real.same",
                            "arguments": {"continuation_token": token, "mode": "full"},
                        },
                    )
                ),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {key_a}",
            )
            result = _tool_result(_view(redeem_req))

        assert result == stored


class TestHeavyClosedSchemaKeepsItsStrictness:
    """
    H20: ``@mcp_heavy`` carries the HOST's schema, so its strictness is theirs.

    H18 fixed ``merge_continuation_branch`` and left this site, on the argument
    that ``_merge_negotiation_schema`` applies to *"argument shapes that are
    ours"*.  Only half true: ``@mcp_dispatcher`` and the group builder produce
    ours, but a host applies ``@mcp_heavy`` to its own tool with its own
    ``input_schema``.  Same consent problem, same runtime effect, different
    decorator — the consumer-enumeration lesson recurring inside the fix for it.

    Unlike the conditional branch, nothing had to be given up here: the flat
    merge declares its fields in the same object ``additionalProperties`` is
    evaluated against, so strictness and negotiation coexist.
    """

    CLOSED: dict[str, Any] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    }

    def _registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_heavy(
                name="item_search",
                description="host-authored closed schema",
                input_schema=dict(self.CLOSED),
            )
            def _fn(arguments: dict[str, Any], _request: Any) -> Any:
                return {"got": sorted(arguments)}

            _ = _fn
        return reg

    def test_published_schema_stays_closed(self) -> None:
        """The host's restriction survives registration."""
        entry = self._registry().get_entry("item_search")
        assert entry is not None
        assert entry.input_schema.get("additionalProperties") is False

    def test_unknown_field_still_rejected_on_call_one(self) -> None:
        """
        Asserted at ``dispatch`` — where the weakening actually bit.

        A schema-shape assertion alone would not have caught the consequence.
        """
        reg = self._registry()
        request = RequestFactory().post("/mcp/")
        request.user = AnonymousUser()

        with pytest.raises(ToolInputError) as exc:
            reg.dispatch(request, "item_search", {"q": "x", "unexpected_field": "smuggled"})
        assert "unexpected_field" in str(exc.value)

        # The host's own field still works — strictness, not breakage.
        assert reg.dispatch(request, "item_search", {"q": "x"}) == {"got": ["q"]}

    def test_negotiation_still_reachable_on_a_closed_schema(self) -> None:
        """
        And unlike the conditional-branch case, the tool still negotiates.

        Preserving the restriction cost nothing here, so a heavy tool with a
        closed schema keeps both its strictness and its continuation call.
        """
        entry = self._registry().get_entry("item_search")
        assert entry is not None
        assert schema_discloses_continuation(entry.input_schema) is True

        # A continuation-shaped call validates against the published schema,
        # even though that schema still rejects everything undeclared.
        validator = jsonschema.Draft7Validator(entry.input_schema)
        assert not list(validator.iter_errors({"continuation_token": "t", "mode": "full"}))
        assert list(validator.iter_errors({"continuation_token": "t", "smuggled": 1}))
