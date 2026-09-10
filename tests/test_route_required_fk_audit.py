"""Discovery-time audit for required FK targets omitted from a route surface."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rest_framework.relations import PrimaryKeyRelatedField

from frisian_mcp.route_audit import _required_fk_outside_surface_findings
from frisian_mcp.route_config import RouteConfig
from frisian_mcp.route_grammar import ToolSurface


def _route() -> RouteConfig:
    return RouteConfig(name="default", path="mcp", allow_list=("*",))


def _entry(*, model: tuple[str, str], write: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        perm_app_label=model[0],
        perm_model=model[1],
        is_write=write,
        view_class=object if write else None,
        perm_drf_action="create" if write else None,
        input_schema={"required": ["parent"]} if write else {},
    )


def _serializer(target: tuple[str, str], *, required: bool = True) -> type:
    meta = SimpleNamespace(
        app_label=target[0], model_name=target[1], label=f"{target[0]}.{target[1]}"
    )
    field = PrimaryKeyRelatedField.__new__(PrimaryKeyRelatedField)
    field.queryset = SimpleNamespace(model=SimpleNamespace(_meta=meta))
    fields = {"parent": field} if required else {"optional_parent": field}
    return type("StubSerializer", (), {"fields": fields})


def test_required_fk_outside_route_surface_is_loud() -> None:
    """A carved write action names its unreachable required target model."""
    entries = {"widget_create": _entry(model=("demo", "widget"), write=True)}
    registry = SimpleNamespace(get_entry=entries.get)
    surface = ToolSurface.build(tool_names=entries)

    with (
        patch("frisian_mcp.registry.tool_registry", registry),
        patch(
            "frisian_mcp.backends.discovery.DRFSyncDiscovery._serializer_class_for",
            return_value=_serializer(("demo", "parent")),
        ),
    ):
        findings = _required_fk_outside_surface_findings(_route(), surface)

    assert [(f.code, f.severity, f.entry) for f in findings] == [("W017", "LOUD", "widget_create")]
    assert "demo.parent" in findings[0].message


def test_visible_or_optional_fk_does_not_warn() -> None:
    """The audit only reports required relations whose target is absent."""
    entries = {
        "widget_create": _entry(model=("demo", "widget"), write=True),
        "parent_list": _entry(model=("demo", "parent")),
    }
    registry = SimpleNamespace(get_entry=entries.get)
    surface = ToolSurface.build(tool_names=entries)

    with (
        patch("frisian_mcp.registry.tool_registry", registry),
        patch(
            "frisian_mcp.backends.discovery.DRFSyncDiscovery._serializer_class_for",
            return_value=_serializer(("demo", "parent")),
        ),
    ):
        assert _required_fk_outside_surface_findings(_route(), surface) == []
