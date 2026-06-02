"""Tests for JSON Schema validation inside ToolRegistry.dispatch()."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from frisian_mcp.registry import ToolInputError, ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _echo(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
    """Tool callable that echoes arguments back."""
    return arguments


def _req() -> Any:
    """Return a minimal mock request."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Valid input tests
# ---------------------------------------------------------------------------


class TestValidInputs:
    """Tests confirming that valid arguments pass schema validation."""

    def test_empty_schema_accepts_empty_args(self, registry: ToolRegistry) -> None:
        """An empty schema allows any object, including an empty dict."""
        registry.register("t1", _echo, "T1", {})
        assert registry.dispatch(_req(), "t1", {}) == {}

    def test_empty_schema_accepts_extra_fields(self, registry: ToolRegistry) -> None:
        """An empty schema permits arguments with arbitrary extra fields."""
        registry.register("t2", _echo, "T2", {})
        result = registry.dispatch(_req(), "t2", {"anything": 123})
        assert result == {"anything": 123}

    def test_required_string_field_present(self, registry: ToolRegistry) -> None:
        """A required string field that is present passes validation."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        registry.register("t3", _echo, "T3", schema)
        assert registry.dispatch(_req(), "t3", {"name": "Alice"}) == {"name": "Alice"}

    def test_optional_field_may_be_omitted(self, registry: ToolRegistry) -> None:
        """An optional field may be absent from the arguments."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"tag": {"type": "string"}},
        }
        registry.register("t4", _echo, "T4", schema)
        assert registry.dispatch(_req(), "t4", {}) == {}

    def test_integer_field_with_correct_type(self, registry: ToolRegistry) -> None:
        """An integer field accepts a Python int."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        registry.register("t5", _echo, "T5", schema)
        assert registry.dispatch(_req(), "t5", {"count": 5}) == {"count": 5}

    def test_boolean_field_accepts_true_false(self, registry: ToolRegistry) -> None:
        """A boolean field accepts True and False."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"active": {"type": "boolean"}},
            "required": ["active"],
        }
        registry.register("t6", _echo, "T6", schema)
        assert registry.dispatch(_req(), "t6", {"active": True})["active"] is True

    def test_nested_object_schema(self, registry: ToolRegistry) -> None:
        """A nested object schema validates correctly when structure matches."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                }
            },
            "required": ["user"],
        }
        registry.register("t7", _echo, "T7", schema)
        result = registry.dispatch(_req(), "t7", {"user": {"id": 1}})
        assert result == {"user": {"id": 1}}


# ---------------------------------------------------------------------------
# Invalid input tests
# ---------------------------------------------------------------------------


class TestInvalidInputs:
    """Tests confirming that invalid arguments are rejected with ToolInputError."""

    def test_missing_required_field_raises(self, registry: ToolRegistry) -> None:
        """Omitting a required field raises ToolInputError."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        registry.register("v1", _echo, "V1", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v1", {})

    def test_wrong_type_string_instead_of_integer(self, registry: ToolRegistry) -> None:
        """Supplying a string where an integer is required raises ToolInputError."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        registry.register("v2", _echo, "V2", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v2", {"count": "five"})

    def test_wrong_type_integer_instead_of_string(self, registry: ToolRegistry) -> None:
        """Supplying an integer where a string is required raises ToolInputError."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        }
        registry.register("v3", _echo, "V3", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v3", {"label": 99})

    def test_additional_properties_forbidden(self, registry: ToolRegistry) -> None:
        """Extra fields are rejected when additionalProperties=false."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        }
        registry.register("v4", _echo, "V4", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v4", {"name": "x", "extra": "y"})

    def test_nested_object_missing_nested_required_field(self, registry: ToolRegistry) -> None:
        """A missing required field deep in a nested object raises ToolInputError."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                }
            },
            "required": ["user"],
        }
        registry.register("v5", _echo, "V5", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v5", {"user": {}})

    def test_tool_input_error_message_is_informative(self, registry: ToolRegistry) -> None:
        """ToolInputError carries a human-readable validation message."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        }
        registry.register("v6", _echo, "V6", schema)
        with pytest.raises(ToolInputError) as exc_info:
            registry.dispatch(_req(), "v6", {})
        assert str(exc_info.value)  # message is non-empty

    def test_null_value_for_non_nullable_field(self, registry: ToolRegistry) -> None:
        """Passing null for a non-nullable field raises ToolInputError."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        registry.register("v7", _echo, "V7", schema)
        with pytest.raises(ToolInputError):
            registry.dispatch(_req(), "v7", {"name": None})
