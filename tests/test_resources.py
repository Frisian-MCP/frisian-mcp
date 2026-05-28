"""Tests for MCP resource registry, @mcp_resource decorator, and views."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from friese_mcp.resources import (
    ResourceDefinition,
    ResourceNotFoundError,
    ResourceRegistry,
)
from friese_mcp.views import McpView

_view = McpView.as_view()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_rf = RequestFactory()


def _make_request() -> Any:
    return _rf.get("/")


def _post_rpc(method: str, params: dict[str, Any] | None = None) -> Any:
    req = _rf.post(
        "/mcp/",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ),
        content_type="application/json",
    )
    req.user = AnonymousUser()
    return req


def _make_registry(*definitions: ResourceDefinition) -> ResourceRegistry:
    """Return a fresh ResourceRegistry pre-populated with *definitions*."""
    reg = ResourceRegistry()
    for defn in definitions:
        reg.register(defn)
    return reg


def _text_handler(uri: str, request: Any) -> str:
    return f"content:{uri}"


# ---------------------------------------------------------------------------
# TestResourceDefinition
# ---------------------------------------------------------------------------


class TestResourceDefinition:
    """Tests for the ResourceDefinition dataclass."""

    def test_required_fields(self) -> None:
        """uri_template, name, fn are required; description and mime_type have defaults."""
        defn = ResourceDefinition(uri_template="rag://docs", name="Docs", fn=_text_handler)
        assert defn.uri_template == "rag://docs"
        assert defn.name == "Docs"
        assert defn.fn is _text_handler
        assert defn.description == ""
        assert defn.mime_type == "text/plain"

    def test_custom_mime_type(self) -> None:
        """mime_type can be overridden."""
        defn = ResourceDefinition(
            uri_template="rag://docs",
            name="Docs",
            fn=_text_handler,
            mime_type="application/json",
        )
        assert defn.mime_type == "application/json"

    def test_frozen(self) -> None:
        """ResourceDefinition is immutable."""
        defn = ResourceDefinition(uri_template="rag://docs", name="Docs", fn=_text_handler)
        with pytest.raises(AttributeError):
            defn.name = "Changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestResourceRegistry
# ---------------------------------------------------------------------------


class TestResourceRegistry:
    """Tests for ResourceRegistry."""

    def test_register_and_list(self) -> None:
        """Registered resource appears in list_resources()."""
        reg = _make_registry(
            ResourceDefinition(
                uri_template="rag://docs",
                name="Docs",
                fn=_text_handler,
                description="All docs",
                mime_type="text/plain",
            )
        )
        listing = reg.list_resources()
        assert len(listing) == 1
        assert listing[0] == {
            "uri": "rag://docs",
            "name": "Docs",
            "description": "All docs",
            "mimeType": "text/plain",
        }

    def test_list_multiple_resources(self) -> None:
        """Multiple resources all appear in list_resources()."""
        reg = _make_registry(
            ResourceDefinition(uri_template="rag://a", name="A", fn=_text_handler),
            ResourceDefinition(uri_template="rag://b", name="B", fn=_text_handler),
        )
        uris = {r["uri"] for r in reg.list_resources()}
        assert uris == {"rag://a", "rag://b"}

    def test_list_empty(self) -> None:
        """Empty registry returns empty list."""
        reg = ResourceRegistry()
        assert reg.list_resources() == []

    def test_read_resource_calls_handler(self) -> None:
        """read_resource dispatches to the registered handler."""
        reg = _make_registry(
            ResourceDefinition(uri_template="rag://docs", name="Docs", fn=_text_handler)
        )
        result = reg.read_resource("rag://docs", _make_request())
        assert result == "content:rag://docs"

    def test_read_resource_passes_uri_and_request(self) -> None:
        """read_resource passes uri and request to the handler."""
        received: list[Any] = []

        def capturing_handler(uri: str, request: Any) -> str:
            received.append((uri, request))
            return "ok"

        reg = _make_registry(
            ResourceDefinition(uri_template="rag://x", name="X", fn=capturing_handler)
        )
        req = _make_request()
        reg.read_resource("rag://x", req)
        assert len(received) == 1
        assert received[0][0] == "rag://x"
        assert received[0][1] is req

    def test_read_resource_not_found_raises(self) -> None:
        """read_resource raises ResourceNotFoundError for unknown URIs."""
        reg = ResourceRegistry()
        with pytest.raises(ResourceNotFoundError, match="not found"):
            reg.read_resource("rag://missing", _make_request())

    def test_get_definition_returns_definition(self) -> None:
        """get_definition returns the ResourceDefinition for a known URI."""
        defn = ResourceDefinition(uri_template="rag://docs", name="Docs", fn=_text_handler)
        reg = _make_registry(defn)
        assert reg.get_definition("rag://docs") is defn

    def test_get_definition_returns_none_for_unknown(self) -> None:
        """get_definition returns None for an unknown URI."""
        reg = ResourceRegistry()
        assert reg.get_definition("rag://missing") is None

    def test_register_overwrites_same_uri(self) -> None:
        """Re-registering the same uri_template overwrites the previous entry."""

        def handler_v2(uri: str, request: Any) -> str:
            return "v2"

        reg = _make_registry(
            ResourceDefinition(uri_template="rag://docs", name="v1", fn=_text_handler)
        )
        reg.register(ResourceDefinition(uri_template="rag://docs", name="v2", fn=handler_v2))
        listing = reg.list_resources()
        assert len(listing) == 1
        assert listing[0]["name"] == "v2"


# ---------------------------------------------------------------------------
# TestMcpResourceDecorator
# ---------------------------------------------------------------------------


class TestMcpResourceDecorator:
    """Tests for the @mcp_resource decorator."""

    def test_decorator_registers_resource(self) -> None:
        """@mcp_resource registers the function in the resource_registry."""
        isolated = ResourceRegistry()
        with patch("friese_mcp.decorators.resource_registry", isolated):
            from friese_mcp.decorators import mcp_resource

            @mcp_resource(uri_template="rag://test", name="Test")
            def my_resource(uri: str, request: Any) -> str:
                return "hello"

        listing = isolated.list_resources()
        assert len(listing) == 1
        assert listing[0]["uri"] == "rag://test"
        assert listing[0]["name"] == "Test"

    def test_decorator_returns_function_unchanged(self) -> None:
        """@mcp_resource returns the original function."""
        isolated = ResourceRegistry()
        with patch("friese_mcp.decorators.resource_registry", isolated):
            from friese_mcp.decorators import mcp_resource

            @mcp_resource(uri_template="rag://test", name="Test")
            def my_resource(uri: str, request: Any) -> str:
                return "hello"

        assert my_resource("rag://test", _make_request()) == "hello"

    def test_decorator_with_description_and_mime_type(self) -> None:
        """@mcp_resource passes description and mime_type to the definition."""
        isolated = ResourceRegistry()
        with patch("friese_mcp.decorators.resource_registry", isolated):
            from friese_mcp.decorators import mcp_resource

            @mcp_resource(
                uri_template="rag://json",
                name="JSON Resource",
                description="A JSON resource",
                mime_type="application/json",
            )
            def json_resource(uri: str, request: Any) -> str:
                return "{}"

        listing = isolated.list_resources()
        assert listing[0]["description"] == "A JSON resource"
        assert listing[0]["mimeType"] == "application/json"


# ---------------------------------------------------------------------------
# TestResourcesListView
# ---------------------------------------------------------------------------


class TestResourcesListView:
    """Tests for the resources/list gateway handler."""

    def _call(self, registry: ResourceRegistry) -> Any:
        req = _post_rpc("resources/list")
        with patch("friese_mcp.views.resource_registry", registry):
            resp = _view(req)
        return json.loads(resp.content)

    def test_resources_list_empty(self) -> None:
        """resources/list with empty registry returns empty list."""
        data = self._call(ResourceRegistry())
        assert data["result"]["resources"] == []

    def test_resources_list_returns_registered(self) -> None:
        """resources/list returns all registered resources."""
        reg = _make_registry(
            ResourceDefinition(
                uri_template="rag://docs",
                name="Docs",
                fn=_text_handler,
                description="Documentation",
                mime_type="text/plain",
            )
        )
        data = self._call(reg)
        resources = data["result"]["resources"]
        assert len(resources) == 1
        assert resources[0] == {
            "uri": "rag://docs",
            "name": "Docs",
            "description": "Documentation",
            "mimeType": "text/plain",
        }


# ---------------------------------------------------------------------------
# TestResourcesReadView
# ---------------------------------------------------------------------------


class TestResourcesReadView:
    """Tests for the resources/read gateway handler."""

    def _call(self, params: dict[str, Any], registry: ResourceRegistry) -> Any:
        req = _post_rpc("resources/read", params)
        with patch("friese_mcp.views.resource_registry", registry):
            resp = _view(req)
        return json.loads(resp.content)

    def test_resources_read_returns_contents(self) -> None:
        """resources/read returns text content for a known URI."""
        reg = _make_registry(
            ResourceDefinition(uri_template="rag://docs", name="Docs", fn=_text_handler)
        )
        data = self._call({"uri": "rag://docs"}, reg)
        assert "result" in data
        contents = data["result"]["contents"]
        assert len(contents) == 1
        assert contents[0]["uri"] == "rag://docs"
        assert contents[0]["text"] == "content:rag://docs"
        assert contents[0]["mimeType"] == "text/plain"

    def test_resources_read_custom_mime_type(self) -> None:
        """resources/read returns the correct mimeType from the definition."""

        def json_handler(uri: str, request: Any) -> str:
            return '{"key": "val"}'

        reg = _make_registry(
            ResourceDefinition(
                uri_template="rag://json",
                name="JSON",
                fn=json_handler,
                mime_type="application/json",
            )
        )
        data = self._call({"uri": "rag://json"}, reg)
        assert data["result"]["contents"][0]["mimeType"] == "application/json"

    def test_resources_read_unknown_uri_returns_error(self) -> None:
        """resources/read returns INVALID_PARAMS error for an unknown URI."""
        data = self._call({"uri": "rag://missing"}, ResourceRegistry())
        assert "error" in data
        assert data["error"]["code"] == -32602

    def test_resources_read_missing_uri_returns_error(self) -> None:
        """resources/read with no 'uri' param returns INVALID_PARAMS."""
        data = self._call({}, ResourceRegistry())
        assert "error" in data
        assert data["error"]["code"] == -32602
