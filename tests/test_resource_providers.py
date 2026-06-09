"""Tests for ResourceRegistry.register_provider() — dynamic resource providers."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from frisian_mcp.resources import (
    ResourceDefinition,
    ResourceNotFoundError,
    ResourceRegistry,
)
from frisian_mcp.views import McpView

_view = McpView.as_view()
_rf = RequestFactory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry() -> ResourceRegistry:
    """Return a fresh, isolated ResourceRegistry."""
    return ResourceRegistry()


def _request() -> MagicMock:
    """Return a minimal mock HTTP request."""
    return MagicMock()


def _static_def(uri: str = "file://static.txt") -> ResourceDefinition:
    """Return a minimal static ResourceDefinition."""
    return ResourceDefinition(
        uri_template=uri,
        name="Static",
        fn=lambda _uri, _req: "static content",
    )


# ---------------------------------------------------------------------------
# register_provider — list_fn
# ---------------------------------------------------------------------------


class TestRegisterProviderList:
    """list_fn from registered providers is called by list_resources(request)."""

    def test_provider_entries_appear_in_list(self) -> None:
        """Entries returned by list_fn appear in list_resources()."""
        reg = _registry()
        req = _request()
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://a", "name": "A"}])
        result = reg.list_resources(req)
        uris = [r["uri"] for r in result]
        assert "dyn://a" in uris

    def test_multiple_providers_all_listed(self) -> None:
        """All registered providers contribute to the list."""
        reg = _registry()
        req = _request()
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://alpha", "name": "A"}])
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://beta", "name": "B"}])
        result = reg.list_resources(req)
        uris = [r["uri"] for r in result]
        assert "dyn://alpha" in uris
        assert "dyn://beta" in uris

    def test_static_and_dynamic_coexist(self) -> None:
        """Static @mcp_resource entries and dynamic providers both appear."""
        reg = _registry()
        reg.register(_static_def("file://static.txt"))
        req = _request()
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://x", "name": "X"}])
        result = reg.list_resources(req)
        uris = [r["uri"] for r in result]
        assert "file://static.txt" in uris
        assert "dyn://x" in uris

    def test_list_fn_receives_request(self) -> None:
        """list_fn is called with the actual request object."""
        reg = _registry()
        req = _request()
        received: list[Any] = []

        def _capture_req(r: Any) -> list[dict]:
            received.append(r)
            return []

        reg.register_provider(list_fn=_capture_req)
        reg.list_resources(req)
        assert received == [req]

    def test_no_request_skips_providers(self) -> None:
        """list_resources(None) returns only static entries."""
        reg = _registry()
        reg.register(_static_def())
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://y", "name": "Y"}])
        result = reg.list_resources(None)
        uris = [r["uri"] for r in result]
        assert "dyn://y" not in uris
        assert "file://static.txt" in uris

    def test_no_providers_returns_static_only(self) -> None:
        """Without providers, list_resources returns only static entries."""
        reg = _registry()
        reg.register(_static_def())
        req = _request()
        result = reg.list_resources(req)
        assert len(result) == 1
        assert result[0]["uri"] == "file://static.txt"


# ---------------------------------------------------------------------------
# register_provider — read_fn
# ---------------------------------------------------------------------------


class TestRegisterProviderRead:
    """read_fn from registered providers is tried after static registry misses."""

    def test_read_fn_handles_unknown_uri(self) -> None:
        """read_fn is called for a URI not in the static registry."""
        reg = _registry()
        req = _request()
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda uri, _req: f"content:{uri}",
        )
        result = reg.read_resource("dyn://doc/1", req)
        assert result == "content:dyn://doc/1"

    def test_static_registry_takes_priority(self) -> None:
        """Static registration is tried before any read provider."""
        reg = _registry()
        reg.register(
            ResourceDefinition(
                uri_template="file://known.txt",
                name="Known",
                fn=lambda _uri, _req: "static result",
            )
        )
        req = _request()
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda uri, _req: "provider result",
        )
        result = reg.read_resource("file://known.txt", req)
        assert result == "static result"

    def test_read_fn_none_passes_to_next_provider(self) -> None:
        """A read_fn returning None passes handling to the next provider."""
        reg = _registry()
        req = _request()
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda _uri, _req: None,
        )
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda _uri, _req: "fallback",
        )
        result = reg.read_resource("dyn://anything", req)
        assert result == "fallback"

    def test_no_matching_read_fn_raises(self) -> None:
        """ResourceNotFoundError raised when no provider handles the URI."""
        reg = _registry()
        req = _request()
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda _uri, _req: None,
        )
        with pytest.raises(ResourceNotFoundError):
            reg.read_resource("dyn://unhandled", req)

    def test_read_fn_receives_uri_and_request(self) -> None:
        """read_fn is called with the exact URI and request from the caller."""
        reg = _registry()
        req = _request()
        calls: list[tuple[str, Any]] = []

        def _capture(uri: str, r: Any) -> str:
            calls.append((uri, r))
            return "ok"

        reg.register_provider(list_fn=lambda _req: [], read_fn=_capture)
        reg.read_resource("dyn://target", req)
        assert calls == [("dyn://target", req)]

    def test_provider_without_read_fn_skipped_for_reads(self) -> None:
        """A provider registered without read_fn is skipped during read_resource."""
        reg = _registry()
        req = _request()
        reg.register_provider(list_fn=lambda _req: [])
        reg.register_provider(
            list_fn=lambda _req: [],
            read_fn=lambda _uri, _req: "handled",
        )
        result = reg.read_resource("dyn://any", req)
        assert result == "handled"


# ---------------------------------------------------------------------------
# Integration: resources/list via McpView passes request to providers
# ---------------------------------------------------------------------------


class TestResourcesListIntegration:
    """resources/list MCP method forwards request to providers."""

    def _post(self, reg: ResourceRegistry) -> Any:
        """Post a resources/list JSON-RPC request through McpView with *reg* patched in."""
        req = _rf.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}),
            content_type="application/json",
        )
        with patch("frisian_mcp.views.resource_registry", reg):
            resp = _view(req)
        return json.loads(resp.content)

    def test_resources_list_includes_provider_entries(self) -> None:
        """Dynamic provider entries appear in the resources/list JSON-RPC response."""
        reg = _registry()

        def _provider(_req: Any) -> list[dict]:
            return [{"uri": "dyn://integration", "name": "Integration"}]

        reg.register_provider(list_fn=_provider)
        data = self._post(reg)
        uris = [r["uri"] for r in data["result"]["resources"]]
        assert "dyn://integration" in uris

    def test_resources_list_static_and_dynamic_combined(self) -> None:
        """Both static and provider entries appear in resources/list response."""
        reg = _registry()
        reg.register(_static_def("file://combined.txt"))
        reg.register_provider(list_fn=lambda _req: [{"uri": "dyn://combined", "name": "Combined"}])
        data = self._post(reg)
        uris = [r["uri"] for r in data["result"]["resources"]]
        assert "file://combined.txt" in uris
        assert "dyn://combined" in uris
