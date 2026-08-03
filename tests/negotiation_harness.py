"""
Shared harness for the response-negotiation conformance suites.

Not a test module (the filename deliberately does not match ``test_*.py``, so
pytest does not collect it).  It holds the request plumbing and the two
dispatcher fixtures used by both ``test_heavy_negotiation_conformance`` and
``test_verify_placement_conformance``, which exercise the same two code paths
from different angles.

Everything here is host-agnostic on purpose: a generic ``widget``/``catalog``
resource, never a real host's schema.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.apps import _install_dispatch_groups
from frisian_mcp.decorators import mcp_action, mcp_dispatcher
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

view = McpView.as_view()

#: Row count of the canned payload.  Large enough that ``summary`` (5 items)
#: and ``paginated`` are unambiguously smaller than ``full``.
ROWS = 200


def payload() -> list[dict[str, Any]]:
    """Return a deterministic, comfortably-oversized list result."""
    return [{"id": i, "name": f"widget-{i}", "blob": "x" * 60} for i in range(ROWS)]


def build_request(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    """Return an anonymous ``tools/call`` request for *name* with *arguments*."""
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
    return request


def call(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke the MCP view for *name* and return the raw response."""
    return view(build_request(rf, name, arguments))


def envelope(response: Any) -> dict[str, Any]:
    """Return the decoded JSON-RPC body."""
    return json.loads(response.content)  # type: ignore[no-any-return]


def tool_result(response: Any) -> Any:
    """Return the tool's decoded result payload."""
    body = envelope(response)
    assert "result" in body, f"expected a tool result, got a JSON-RPC error: {body.get('error')}"
    return json.loads(body["result"]["content"][0]["text"])


def result_bytes(value: Any) -> int:
    """Return the serialised byte length of *value*."""
    return len(json.dumps(value).encode())


def group_registry() -> ToolRegistry:
    """Return an isolated registry holding two flat tools ready to be grouped."""
    reg = ToolRegistry()

    def _list(arguments: dict[str, Any], _request: Any) -> Any:
        # Echo the arguments the dispatcher actually forwarded so that flat-form
        # routing can be asserted on, not inferred.
        return {"rows": payload(), "received": arguments} if arguments else payload()

    def _create(arguments: dict[str, Any], _request: Any) -> Any:
        return {"id": 42, "name": arguments.get("name"), "received": arguments}

    schema = {"type": "object", "properties": {}}
    reg.register("widget_list", _list, "list widgets", schema, permission_tier="read")
    reg.register(
        "widget_create",
        _create,
        "create a widget",
        schema,
        permission_tier="read_write",
        is_write=True,
    )
    return reg


def install_group(reg: ToolRegistry, settings: Any) -> None:
    """Install the ``catalog`` group dispatcher over *reg*'s widget tools."""
    settings.FRISIAN_MCP_DISPATCH_GROUPS = {"catalog": ["widget"]}
    with patch("frisian_mcp.registry.tool_registry", reg):
        _install_dispatch_groups()


def class_dispatcher_registry() -> ToolRegistry:
    """Return an isolated registry holding an ``@mcp_dispatcher`` class tool."""
    reg = ToolRegistry()

    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_dispatcher(name="tasks", description="Task dispatcher")
        class _Tasks:  # pylint: disable=too-few-public-methods
            """Minimal two-action dispatcher: one read, one write."""

            @mcp_action(name="list", description="List tasks")
            def list(self, _request: Any, params: dict[str, Any]) -> Any:
                """Return the canned payload plus the params actually received."""
                return {"rows": payload(), "received": params}

            @mcp_action(name="create", description="Create a task", write=True)
            def create(self, _request: Any, params: dict[str, Any]) -> Any:
                """Return a created object plus the params actually received."""
                return {"id": 7, "title": params.get("title"), "received": params}

    return reg


class CacheStub:
    """
    Dict-backed stand-in for the Django cache.

    A real round-trip matters here: the probe call must *store* an entry that
    the continuation call then *reads*.  Tests that stub ``cache.get`` with a
    fixed return value cannot catch a token that is issued but never stored,
    which is the failure mode this protocol is most exposed to.
    """

    def __init__(self) -> None:
        """Start with an empty backing store."""
        self.store: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value for *key*, or *default*."""
        return self.store.get(key, default)

    def set(self, key: str, value: Any, *_args: Any, **_kwargs: Any) -> None:
        """Store *value* under *key*, ignoring TTL arguments."""
        self.store[key] = value


def probe_then(
    rf: RequestFactory,
    reg: ToolRegistry,
    tool: str,
    probe_args: dict[str, Any],
    follow_up: dict[str, Any] | None,
) -> tuple[dict[str, Any], Any]:
    """
    Drive a full two-call negotiation and return ``(probe_envelope, follow_up_result)``.

    Both calls run against the same dict-backed cache so the continuation
    genuinely redeems the token the probe issued.
    """
    cache = CacheStub()
    with (
        patch("frisian_mcp.views.tool_registry", reg),
        patch("frisian_mcp.registry.tool_registry", reg),
        patch("frisian_mcp.views.django_cache", cache),
        override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=500),
    ):
        probe = tool_result(call(rf, tool, probe_args))
        if follow_up is None:
            return probe, None
        args = {**follow_up, "continuation_token": probe["continuation_token"]}
        return probe, tool_result(call(rf, tool, args))


def write_call(rf: RequestFactory, reg: ToolRegistry, tool: str, arguments: dict[str, Any]) -> Any:
    """
    Invoke a write through *tool* at admin tier and return the decoded result.

    The tier override is what lets an anonymous test request reach a write
    action at all; it is incidental to what these tests measure.
    """
    cache = CacheStub()
    with (
        patch("frisian_mcp.views.tool_registry", reg),
        patch("frisian_mcp.registry.tool_registry", reg),
        patch("frisian_mcp.views.django_cache", cache),
        override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="admin"),
    ):
        return tool_result(call(rf, tool, arguments))
