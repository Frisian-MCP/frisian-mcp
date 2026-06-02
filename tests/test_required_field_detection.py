"""
PKG-25 — Required field detection for FK fields in DRFSyncDiscovery.

Covers two mechanisms:

1. ``_infer_required``: heuristic that detects FK serializer fields whose
   underlying model field is NOT NULL / no default, marking them required
   even when the DRF serializer says ``required=False``.

2. ``FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES`` setting applied via
   ``_apply_required_overrides``: operator escape hatch for fields that
   introspection cannot detect (computed properties, GenericForeignKeys, etc.).
"""

# pylint: disable=redefined-outer-name,abstract-method
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from rest_framework import serializers
from rest_framework.relations import PrimaryKeyRelatedField, SlugRelatedField

from frisian_mcp.backends.discovery import (
    _apply_required_overrides,
    _infer_required,
)

# ---------------------------------------------------------------------------
# Stub helpers for _infer_required
# ---------------------------------------------------------------------------


def _make_field(
    *,
    is_slug: bool = False,
    null: bool = False,
    has_default: bool = False,
    source: str | None = None,
    has_queryset: bool = True,
    has_model: bool = True,
    field_raises: bool = False,
) -> Any:
    """
    Build a minimal DRF RelatedField-like mock for _infer_required.

    All parameters default to "required FK field" shape:
    - RelatedField (not SlugRelatedField)
    - NOT NULL, no default
    - queryset with an accessible .model
    """
    model_field = MagicMock()
    model_field.null = null
    model_field.has_default.return_value = has_default

    model = MagicMock()
    if field_raises:
        model._meta.get_field.side_effect = Exception("no field")
    else:
        model._meta.get_field.return_value = model_field

    queryset = MagicMock() if has_queryset else None
    if has_queryset:
        if has_model:
            queryset.model = model
        else:
            del queryset.model  # make hasattr() return False

    field_class = SlugRelatedField if is_slug else PrimaryKeyRelatedField
    field = field_class.__new__(field_class)
    field.queryset = queryset
    if source is not None:
        field.source = source
    return field


# ---------------------------------------------------------------------------
# TestInferRequired
# ---------------------------------------------------------------------------


class TestInferRequired:
    """Unit tests for _infer_required."""

    def test_not_null_no_default_returns_true(self) -> None:
        """FK field on NOT NULL / no-default model field is detected as required."""
        field = _make_field(null=False, has_default=False)
        assert _infer_required(field, "parent") is True

    def test_nullable_model_field_returns_false(self) -> None:
        """Nullable model field → field is genuinely optional."""
        field = _make_field(null=True, has_default=False)
        assert _infer_required(field, "parent") is False

    def test_model_field_with_default_returns_false(self) -> None:
        """NOT NULL field that has a DB default does not need to be required."""
        field = _make_field(null=False, has_default=True)
        assert _infer_required(field, "parent") is False

    def test_slug_related_field_returns_false(self) -> None:
        """SlugRelatedField is excluded from this heuristic."""
        field = _make_field(is_slug=True, null=False, has_default=False)
        assert _infer_required(field, "owner") is False

    def test_non_related_field_returns_false(self) -> None:
        """Plain CharField is not a RelatedField — returns False."""
        assert _infer_required(serializers.CharField(), "name") is False

    def test_no_queryset_returns_false(self) -> None:
        """RelatedField with no queryset (e.g. read-only) returns False."""
        field = _make_field(has_queryset=False, null=False, has_default=False)
        assert _infer_required(field, "parent") is False

    def test_queryset_without_model_returns_false(self) -> None:
        """Queryset that has no .model attribute (unusual) returns False."""
        field = _make_field(has_model=False, null=False, has_default=False)
        assert _infer_required(field, "parent") is False

    def test_get_field_raises_returns_false(self) -> None:
        """If model._meta.get_field raises, fall back to False without error."""
        field = _make_field(null=False, has_default=False, field_raises=True)
        assert _infer_required(field, "parent") is False

    def test_source_attribute_used_over_field_name(self) -> None:
        """When field.source is set it is passed to get_field instead of field_name."""
        model_field = MagicMock()
        model_field.null = False
        model_field.has_default.return_value = False

        model = MagicMock()
        model._meta.get_field.return_value = model_field

        queryset = MagicMock()
        queryset.model = model

        field = PrimaryKeyRelatedField.__new__(PrimaryKeyRelatedField)
        field.queryset = queryset
        field.source = "device_role"  # serializer name might be "role"

        _infer_required(field, "role")
        model._meta.get_field.assert_called_once_with("device_role")


# ---------------------------------------------------------------------------
# TestApplyRequiredOverrides
# ---------------------------------------------------------------------------


class TestApplyRequiredOverrides:
    """Unit tests for _apply_required_overrides."""

    def test_override_adds_field_to_required(self) -> None:
        """Field listed in FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES is added to required."""
        schema: dict[str, Any] = {"type": "object", "properties": {"status": {"type": "string"}}}
        overrides = {"devices.create": ["status"]}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = overrides
            _apply_required_overrides(schema, "devices.create")
        assert "status" in schema["required"]

    def test_override_for_different_tool_not_applied(self) -> None:
        """Override for a different tool name must not affect the current tool."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        overrides = {"devices.create": ["status"]}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = overrides
            _apply_required_overrides(schema, "interfaces.create")
        assert "required" not in schema

    def test_field_already_required_deduplicated(self) -> None:
        """A field already in required is not duplicated by the override."""
        schema: dict[str, Any] = {"type": "object", "properties": {}, "required": ["status"]}
        overrides = {"devices.create": ["status", "name"]}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = overrides
            _apply_required_overrides(schema, "devices.create")
        assert schema["required"].count("status") == 1
        assert "name" in schema["required"]

    def test_setting_absent_is_noop(self) -> None:
        """When FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES is not defined, schema is unchanged."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            del mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES
            _apply_required_overrides(schema, "devices.create")
        assert "required" not in schema

    def test_empty_override_dict_is_noop(self) -> None:
        """An empty override dict leaves the schema unchanged."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = {}
            _apply_required_overrides(schema, "devices.create")
        assert "required" not in schema

    def test_none_setting_treated_as_empty(self) -> None:
        """FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = None is treated as no overrides."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = None
            _apply_required_overrides(schema, "devices.create")
        assert "required" not in schema

    def test_multiple_fields_added(self) -> None:
        """Multiple override fields are all added to required."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        overrides = {"devices.create": ["status", "role", "location"]}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = overrides
            _apply_required_overrides(schema, "devices.create")
        assert set(schema["required"]) == {"status", "role", "location"}

    def test_schema_modified_in_place(self) -> None:
        """_apply_required_overrides modifies the schema dict in place (no return value)."""
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        overrides = {"devices.create": ["status"]}
        with patch("frisian_mcp.backends.discovery.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_REQUIRED_FIELD_OVERRIDES = overrides
            _apply_required_overrides(schema, "devices.create")
        # Mutation is visible on the original dict; function returns None implicitly
        assert "status" in schema["required"]
