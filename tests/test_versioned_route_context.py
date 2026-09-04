"""Executable reproduction for #77's missing synthetic-route context."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from frisian_mcp.backends.base import ToolDefinition
from frisian_mcp.backends.discovery import DRFSyncDiscovery
from frisian_mcp.backends.invocation import SyncInvocation
from tests.urls import CatalogViewSet, ItemViewSet, OrderViewSet


def _tool(name: str, view_class: type, url_path: str) -> ToolDefinition:
    """Build an auto-discovered list tool located at a versioned URL."""
    return ToolDefinition(
        name=name,
        description="Versioned fixture",
        input_schema={"type": "object", "properties": {}},
        permission_classes=(),
        source="auto",
        view_class=view_class,
        action="list",
        url_path=url_path,
    )


def _outer_request(rf: RequestFactory) -> Any:
    """Build the gateway request from which invocation creates its DRF request."""
    request = rf.post("/mcp/", content_type="application/json")
    request.user = AnonymousUser()
    return request


@pytest.mark.usefixtures("use_test_urls")
class TestVersionedRouteContext:
    """#77: discovery and invocation must retain the selected route version."""

    @pytest.mark.xfail(
        reason="#77: discovery has no URL kwargs for the v2 catalog route", strict=True
    )
    def test_url_path_v2_schema_uses_v2_fields(self) -> None:
        """A: the v2 catalog route must advertise its additional description field."""
        tool = next(
            tool
            for tool in DRFSyncDiscovery().discover_tools()
            if tool.view_class is CatalogViewSet
            and tool.action == "create"
            and tool.url_path == "api/v2/catalog/"
        )
        assert {"description", "name"} <= set(tool.input_schema["properties"])

    @pytest.mark.xfail(
        reason="#77: versioned routes collapse before each version gets a schema", strict=True
    )
    def test_discovery_keeps_one_tool_per_catalog_route_version(self) -> None:
        """B: v1 and v2 are separate API contracts, so both routes need a tool."""
        paths = {
            tool.url_path
            for tool in DRFSyncDiscovery().discover_tools()
            if tool.view_class is CatalogViewSet and tool.action == "list"
        }
        assert paths == {"api/v1/catalog/", "api/v2/catalog/"}

    @pytest.mark.parametrize(
        ("view_class", "url_path"),
        [
            (CatalogViewSet, "api/v2/catalog/"),
            (ItemViewSet, "api/v2/item/"),
        ],
        ids=["url-path", "namespace"],
    )
    @pytest.mark.xfail(
        reason="#77: invocation has no resolver kwargs or resolver match", strict=True
    )
    def test_dispatch_observes_v2_from_its_route(
        self, rf: RequestFactory, view_class: type, url_path: str
    ) -> None:
        """C: dispatch must give both DRF route versioning schemes the v2 route version."""
        result = SyncInvocation().invoke(
            _tool("catalog_list", view_class, url_path), {}, _outer_request(rf)
        )
        assert result.content == {"version": "v2"}

    @pytest.mark.xfail(
        reason="#77: the synthetic invocation has no version context for a no-default route",
        strict=True,
    )
    def test_missing_default_version_returns_not_found(self, rf: RequestFactory) -> None:
        """D: without route kwargs and no default, DRF should reject the synthetic request."""
        result = SyncInvocation().invoke(
            _tool("order_list", OrderViewSet, "api/v2/order/"), {}, _outer_request(rf)
        )
        assert result.is_error
        assert result.content["status_code"] == 404
