"""
Tests for the ``lite: true`` per-call parameter on ``tools/call``.

Verifies the four contractual behaviours of the lite-mode opt-in:

* On a dispatcher help payload, instructional scaffolding
  (``inputSchema`` / ``description`` / hint text) is stripped and only
  action names remain.
* On a non-dispatcher (or non-help) call, the operation result is returned
  unchanged.
* On a failed call (bad params, validation error, etc.), the tool's
  ``inputSchema`` is re-included on the error response — the failure
  escape hatch.
* The ``lite`` flag is stripped from ``arguments`` before dispatch so the
  underlying tool implementation never observes it.
* ``lite: false`` (or absent) returns the full response unchanged.
"""

# pylint: disable=redefined-outer-name,protected-access

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from frisian_mcp.backends.dispatcher import (
    ActionEntry,
    DispatcherMeta,
    _build_dispatcher_input_schema,
    _make_dispatcher_invoke,
)
from frisian_mcp.protocol import INVALID_PARAMS
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

_view = McpView.as_view()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(rf: RequestFactory, payload: Any) -> Any:
    """Build a POST request with a JSON body."""
    request = rf.post(
        "/mcp/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    request.user = AnonymousUser()
    return request


def _call(
    rf: RequestFactory,
    name: str,
    arguments: dict[str, Any],
    req_id: Any = 1,
) -> Any:
    """POST a JSON-RPC ``tools/call`` for *name* with *arguments*."""
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return _view(_post(rf, body))


def _response_data(response: Any) -> dict[str, Any]:
    """Parse the JSON body of a JsonResponse."""
    return json.loads(response.content)  # type: ignore[no-any-return]


def _result_content(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the parsed ``content[0].text`` JSON from a tools/call response."""
    return json.loads(data["result"]["content"][0]["text"])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_dispatcher_registry() -> ToolRegistry:
    """
    Build a registry with a single dispatcher ``tasks`` and two actions.

    Mirrors the shape that ``@mcp_dispatcher`` produces at runtime but is
    constructed by hand so the test does not depend on decorator side
    effects against the module-level registry.
    """
    reg = ToolRegistry()

    def _create_method(
        instance: Any, request: Any, params: dict[str, Any]  # noqa: ARG001
    ) -> dict[str, Any]:
        return {"created": params.get("title")}

    def _list_method(
        instance: Any, request: Any, params: dict[str, Any]  # noqa: ARG001
    ) -> dict[str, Any]:
        return {"tasks": []}

    actions = {
        "create": ActionEntry(
            name="create",
            description="Create a task.",
            params={"title": "required"},
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            method=_create_method,
            permission_tier="read",
        ),
        "list": ActionEntry(
            name="list",
            description="List tasks.",
            params={},
            input_schema=None,
            method=_list_method,
            permission_tier="read",
        ),
    }
    meta = DispatcherMeta(
        name="tasks",
        description="Manage tasks (test fixture).",
        actions=actions,
    )

    class _Dispatcher:
        """Placeholder class for _make_dispatcher_invoke."""

    invoke = _make_dispatcher_invoke(_Dispatcher, meta)
    reg.register(
        name="tasks",
        fn=invoke,
        description=meta.description,
        input_schema=_build_dispatcher_input_schema(meta),
        is_dispatcher=True,
        dispatcher_meta=meta,
    )
    return reg


@pytest.fixture()
def dispatcher_registry() -> ToolRegistry:
    """Return a fresh registry holding a single dispatcher ``tasks``."""
    return _make_dispatcher_registry()


@pytest.fixture()
def plain_registry() -> ToolRegistry:
    """Return a registry with a single non-dispatcher ``echo`` tool."""
    reg = ToolRegistry()
    reg.register(
        name="echo",
        fn=lambda arguments, request: {"echoed": arguments.get("msg", "")},
        description="Echo the ``msg`` argument back.",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    )
    return reg


@pytest.fixture()
def rf() -> RequestFactory:
    """Return a Django RequestFactory."""
    return RequestFactory()


# ---------------------------------------------------------------------------
# Dispatcher help payload — lite strips scaffolding
# ---------------------------------------------------------------------------


class TestLiteDispatcherHelp:
    """``lite: true`` on a dispatcher help call strips schema and descriptions."""

    def test_lite_help_strips_action_descriptions_and_schemas(
        self, rf: RequestFactory, dispatcher_registry: ToolRegistry
    ) -> None:
        """``lite: true`` reduces each action entry to a plain name string."""
        with patch("frisian_mcp.views.tool_registry", dispatcher_registry):
            data = _response_data(
                _call(rf, "tasks", {"action": "help", "lite": True})
            )

        assert "error" not in data
        content = _result_content(data)
        assert content["help"] is True
        # Actions reduced to plain name strings.
        assert set(content["actions"]) == {"create", "list"}
        # No description / input_schema / params keys leaked through.
        for entry in content["actions"]:
            assert isinstance(entry, str)

    def test_lite_help_default_action_strips_scaffolding(
        self, rf: RequestFactory, dispatcher_registry: ToolRegistry
    ) -> None:
        """``lite: true`` with no action (defaults to help) still strips."""
        with patch("frisian_mcp.views.tool_registry", dispatcher_registry):
            data = _response_data(_call(rf, "tasks", {"lite": True}))

        content = _result_content(data)
        assert content["help"] is True
        for entry in content["actions"]:
            assert isinstance(entry, str)

    def test_lite_false_returns_full_help_payload(
        self, rf: RequestFactory, dispatcher_registry: ToolRegistry
    ) -> None:
        """``lite: false`` returns the full action listing untouched."""
        with patch("frisian_mcp.views.tool_registry", dispatcher_registry):
            data = _response_data(
                _call(rf, "tasks", {"action": "help", "lite": False})
            )

        content = _result_content(data)
        action_entries = content["actions"]
        assert all(isinstance(a, dict) for a in action_entries)
        # Full payload retains description + input_schema + params.
        by_name = {a["name"]: a for a in action_entries}
        assert by_name["create"]["description"] == "Create a task."
        assert by_name["create"]["input_schema"] is not None
        assert by_name["create"]["params"] == {"title": "required"}

    def test_lite_absent_returns_full_help_payload(
        self, rf: RequestFactory, dispatcher_registry: ToolRegistry
    ) -> None:
        """No ``lite`` key in arguments returns the full action listing."""
        with patch("frisian_mcp.views.tool_registry", dispatcher_registry):
            data = _response_data(_call(rf, "tasks", {"action": "help"}))

        content = _result_content(data)
        assert all(isinstance(a, dict) for a in content["actions"])


# ---------------------------------------------------------------------------
# Non-dispatcher / non-help — lite leaves data alone
# ---------------------------------------------------------------------------


class TestLiteNonDispatcher:
    """``lite: true`` never strips actual operation data."""

    def test_lite_true_on_plain_tool_returns_result_unchanged(
        self, rf: RequestFactory, plain_registry: ToolRegistry
    ) -> None:
        """``lite: true`` on a non-dispatcher call leaves the result untouched."""
        with patch("frisian_mcp.views.tool_registry", plain_registry):
            data = _response_data(
                _call(rf, "echo", {"msg": "hello", "lite": True})
            )

        assert data["result"]["isError"] is False
        content = _result_content(data)
        assert content == {"echoed": "hello"}

    def test_lite_true_on_dispatcher_action_invocation_returns_data_unchanged(
        self, rf: RequestFactory, dispatcher_registry: ToolRegistry
    ) -> None:
        """``lite: true`` on a non-help dispatcher action does not strip data."""
        with patch("frisian_mcp.views.tool_registry", dispatcher_registry):
            data = _response_data(
                _call(
                    rf,
                    "tasks",
                    {
                        "action": "create",
                        "params": {"title": "tick"},
                        "lite": True,
                    },
                )
            )

        assert data["result"]["isError"] is False
        content = _result_content(data)
        assert content == {"created": "tick"}


# ---------------------------------------------------------------------------
# ``lite`` is stripped before dispatch
# ---------------------------------------------------------------------------


class TestLiteStrippedFromArguments:
    """The ``lite`` flag must never reach the tool implementation."""

    def test_tool_does_not_observe_lite_flag(self, rf: RequestFactory) -> None:
        """A plain tool's fn receives arguments without the ``lite`` key."""
        seen: dict[str, Any] = {}

        def _capturing_tool(arguments: dict[str, Any], request: Any) -> dict[str, Any]:  # noqa: ARG001
            seen.update(arguments)
            return {"ok": True}

        reg = ToolRegistry()
        reg.register(
            name="capture",
            fn=_capturing_tool,
            description="Capture arguments.",
            input_schema={"type": "object", "additionalProperties": True},
        )

        with patch("frisian_mcp.views.tool_registry", reg):
            _call(rf, "capture", {"foo": "bar", "lite": True})

        assert "lite" not in seen
        assert seen == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Failure escape hatch — lite re-includes inputSchema on errors
# ---------------------------------------------------------------------------


class TestLiteFailureEscapeHatch:
    """A failing ``tools/call`` with ``lite: true`` re-includes ``inputSchema``."""

    def test_lite_bad_params_includes_input_schema_on_invalid_params_error(
        self, rf: RequestFactory, plain_registry: ToolRegistry
    ) -> None:
        """An invalid-args JSON-RPC error embeds the failing tool's schema."""
        with patch("frisian_mcp.views.tool_registry", plain_registry):
            # ``msg`` is required by the schema; omit it to provoke validation.
            data = _response_data(_call(rf, "echo", {"lite": True}))

        assert data["error"]["code"] == INVALID_PARAMS
        err_data = data["error"]["data"]
        assert isinstance(err_data, dict), "lite-mode error.data must be a dict"
        schema = err_data["inputSchema"]
        assert schema["type"] == "object"
        assert "msg" in schema["properties"]
        # The original error string is preserved under ``detail``.
        assert isinstance(err_data["detail"], str)

    def test_lite_false_bad_params_returns_string_data(
        self, rf: RequestFactory, plain_registry: ToolRegistry
    ) -> None:
        """Without ``lite``, the error data stays a plain string (unchanged)."""
        with patch("frisian_mcp.views.tool_registry", plain_registry):
            data = _response_data(_call(rf, "echo", {}))

        assert data["error"]["code"] == INVALID_PARAMS
        assert isinstance(data["error"]["data"], str)

    def test_lite_value_error_includes_input_schema_in_iserror_content(
        self, rf: RequestFactory
    ) -> None:
        """A tool raising ValueError under ``lite: true`` embeds inputSchema."""
        reg = ToolRegistry()

        def _raise_value_error(
            arguments: dict[str, Any], request: Any  # noqa: ARG001
        ) -> dict[str, Any]:
            raise ValueError("bad uuid")

        reg.register(
            name="needs_uuid",
            fn=_raise_value_error,
            description="Always raises ValueError.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        )

        with patch("frisian_mcp.views.tool_registry", reg):
            data = _response_data(
                _call(rf, "needs_uuid", {"id": "not-a-uuid", "lite": True})
            )

        assert data["result"]["isError"] is True
        content = _result_content(data)
        assert content["error"] == "bad uuid"
        assert "inputSchema" in content
        assert content["inputSchema"]["properties"]["id"]["type"] == "string"

    def test_lite_false_value_error_omits_input_schema(
        self, rf: RequestFactory
    ) -> None:
        """Without ``lite``, an isError content block does NOT carry inputSchema."""
        reg = ToolRegistry()

        def _raise_value_error(
            arguments: dict[str, Any], request: Any  # noqa: ARG001
        ) -> dict[str, Any]:
            raise ValueError("bad uuid")

        reg.register(
            name="needs_uuid",
            fn=_raise_value_error,
            description="Always raises ValueError.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        )

        with patch("frisian_mcp.views.tool_registry", reg):
            data = _response_data(_call(rf, "needs_uuid", {"id": "x"}))

        content = _result_content(data)
        assert "inputSchema" not in content


# ---------------------------------------------------------------------------
# Direct exercise of the helper functions for branch coverage
# ---------------------------------------------------------------------------


class TestLiteHelpers:
    """Direct unit tests for the ``views`` lite helper functions."""

    def test_strip_lite_scaffolding_passthrough_when_not_dict(self) -> None:
        """Non-dict payloads (e.g. a list) are returned unchanged."""
        from frisian_mcp.views import _strip_lite_scaffolding

        payload = [1, 2, 3]
        assert _strip_lite_scaffolding(payload) is payload

    def test_strip_lite_scaffolding_passthrough_when_not_help(self) -> None:
        """A dict without ``help: True`` is returned unchanged (operation data)."""
        from frisian_mcp.views import _strip_lite_scaffolding

        payload = {"results": [1, 2], "count": 2}
        assert _strip_lite_scaffolding(payload) == payload

    def test_strip_lite_scaffolding_handles_string_action_entries(self) -> None:
        """Group-dispatcher style ``actions: [str, ...]`` is preserved."""
        from frisian_mcp.views import _strip_lite_scaffolding

        payload = {
            "help": True,
            "group": "catalog",
            "actions": ["list", "retrieve"],
        }
        out = _strip_lite_scaffolding(payload)
        assert out["actions"] == ["list", "retrieve"]

    def test_strip_lite_scaffolding_removes_hints_field(self) -> None:
        """``hints`` is dropped entirely under lite mode."""
        from frisian_mcp.views import _strip_lite_scaffolding

        payload = {
            "help": True,
            "actions": [{"name": "a"}],
            "hints": {"a": "use action='help' to discover"},
        }
        out = _strip_lite_scaffolding(payload)
        assert "hints" not in out

    def test_strip_lite_scaffolding_drops_string_field_with_scaffolding_text(
        self,
    ) -> None:
        """A top-level string value containing ``"use action="`` is dropped."""
        from frisian_mcp.views import _strip_lite_scaffolding

        payload = {
            "help": True,
            "actions": [],
            "tip": "Use action='help' to list available resources.",
            "group": "catalog",
        }
        out = _strip_lite_scaffolding(payload)
        assert "tip" not in out
        # Non-scaffolding fields remain.
        assert out["group"] == "catalog"

    def test_lite_enrich_error_content_no_op_when_lite_false(self) -> None:
        """``lite=False`` returns the content dict unchanged."""
        from frisian_mcp.views import _lite_enrich_error_content

        content = {"error": "boom"}
        assert _lite_enrich_error_content(content, "any.tool", False) is content

    def test_lite_enrich_error_content_no_op_for_unknown_tool(self) -> None:
        """A tool not in the registry returns the content unchanged."""
        from frisian_mcp.views import _lite_enrich_error_content

        reg = ToolRegistry()
        content = {"error": "boom"}
        with patch("frisian_mcp.views.tool_registry", reg):
            assert _lite_enrich_error_content(content, "missing", True) is content
