"""
CL-7 / GH #65 defect A — inputSchema validation is a tool error, not a protocol error.

MCP 2025-11-25 ``server/tools.mdx`` splits error reporting in two:

* **Protocol errors** — unknown tools, *malformed requests* (those failing the
  ``CallToolRequest`` schema), server errors.
* **Tool execution errors** — reported in the tool result with ``isError: true``.
  Input validation errors are named there explicitly.

A failure against the tool's own ``inputSchema`` is an input validation error.
"Malformed request" means failing the *CallToolRequest envelope* — a missing
``name``, or ``arguments`` that is not an object — not a missing required field
*inside* ``arguments``.

Reporting it as a protocol error also put the only actionable text in
``error.data``, which clients deliver as ``null``: the agent saw "Invalid
arguments" and could not self-correct.  That is the consequence; the wrong
mechanism is the defect.

Two validation sites feed this one handler, and both matter:

* ``registry.dispatch`` validates the whole ``arguments`` against the tool schema
* the dispatcher invoke validates ``params`` against the **member** tool's
  schema — the site a grouped create actually reaches

Both previously reported only their FIRST failure, because
``jsonschema.validate()`` raises on it.  A create missing four fields therefore
cost four blind round-trips.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from frisian_mcp.backends.group_dispatcher import build_group_input_schema, make_group_invoke
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

_view = McpView.as_view()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A fixture schema with two required fields and one typed optional, which is
#: everything these cells need.  Deliberately generic: no host schema, field
#: names or data, even though the live reproduction used them.
_MEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "alpha": {"type": "string"},
        "beta": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["alpha", "beta"],
}


def _call(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke *name* through the real view and return the decoded JSON-RPC body."""
    request = rf.post(
        "/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ),
        content_type="application/json",
    )
    request.user = AnonymousUser()
    return json.loads(_view(request).content)


def _tool_error_content(body: dict[str, Any]) -> dict[str, Any]:
    """Assert *body* is an ``isError`` tool result and return its content dict."""
    assert "error" not in body, (
        "input validation was reported as a JSON-RPC protocol error; "
        f"got code {body.get('error', {}).get('code')!r}"
    )
    result = body["result"]
    assert result["isError"] is True
    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def flat_registry() -> ToolRegistry:
    """Return a registry holding one flat tool with required fields."""
    isolated = ToolRegistry()
    isolated.register(
        name="thing_create",
        fn=lambda arguments, request: {"ok": True},
        description="stub",
        input_schema=_MEMBER_SCHEMA,
        permission_classes=[],
        permission_tier="read",
    )
    return isolated


@pytest.fixture()
def group_registry() -> ToolRegistry:
    """Return a registry whose group dispatcher routes to the flat tool."""
    isolated = ToolRegistry()
    isolated.register(
        name="thing_create",
        fn=lambda arguments, request: {"ok": True},
        description="stub",
        input_schema=_MEMBER_SCHEMA,
        permission_classes=[],
        permission_tier="read",
    )
    isolated.register(
        name="grp",
        fn=make_group_invoke("grp", frozenset({"thing_create"}), isolated),
        description="group",
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        group_tool_names=frozenset({"thing_create"}),
    )
    return isolated


# ---------------------------------------------------------------------------
# The mechanism moves — both validation sites
# ---------------------------------------------------------------------------


class TestInputValidationIsAToolError:
    """A failure against the tool's own inputSchema is reported in the result."""

    def test_flat_tool_missing_field_is_a_tool_error(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """registry.dispatch's validation surfaces as isError, not -32602."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {})

        content = _tool_error_content(body)
        assert content["status_code"] == 400
        assert "alpha" in content["error"]

    def test_flat_tool_wrong_type_is_a_tool_error(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """A type violation is an input validation error too, not a malformed request."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {"alpha": "a", "beta": "b", "limit": "not-an-int"})

        content = _tool_error_content(body)
        assert content["status_code"] == 400
        assert "limit" in content["error"] or "integer" in content["error"]

    def test_grouped_call_missing_field_is_a_tool_error(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """
        The dispatcher's params validation surfaces the same way.

        This is the site a grouped create actually reaches — the member tool's
        schema, validated inside the dispatcher invoke rather than in
        ``registry.dispatch``.  Fixing only the outer site would leave the
        production-dominant mount unchanged.
        """
        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(rf, "grp", {"resource": "thing", "action": "create", "params": {}})

        content = _tool_error_content(body)
        assert content["status_code"] == 400
        assert "alpha" in content["error"]

    def test_the_error_text_survives_in_the_payload(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """
        The whole point: the field name must travel where clients read it.

        Previously the name lived only in ``error.data``, which clients deliver
        as ``null``.  Asserting on the serialised content block is what pins
        that it now travels in the payload.
        """
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {})

        text = body["result"]["content"][0]["text"]
        assert "alpha" in text, "the missing field name is not in the payload the client reads"


# ---------------------------------------------------------------------------
# Every missing field at once
# ---------------------------------------------------------------------------


class TestAllValidationErrorsReportedAtOnce:
    """``iter_errors`` replaces ``validate``, so one round-trip names every fault."""

    def test_flat_tool_reports_both_missing_fields(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """Both required fields arrive together, not one per round-trip."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {})

        message = _tool_error_content(body)["error"]
        assert "alpha" in message
        assert "beta" in message, "only the first failure was reported"

    def test_grouped_call_reports_both_missing_fields(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """The dispatcher's params validation reports them all too."""
        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(rf, "grp", {"resource": "thing", "action": "create", "params": {}})

        message = _tool_error_content(body)["error"]
        assert "alpha" in message
        assert "beta" in message, "only the first failure was reported on the grouped path"

    def test_mixed_faults_are_all_reported(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """A missing field and a type violation in one call yield both."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {"alpha": "a", "limit": "not-an-int"})

        message = _tool_error_content(body)["error"]
        assert "beta" in message
        assert "integer" in message

    def test_message_is_deterministic(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """
        Identical input yields an identical message.

        ``iter_errors`` does not promise a stable order, so the formatter sorts.
        Without that, the same failure reads differently between runs and an
        agent cannot tell a new fault from a reshuffled one.
        """
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            first = _tool_error_content(_call(rf, "thing_create", {}))["error"]
            second = _tool_error_content(_call(rf, "thing_create", {}))["error"]

        assert first == second

    def test_single_fault_message_is_unchanged(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """
        One fault still reads exactly as it did before.

        The multi-error join must not add scaffolding to the common case — a
        lone failure is the same sentence ``jsonschema`` always produced.
        """
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {"alpha": "a", "beta": "b", "limit": "nope"})

        assert _tool_error_content(body)["error"] == "'nope' is not of type 'integer'"


# ---------------------------------------------------------------------------
# The escape hatch resolves inward on a grouped call
# ---------------------------------------------------------------------------


class TestEscapeHatchEchoesTheFailingToolsSchema:
    """
    Under ``lite``, the schema echoed is the one that actually failed.

    ``_lite_enrich_error_content`` looks the entry up by the name the caller
    invoked.  On a grouped call that is the *dispatcher*, so the hatch echoed
    ``{resource, action, params}`` — the shape the caller already had — and
    never the member tool's fields.  The hatch could therefore not disclose the
    missing field on the dominant call shape, which is most of the value of
    moving validation into the tool result in the first place.

    Disclosing the member's schema is not an escalation: ``registry.dispatch``
    runs its tier check *before* argument validation, deliberately, so a member
    that reaches validation is one this caller is already permitted to invoke.
    """

    def test_grouped_call_echoes_the_member_schema(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """The echoed schema names the member's fields, not the dispatcher's."""
        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(
                rf,
                "grp",
                {"resource": "thing", "action": "create", "params": {}, "lite": True},
            )

        schema = _tool_error_content(body)["inputSchema"]
        assert "alpha" in schema["properties"], (
            "the escape hatch echoed the dispatcher's schema, not the failing tool's; "
            f"got {sorted(schema.get('properties', {}))}"
        )
        assert schema.get("required") == ["alpha", "beta"]
        assert "params" not in schema["properties"], "dispatcher shape leaked into the echo"

    def test_flat_call_still_echoes_its_own_schema(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """A flat tool has nothing to resolve inward to and is unchanged."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {"lite": True})

        schema = _tool_error_content(body)["inputSchema"]
        assert "alpha" in schema["properties"]

    def test_inward_resolution_is_opt_in_via_lite(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """Without ``lite`` no schema is attached at all — the hatch stays opt-in."""
        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(rf, "grp", {"resource": "thing", "action": "create", "params": {}})

        assert "inputSchema" not in _tool_error_content(body)

    def test_unroutable_grouped_call_falls_back_to_the_dispatcher(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """
        A call that names no member resolves nothing and keeps the outer schema.

        The failure here is against the *dispatcher's* own schema, so the
        dispatcher's schema is the correct thing to echo.

        ``lite`` is required: the escape hatch is opt-in, so without it no
        schema is attached at all and there is nothing to assert about. This
        previously guarded the assertion behind ``if "inputSchema" in content``
        and omitted the flag, which made the branch unreachable and the test
        vacuous -- it passed without checking anything.

        Both directions are asserted, mirroring
        ``test_escape_hatch_echoes_the_member_schema``. The positive alone would
        still pass if inward resolution fired when it must not, and "resolves
        nothing" is the half this test exists for.

        The call omits ``resource``, so there is genuinely no member to resolve
        to. The previous version passed ``resource="thing"`` -- which names a
        member perfectly well -- so it never exercised the fallback its own
        docstring describes.
        """
        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(rf, "grp", {"action": "create", "params": 5, "lite": True})

        schema = _tool_error_content(body)["inputSchema"]
        assert "params" in schema["properties"], (
            "the dispatcher's own schema was not echoed; "
            f"got {sorted(schema.get('properties', {}))}"
        )
        assert (
            "alpha" not in schema["properties"]
        ), "resolved inward to the member schema on a call that names no member"


# ---------------------------------------------------------------------------
# A rejected bare list says how to send one
# ---------------------------------------------------------------------------


class TestBareListErrorNamesTheWrapperKeys:
    """
    CL-14 — "[...] is not of type 'object'" says what is wrong, not what to do.

    Moving the error into the payload made it readable; it still left a caller
    who had just had their list rejected with no way to learn the wrapper key
    except by guessing. One did, in preference to reading it.  Same fault as
    defect A, one level down.

    The keys are read from ``_BULK_LIST_BODY_KEYS`` rather than restated here,
    so this test cannot pass against guidance that has drifted from what the
    invocation layer actually accepts.
    """

    def test_grouped_bulk_list_error_names_the_accepted_keys(
        self, rf: RequestFactory, group_registry: ToolRegistry
    ) -> None:
        """The rejection carries the remedy, not just the diagnosis."""
        from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
            _BULK_LIST_BODY_KEYS,
        )

        with patch("frisian_mcp.views.tool_registry", group_registry):
            body = _call(
                rf,
                "grp",
                {
                    "resource": "thing",
                    "action": "create",
                    "params": [{"alpha": "a", "beta": "b"}],
                },
            )

        message = _tool_error_content(body)["error"]
        assert "is not of type 'object'" in message, "the original diagnosis was lost"
        for key in _BULK_LIST_BODY_KEYS:
            assert key in message, f"accepted wrapper key {key!r} not named in the error"

    def test_ordinary_field_errors_carry_no_wrapper_guidance(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """
        The hint is scoped to the shape it explains.

        Without this the guidance could be appended to every validation
        failure, which would make the common single-object error longer for no
        reason.
        """
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "thing_create", {})

        assert "wrap it under" not in _tool_error_content(body)["error"]


# ---------------------------------------------------------------------------
# Genuine protocol errors stay protocol errors
# ---------------------------------------------------------------------------


class TestGenuineProtocolErrorsAreUnchanged:
    """
    Only tool-``inputSchema`` validation moves.

    The spec's "malformed request" category is about the ``CallToolRequest``
    envelope, and those failures keep their JSON-RPC error — as does an unknown
    tool.  Asserted so the change cannot quietly widen.
    """

    def test_missing_name_is_still_a_protocol_error(self, rf: RequestFactory) -> None:
        """A CallToolRequest with no ``name`` fails the envelope, not a schema."""
        request = rf.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}),
            content_type="application/json",
        )
        request.user = AnonymousUser()
        body = json.loads(_view(request).content)

        assert "error" in body
        assert body["error"]["code"] == -32602

    def test_non_object_arguments_is_still_a_protocol_error(self, rf: RequestFactory) -> None:
        """``arguments`` that is not an object fails the envelope."""
        request = rf.post(
            "/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "thing_create", "arguments": "not-an-object"},
                }
            ),
            content_type="application/json",
        )
        request.user = AnonymousUser()
        body = json.loads(_view(request).content)

        assert "error" in body
        assert body["error"]["code"] == -32602

    def test_unknown_tool_is_still_a_protocol_error(
        self, rf: RequestFactory, flat_registry: ToolRegistry
    ) -> None:
        """An unknown tool stays METHOD_NOT_FOUND, not a tool result."""
        with patch("frisian_mcp.views.tool_registry", flat_registry):
            body = _call(rf, "no_such_tool", {})

        assert "error" in body
        assert body["error"]["code"] == -32601
