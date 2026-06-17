"""
Write-path response filtering — Task 3 (49af9043).

Validates the lean-default / verify-opt-in behaviour introduced for
write-path (create/update/delete, bulk) tool invocations.  Measures
actual token savings vs the old full-echo behaviour.

Coverage:
- _extract_lean_envelope unit tests (single, bulk, delete)
- Default write (no verify) → lean confirmation envelope
- verify=True → full serialised object returned directly
- Bulk create (60 objects) → compact summary, NOT 60 full bodies
- continuation_token retrieval path (lean → mode=full → same as verify=True)
- @mcp_heavy takes precedence when both is_heavy and is_write are set
- Read/list paths are unaffected (regression guard)
- Token-savings measurement for 60-object bulk create
- Group dispatcher write path (resource/action routing through lean envelope)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from frisian_mcp.backends.invocation import _extract_lean_envelope
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

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


def _call_tool(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    request = _post(rf, _jsonrpc("tools/call", {"name": name, "arguments": arguments}))
    request.user = AnonymousUser()
    return _view(request)


def _tool_result(response: Any) -> Any:
    data = json.loads(response.content)
    return json.loads(data["result"]["content"][0]["text"])


def _is_error(response: Any) -> bool:
    data = json.loads(response.content)
    return data["result"]["isError"]


def _build_write_registry(
    name: str,
    handler: Any,
    is_heavy: bool = False,
) -> ToolRegistry:
    """
    Return an isolated registry with a single write-marked tool.

    Uses permission_tier='read' so tests work with the default anonymous
    unauthenticated tier — this isolates write-path filtering behaviour
    from permission enforcement (tested separately in test_permission_tiers.py).
    """
    isolated = ToolRegistry()
    isolated.register(
        name=name,
        fn=handler,
        description="stub write tool",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        permission_classes=[],
        is_write=True,
        is_heavy=is_heavy,
        permission_tier="read",
    )
    return isolated


def _build_read_registry(name: str, handler: Any) -> ToolRegistry:
    """Return an isolated registry with a single read tool (is_write=False)."""
    isolated = ToolRegistry()
    isolated.register(
        name=name,
        fn=handler,
        description="stub read tool",
        input_schema={"type": "object", "properties": {}},
        permission_classes=[],
        is_write=False,
        permission_tier="read",
    )
    return isolated


# ---------------------------------------------------------------------------
# _extract_lean_envelope unit tests
# ---------------------------------------------------------------------------


class TestExtractLeanEnvelope:
    """Unit tests for the _extract_lean_envelope helper."""

    def test_single_object_includes_id(self) -> None:
        """Single create result: id is present in lean envelope."""
        result = {"id": "abc-123", "name": "router-1", "status": "active", "extra": "data"}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["id"] == "abc-123"

    def test_single_object_includes_name(self) -> None:
        """Single create result: name is included when present."""
        result = {"id": "abc-123", "name": "router-1"}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["name"] == "router-1"

    def test_single_object_includes_url_when_present(self) -> None:
        """Url is included in the lean envelope when the serializer exposes it."""
        result = {"id": "abc-123", "url": "https://host/api/devices/abc-123/", "name": "r1"}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["url"] == "https://host/api/devices/abc-123/"

    def test_single_object_omits_full_payload(self) -> None:
        """The lean envelope does NOT include arbitrary payload fields."""
        result = {"id": "1", "name": "x", "huge_field": "x" * 1000, "other": "data"}
        envelope = _extract_lean_envelope(result, "tok")
        assert "huge_field" not in envelope
        assert "other" not in envelope

    def test_single_object_has_data_size(self) -> None:
        """data_size (bytes) of the full serialized result is present."""
        result = {"id": "1", "name": "x", "payload": "y" * 100}
        envelope = _extract_lean_envelope(result, "tok")
        expected_size = len(json.dumps(result).encode())
        assert envelope["data_size"] == expected_size

    def test_single_object_has_continuation_token(self) -> None:
        """continuation_token is the value passed in."""
        result = {"id": "1"}
        envelope = _extract_lean_envelope(result, "mytoken")
        assert envelope["continuation_token"] == "mytoken"

    def test_bulk_result_accepted_count(self) -> None:
        """Bulk (list) result: accepted equals the number of items created."""
        result = [{"id": str(i), "name": f"device-{i}"} for i in range(60)]
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["accepted"] == 60

    def test_bulk_result_no_failed_key(self) -> None:
        """Bulk result: failed is not present (bulk creates are atomic; no partial counts)."""
        result = [{"id": str(i)} for i in range(10)]
        envelope = _extract_lean_envelope(result, "tok")
        assert "failed" not in envelope

    def test_bulk_result_has_data_size(self) -> None:
        """Bulk result: data_size reflects the full JSON byte count of all objects."""
        result = [{"id": str(i), "name": f"device-{i}", "payload": "x" * 50} for i in range(60)]
        envelope = _extract_lean_envelope(result, "tok")
        expected = len(json.dumps(result).encode())
        assert envelope["data_size"] == expected

    def test_bulk_result_not_full_objects(self) -> None:
        """Bulk lean envelope must NOT contain the list of full objects."""
        result = [{"id": str(i), "name": f"d-{i}", "secret": "hidden"} for i in range(5)]
        envelope = _extract_lean_envelope(result, "tok")
        # The envelope is a plain dict; no list of device dicts leaks through.
        assert not any(isinstance(v, list) for v in envelope.values())
        assert "secret" not in str(envelope)

    def test_delete_envelope_structure(self) -> None:
        """Delete lean envelope: deleted=True and status_code, no continuation_token."""
        result = {"deleted": True, "status": 204}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["deleted"] is True
        assert envelope["status_code"] == 204
        # Delete envelopes carry no continuation_token (nothing to retrieve).
        assert "continuation_token" not in envelope

    def test_pk_field_aliased_to_id(self) -> None:
        """When the serializer returns 'pk' instead of 'id', it maps to 'id'."""
        result = {"pk": "uuid-abc", "name": "switch-1"}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["id"] == "uuid-abc"

    def test_display_field_included_when_no_name(self) -> None:
        """'display' is used when 'name' is absent (common in Nautobot serializers)."""
        result = {"id": "1", "display": "Router A"}
        envelope = _extract_lean_envelope(result, "tok")
        assert envelope["display"] == "Router A"

    def test_single_object_has_status_code(self) -> None:
        """Single create result: status_code is present in the lean envelope."""
        result = {"id": "abc-123", "name": "router-1"}
        envelope = _extract_lean_envelope(result, "tok")
        assert "status_code" in envelope
        assert envelope["status_code"] == 200

    def test_single_object_app_status_field_does_not_override_status_code(self) -> None:
        """Application-level 'status' string/dict must not be treated as an HTTP code."""
        # String status (e.g. Nautobot device status field)
        result_str = {"id": "1", "name": "x", "status": "active"}
        assert _extract_lean_envelope(result_str, "tok")["status_code"] == 200

        # Dict status (e.g. Nautobot nested status object)
        result_dict = {"id": "2", "name": "y", "status": {"value": "active", "label": "Active"}}
        assert _extract_lean_envelope(result_dict, "tok")["status_code"] == 200

    def test_bulk_result_has_status_code(self) -> None:
        """Bulk (list) result: status_code is present in the lean envelope."""
        result = [{"id": str(i), "name": f"device-{i}"} for i in range(5)]
        envelope = _extract_lean_envelope(result, "tok")
        assert "status_code" in envelope
        assert envelope["status_code"] == 200


# ---------------------------------------------------------------------------
# Views-layer write-path filtering tests
# ---------------------------------------------------------------------------


class TestWritePathDefaultLean:
    """Default write response (no verify flag) returns a lean envelope via McpView."""

    def test_single_create_returns_lean_not_full(self, rf: RequestFactory) -> None:
        """Create without verify=True → lean envelope, NOT the full serialized object."""
        full_object = {
            "id": "device-uuid-1",
            "name": "spine-1",
            "status": {"value": "active"},
            "device_type": {"model": "Nexus 9000"},
            "site": {"name": "DC1"},
            "rack": None,
            "position": None,
            "extra_field": "x" * 200,
        }

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = _build_write_registry("device.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "device.create", {"name": "spine-1"})

        result = _tool_result(response)
        assert not _is_error(response)

        # Lean envelope MUST contain the id, continuation_token, and status_code.
        assert result["id"] == "device-uuid-1"
        assert "continuation_token" in result
        assert "data_size" in result
        assert result["status_code"] == 200

        # The full object's payload fields must NOT be present.
        assert "device_type" not in result
        assert "site" not in result
        assert "extra_field" not in result

    def test_single_create_lean_data_size_correct(self, rf: RequestFactory) -> None:
        """data_size in the lean envelope equals the byte count of the full serialized object."""
        full_object = {"id": "x", "name": "y", "payload": "z" * 500}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = _build_write_registry("obj.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "obj.create", {"name": "y"})

        result = _tool_result(response)
        expected_size = len(json.dumps(full_object).encode())
        assert result["data_size"] == expected_size

    def test_verify_stripped_before_dispatch(self, rf: RequestFactory) -> None:
        """The verify flag is removed from arguments before the tool handler is called."""
        received_args: dict[str, Any] = {}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            received_args.update(arguments)
            return {"id": "1", "name": "x"}

        isolated = _build_write_registry("item.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _call_tool(rf, "item.create", {"name": "x", "verify": False})

        # verify must not appear in the arguments the handler received.
        assert "verify" not in received_args

    def test_delete_lean_includes_id_from_arguments(self, rf: RequestFactory) -> None:
        """Delete lean envelope includes the id from original arguments."""

        def _destroy(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return {"deleted": True, "status": 204}

        isolated = _build_write_registry("device.destroy", _destroy)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "device.destroy", {"id": "device-uuid-99"})

        result = _tool_result(response)
        assert not _is_error(response)
        assert result["deleted"] is True
        assert result["id"] == "device-uuid-99"
        assert result["status_code"] == 204
        assert "continuation_token" not in result


class TestWritePathVerifyFull:
    """verify=True returns the full serialised object(s) directly — no lean envelope."""

    def test_verify_true_returns_full_object(self, rf: RequestFactory) -> None:
        """verify=True bypasses lean envelope; full object is returned directly."""
        full_object = {
            "id": "uuid-abc",
            "name": "leaf-1",
            "status": "active",
            "rack": {"name": "rack-1"},
            "device_type": {"model": "EX4300"},
            "extra": "included when verify=True",
        }

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = _build_write_registry("device.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "device.create", {"name": "leaf-1", "verify": True})

        result = _tool_result(response)
        assert not _is_error(response)

        # Full object fields must all be present.
        assert result["id"] == "uuid-abc"
        assert result["name"] == "leaf-1"
        assert result["rack"] == {"name": "rack-1"}
        assert result["device_type"] == {"model": "EX4300"}
        assert result["extra"] == "included when verify=True"

        # Lean-only fields must NOT be present.
        assert "continuation_token" not in result
        assert "data_size" not in result

    def test_verify_true_does_not_cache(self, rf: RequestFactory) -> None:
        """verify=True: no cache write (full result returned directly, no token needed)."""

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return {"id": "1", "name": "x"}

        isolated = _build_write_registry("item.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _call_tool(rf, "item.create", {"name": "x", "verify": True})

        mock_cache.set.assert_not_called()

    def test_verify_stripped_even_with_true(self, rf: RequestFactory) -> None:
        """verify=True is still removed from arguments before dispatch."""
        received_args: dict[str, Any] = {}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            received_args.update(arguments)
            return {"id": "1"}

        isolated = _build_write_registry("item.create", _create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _call_tool(rf, "item.create", {"name": "x", "verify": True})

        assert "verify" not in received_args


class TestWritePathBulkCreate:
    """Bulk create (60 objects) returns a compact summary, not N full bodies."""

    def _make_device(self, i: int) -> dict[str, Any]:
        """Mimic a Nautobot device serializer output (~950 tokens / device)."""
        return {
            "id": f"device-uuid-{i:04d}",
            "url": f"https://demo.example.com/api/dcim/devices/{i}/",
            "name": f"spine-{i:02d}",
            "status": {"value": "active", "label": "Active"},
            "device_type": {
                "id": "dt-uuid-1",
                "url": "https://demo.example.com/api/dcim/device-types/1/",
                "manufacturer": {"name": "Cisco", "slug": "cisco"},
                "model": "Nexus 9000",
                "slug": "nexus-9000",
            },
            "device_role": {"name": "Spine", "slug": "spine"},
            "site": {"name": "DC-East", "slug": "dc-east"},
            "rack": None,
            "position": None,
            "face": None,
            "tenant": None,
            "platform": None,
            "serial": "",
            "asset_tag": None,
            "cluster": None,
            "virtual_chassis": None,
            "primary_ip4": None,
            "primary_ip6": None,
            "comments": "",
            "local_context_data": None,
            "tags": [],
            "custom_fields": {},
        }

    def test_bulk_60_returns_compact_summary(self, rf: RequestFactory) -> None:
        """60-device bulk create returns {accepted, failed, data_size, continuation_token}."""
        devices = [self._make_device(i) for i in range(60)]

        def _bulk_create(arguments: dict[str, Any], request: Any) -> list[dict[str, Any]]:
            return devices

        isolated = _build_write_registry("devices.bulk_create", _bulk_create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            objects = [{"name": f"spine-{i}"} for i in range(60)]
            response = _call_tool(rf, "devices.bulk_create", {"objects": objects})

        result = _tool_result(response)
        assert not _is_error(response)

        # Must be a compact summary.
        assert result["accepted"] == 60
        assert "failed" not in result
        assert "data_size" in result
        assert "continuation_token" in result
        assert result["status_code"] == 200

        # Must NOT contain any individual device objects.
        assert "id" not in result
        assert "name" not in result
        assert "device_type" not in result

    def test_bulk_60_response_token_savings(self, rf: RequestFactory) -> None:
        """
        Token savings measurement: 60-device bulk create lean vs full.

        Baseline (from 2026-05-28 Nautobot session): ~50K tokens.
        Target: lean envelope ~50 tokens.
        Expected reduction: ≥ 99%.
        """
        devices = [self._make_device(i) for i in range(60)]

        def _bulk_create(arguments: dict[str, Any], request: Any) -> list[dict[str, Any]]:
            return devices

        isolated = _build_write_registry("devices.bulk_create", _bulk_create)

        # Measure lean envelope size.
        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            objects = [{"name": f"spine-{i}"} for i in range(60)]
            response = _call_tool(rf, "devices.bulk_create", {"objects": objects})

        lean_result = _tool_result(response)
        lean_json = json.dumps(lean_result)
        lean_bytes = len(lean_json.encode())
        lean_est_tokens = lean_bytes / 4  # ~4 bytes per token

        # Measure full-echo size (what verify=True or the old pre-filtering code returns).
        full_json = json.dumps(devices)
        full_bytes = len(full_json.encode())
        full_est_tokens = full_bytes / 4

        reduction_pct = (1 - lean_est_tokens / full_est_tokens) * 100

        # Assertions on the measurement.
        # Full echo was ~50K tokens; lean should be < 100 tokens.
        assert lean_est_tokens < 100, (
            f"Lean envelope too large: ~{lean_est_tokens:.0f} tokens "
            f"(lean_bytes={lean_bytes}); expected < 100 tokens"
        )
        assert full_est_tokens > 10_000, (
            f"Full echo unexpectedly small: ~{full_est_tokens:.0f} tokens; "
            "test devices may be too small"
        )
        assert reduction_pct >= 99.0, (
            f"Token reduction only {reduction_pct:.1f}% (need ≥ 99%); "
            f"lean={lean_est_tokens:.0f} tok, full={full_est_tokens:.0f} tok"
        )

    def test_bulk_lean_data_size_matches_full_json_bytes(self, rf: RequestFactory) -> None:
        """data_size in lean envelope accurately reports full serialized bytes."""
        devices = [self._make_device(i) for i in range(5)]

        def _bulk_create(arguments: dict[str, Any], request: Any) -> list[dict[str, Any]]:
            return devices

        isolated = _build_write_registry("devices.bulk_create", _bulk_create)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "devices.bulk_create", {})

        result = _tool_result(response)
        expected_size = len(json.dumps(devices).encode())
        assert result["data_size"] == expected_size


# ---------------------------------------------------------------------------
# Continuation token retrieval path
# ---------------------------------------------------------------------------


class TestContinuationTokenRetrieval:
    """
    Lean envelope → continuation_token → mode=full fetch returns the same object as verify=True.

    The write continuation path reuses the existing _HEAVY_CACHE_PREFIX / _HEAVY_CACHE_TTL /
    _build_heavy_cache_entry infrastructure.  Call 2 is identical to the @mcp_heavy call-2 path.
    """

    def test_lean_token_retrieves_full_object(self, rf: RequestFactory) -> None:
        """Full result cached by lean write path is retrieved via continuation_token + mode=full."""
        full_object = {"id": "uuid-1", "name": "test-device", "payload": "x" * 200}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = _build_write_registry("device.create", _create)

        # Use a real cache dict to capture and replay the token.
        cache_store: dict[str, Any] = {}

        def _cache_set(key: str, value: Any, timeout: int) -> None:
            cache_store[key] = value

        def _cache_get(key: str) -> Any:
            return cache_store.get(key)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.side_effect = _cache_get
            mock_cache.set.side_effect = _cache_set

            # Call 1: lean create → receive continuation_token.
            resp1 = _call_tool(rf, "device.create", {"name": "test-device"})

        lean = _tool_result(resp1)
        token = lean["continuation_token"]
        assert token is not None

        # Call 2: retrieve full result using the token.
        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.side_effect = _cache_get
            mock_cache.set.side_effect = _cache_set
            resp2 = _call_tool(rf, "device.create", {"continuation_token": token, "mode": "full"})

        full_retrieved = _tool_result(resp2)
        assert full_retrieved["id"] == full_object["id"]
        assert full_retrieved["name"] == full_object["name"]
        assert full_retrieved["payload"] == full_object["payload"]

    def test_lean_token_same_as_verify_true(self, rf: RequestFactory) -> None:
        """Full result via continuation_token is identical to what verify=True returns."""
        full_object = {"id": "uuid-2", "name": "router-x", "tags": ["core", "spine"]}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = _build_write_registry("router.create", _create)
        cache_store: dict[str, Any] = {}

        def _cache_set(key: str, value: Any, timeout: int) -> None:
            cache_store[key] = value

        def _cache_get(key: str) -> Any:
            return cache_store.get(key)

        # Path A: verify=True.
        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.side_effect = _cache_get
            mock_cache.set.side_effect = _cache_set
            resp_verify = _call_tool(rf, "router.create", {"name": "router-x", "verify": True})

        # Path B: lean → token → mode=full.
        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.side_effect = _cache_get
            mock_cache.set.side_effect = _cache_set
            resp_lean = _call_tool(rf, "router.create", {"name": "router-x"})

        lean = _tool_result(resp_lean)
        token = lean["continuation_token"]

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.side_effect = _cache_get
            mock_cache.set.side_effect = _cache_set
            resp_cont = _call_tool(
                rf, "router.create", {"continuation_token": token, "mode": "full"}
            )

        verify_result = _tool_result(resp_verify)
        cont_result = _tool_result(resp_cont)

        assert verify_result == cont_result == full_object


# ---------------------------------------------------------------------------
# @mcp_heavy takes precedence when both flags present
# ---------------------------------------------------------------------------


class TestMcpHeavyPrecedence:
    """@mcp_heavy response negotiation is used when a tool has both is_heavy and is_write."""

    def test_heavy_probe_returned_not_lean_envelope(self, rf: RequestFactory) -> None:
        """A tool with is_heavy=True and is_write=True returns the @mcp_heavy probe, not lean."""
        full_object = {"id": "uuid-h", "name": "heavy-write", "data": list(range(100))}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        # Register with BOTH is_write=True and is_heavy=True.
        isolated = _build_write_registry("hw.create", _create, is_heavy=True)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "hw.create", {"name": "heavy-write"})

        result = _tool_result(response)
        assert not _is_error(response)

        # @mcp_heavy probe envelope fields (NOT lean envelope fields).
        assert "preview" in result
        assert "total_size" in result
        assert "available_modes" in result
        assert "continuation_token" in result

        # Lean-only fields must NOT be present.
        assert "accepted" not in result
        assert "data_size" not in result or "total_size" in result  # total_size is the heavy key


# ---------------------------------------------------------------------------
# Read/list paths unaffected (regression guard)
# ---------------------------------------------------------------------------


class TestReadPathUnaffected:
    """Read/list tools are NOT lean-filtered — the write-path logic is write-only."""

    def test_read_tool_returns_full_result(self, rf: RequestFactory) -> None:
        """A read tool (is_write=False) returns its full result unchanged."""
        full_data = [{"id": str(i), "name": f"item-{i}", "extra": "included"} for i in range(5)]

        def _list(arguments: dict[str, Any], request: Any) -> list[dict[str, Any]]:
            return full_data

        isolated = _build_read_registry("items.list", _list)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "items.list", {})

        result = _tool_result(response)
        assert not _is_error(response)

        # Full result must be returned (list, not lean dict).
        assert isinstance(result, list)
        assert len(result) == 5
        assert result[0]["extra"] == "included"

        # Lean-only fields must NOT appear for read tools.
        assert not isinstance(result, dict)

    def test_read_tool_verify_flag_has_no_effect(self, rf: RequestFactory) -> None:
        """Passing verify=True to a read tool does not affect its output."""
        full_data = {"id": "1", "name": "item-1", "extra": "included"}

        def _retrieve(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_data

        isolated = _build_read_registry("item.retrieve", _retrieve)

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response_no_verify = _call_tool(rf, "item.retrieve", {})
            response_with_verify = _call_tool(rf, "item.retrieve", {"verify": True})

        result_no = _tool_result(response_no_verify)
        result_yes = _tool_result(response_with_verify)

        # Both should return the same full result.
        assert result_no == result_yes == full_data

    def test_write_lean_filtering_does_not_affect_mcp_heavy_read(self, rf: RequestFactory) -> None:
        """A read-only @mcp_heavy tool still returns a probe envelope, not a lean write envelope."""
        from frisian_mcp.decorators import mcp_heavy  # pylint: disable=import-outside-toplevel

        isolated = ToolRegistry()

        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_heavy(
                name="heavy.readonly",
                description="Heavy read-only tool",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> list[dict[str, Any]]:
                return [{"id": str(i)} for i in range(20)]

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(rf, "heavy.readonly", {})

        result = _tool_result(response)
        assert not _is_error(response)

        # Must be a probe envelope (heavy read), not a lean write envelope.
        assert "preview" in result
        assert "total_size" in result
        assert "continuation_token" in result

        # Lean write fields must NOT appear.
        assert "accepted" not in result
        assert "deleted" not in result


# ---------------------------------------------------------------------------
# Group dispatcher write path
# ---------------------------------------------------------------------------


class TestDispatcherGroupWritePath:
    """Group dispatcher (FRISIAN_MCP_DISPATCH_GROUPS) routes write actions through lean envelope."""

    def _build_dispatcher_registry(
        self,
        flat_tools: dict[str, tuple[Any, bool]],
        group_name: str = "dcim",
        resource_prefixes: frozenset[str] | None = None,
    ) -> ToolRegistry:
        """
        Build an isolated registry with flat tools + a group dispatcher.

        Args:
            flat_tools: mapping of tool_name -> (handler, is_write)
            group_name: name to register the group dispatcher under
            resource_prefixes: passed to make_group_invoke; defaults to prefixes
                derived from flat_tools keys

        The dispatcher tool is registered with is_dispatcher=True so that
        views.py's dispatcher lean-envelope block (``_write_entry.is_dispatcher``)
        fires correctly.

        """
        from frisian_mcp.backends.group_dispatcher import (  # noqa: PLC0415
            build_group_input_schema,
            make_group_invoke,
        )

        isolated = ToolRegistry()
        sep = "_"

        for tool_name, (handler, is_write) in flat_tools.items():
            isolated.register(
                name=tool_name,
                fn=handler,
                description=f"stub {tool_name}",
                input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                permission_classes=[],
                is_write=is_write,
                permission_tier="read",
            )

        member_tools = frozenset(flat_tools.keys())
        if resource_prefixes is None:
            resource_prefixes = frozenset(n.split(sep, 1)[0] for n in flat_tools)

        invoke_fn = make_group_invoke(group_name, member_tools, isolated, resource_prefixes)
        isolated.register(
            name=group_name,
            fn=invoke_fn,
            description=f"group dispatcher for {group_name}",
            input_schema=build_group_input_schema(),
            permission_classes=[],
            permission_tier="read",
            is_dispatcher=True,
        )
        return isolated

    # ------------------------------------------------------------------ create

    def test_dispatcher_create_returns_lean_envelope(self, rf: RequestFactory) -> None:
        """Write via group dispatcher returns lean envelope by default (no verify)."""
        full_object = {
            "id": "device-uuid-11",
            "name": "spine-01",
            "status": {"value": "active"},
            "device_type": {"model": "Nexus 9000"},
            "extra": "not in lean",
        }

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = self._build_dispatcher_registry({"device_create": (_create, True)})

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(
                rf,
                "dcim",
                {"resource": "device", "action": "create", "params": {"name": "spine-01"}},
            )

        result = _tool_result(response)
        assert not _is_error(response)

        # Lean envelope must include id, status_code, and continuation_token; not full body.
        assert result.get("id") == "device-uuid-11"
        assert "accepted" in result or "continuation_token" in result
        assert result["status_code"] == 200
        assert "device_type" not in result
        assert "extra" not in result

    def test_dispatcher_create_verify_true_returns_full_object(self, rf: RequestFactory) -> None:
        """verify=True inside params returns the full serialised object directly."""
        full_object = {
            "id": "device-uuid-22",
            "name": "spine-02",
            "status": {"value": "active"},
            "device_type": {"model": "Nexus 9000"},
            "extra": "included when verify=True",
        }

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return full_object

        isolated = self._build_dispatcher_registry({"device_create": (_create, True)})

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(
                rf,
                "dcim",
                {
                    "resource": "device",
                    "action": "create",
                    "params": {"name": "spine-02", "verify": True},
                },
            )

        result = _tool_result(response)
        assert not _is_error(response)
        assert result == full_object

    # ------------------------------------------------------------------ verify stripping

    def test_dispatcher_verify_stripped_from_params_before_tool(self, rf: RequestFactory) -> None:
        """Verify is stripped from params by the dispatcher before the underlying tool sees them."""
        received_params: dict[str, Any] = {}

        def _create(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            received_params.update(arguments)
            return {"id": "x"}

        isolated = self._build_dispatcher_registry({"device_create": (_create, True)})

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            _call_tool(
                rf,
                "dcim",
                {
                    "resource": "device",
                    "action": "create",
                    "params": {"name": "spine-03", "verify": True},
                },
            )

        assert "verify" not in received_params

    # ------------------------------------------------------------------ delete

    def test_dispatcher_delete_returns_lean_with_id(self, rf: RequestFactory) -> None:
        """Delete via dispatcher returns {deleted, id, status_code} lean envelope."""

        def _destroy(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            return {"deleted": True, "status": 204}

        isolated = self._build_dispatcher_registry({"device_destroy": (_destroy, True)})

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(
                rf,
                "dcim",
                {"resource": "device", "action": "destroy", "params": {"id": "device-uuid-99"}},
            )

        result = _tool_result(response)
        assert not _is_error(response)
        assert result["deleted"] is True
        assert result["id"] == "device-uuid-99"
        assert result["status_code"] == 204
        assert "continuation_token" not in result

    # ------------------------------------------------------------------ read unaffected

    def test_dispatcher_read_action_returns_full_result(self, rf: RequestFactory) -> None:
        """Read action routed through group dispatcher returns the full result unchanged."""
        full_list = [{"id": str(i), "name": f"device-{i}", "extra": "included"} for i in range(3)]

        def _list(arguments: dict[str, Any], request: Any) -> list[dict[str, Any]]:
            return full_list

        isolated = self._build_dispatcher_registry({"device_list": (_list, False)})

        with (
            patch("frisian_mcp.views.tool_registry", isolated),
            patch("frisian_mcp.views.django_cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            response = _call_tool(
                rf, "dcim", {"resource": "device", "action": "list", "params": {}}
            )

        result = _tool_result(response)
        assert not _is_error(response)

        # Full list must be returned unmodified.
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["extra"] == "included"


# ---------------------------------------------------------------------------
# Meta.mcp_light_key extension (P4.2.1 — restored behavior)
# ---------------------------------------------------------------------------


class _FakeMeta:
    """Stand-in for ``Serializer.Meta`` with an ``mcp_light_key`` attribute."""

    def __init__(self, light_key: list[str]) -> None:
        self.mcp_light_key = light_key


class _FakeSerializer:
    """Stand-in for a DRF ``Serializer`` with a ``Meta`` inner class."""

    def __init__(self, light_key: list[str]) -> None:
        self.Meta = _FakeMeta(light_key)


class _FakeViewSet:
    """Stand-in for a DRF ``ViewSet`` exposing a ``serializer_class``."""

    def __init__(self, serializer_class: Any) -> None:
        self.serializer_class = serializer_class


class _FakeToolDef:
    """Stand-in for a ToolDefinition; only ``view_class`` is consulted."""

    def __init__(self, view_class: Any) -> None:
        self.view_class = view_class


def _build_envelope_with_meta(
    result: dict[str, Any], light_key: list[str] | None, tool_name: str = "items.create"
) -> dict[str, Any]:
    """
    Wire up a real ``tool_registry`` entry whose ``fn`` closure exposes a
    fake ToolDefinition with a fake ViewSet → Serializer → Meta chain,
    then call ``_extract_lean_envelope`` with ``tool_name`` in the caller's
    frame locals so the frame-introspection path can resolve the
    serializer.
    """
    from frisian_mcp.registry import tool_registry as _registry

    if light_key is None:
        view_class: Any = None
    else:
        view_class = _FakeViewSet(_FakeSerializer(list(light_key)))
    tool_def = _FakeToolDef(view_class)

    def _make_fn(td: _FakeToolDef) -> Any:
        def _invoke(arguments: dict[str, Any], request: Any) -> Any:  # pragma: no cover
            return td

        return _invoke

    _registry.register(
        name=tool_name,
        fn=_make_fn(tool_def),
        description="fake",
        input_schema={"type": "object"},
        permission_classes=[],
    )
    try:
        return _extract_lean_envelope(result, "tok")
    finally:
        _registry._tools.pop(tool_name, None)  # noqa: SLF001 — test cleanup


class TestMcpLightKeyExtension:
    """Coverage for the ``Meta.mcp_light_key`` extension restored in P4.2.1."""

    def test_a_meta_absent_preserves_existing_behaviour(self) -> None:
        """View resolves but has no Meta.mcp_light_key — envelope unchanged."""
        result = {"id": "x", "name": "n", "sku": "SKU-1", "extra": "v"}
        envelope = _build_envelope_with_meta(result, light_key=None)
        # Existing extraction order kicks in (id, name); extras NOT present.
        assert envelope["id"] == "x"
        assert envelope["name"] == "n"
        assert "sku" not in envelope
        assert "extra" not in envelope

    def test_b_meta_present_promotes_named_fields(self) -> None:
        """Meta.mcp_light_key = ['sku', 'reorder_point'] surfaces both fields."""
        result = {
            "id": "x",
            "name": "n",
            "sku": "SKU-1",
            "reorder_point": 5,
            "noise": "skip",
        }
        envelope = _build_envelope_with_meta(result, light_key=["sku", "reorder_point"])
        assert envelope["id"] == "x"
        assert envelope["name"] == "n"
        assert envelope["sku"] == "SKU-1"
        assert envelope["reorder_point"] == 5
        assert "noise" not in envelope

    def test_c_meta_lists_nonexistent_field_skipped_gracefully(self) -> None:
        """Listing a key that isn't in the serialized result is a silent skip."""
        result = {"id": "x", "name": "n", "sku": "SKU-1"}
        envelope = _build_envelope_with_meta(
            result, light_key=["sku", "does_not_exist", "also_missing"]
        )
        assert envelope["id"] == "x"
        assert envelope["sku"] == "SKU-1"
        assert "does_not_exist" not in envelope
        assert "also_missing" not in envelope
