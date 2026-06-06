"""
CRUD payload hardening — Approach B: reject nested data:{} wrapper on write tools.

Agents using REST conventions often send {data: {field: value, ...}} instead of
flat {field: value, ...}. Without this check the server silently creates empty
records because the serializer never sees the expected top-level field keys.

Hardening: when a write tool receives a single-key dict whose key is in
_BULK_LIST_BODY_KEYS and whose value is a dict (not a list), and that key is
not a declared property in the tool's inputSchema, raise ToolInputError with a
clear correction message.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from frisian_mcp.registry import ToolInputError, ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
    },
    "required": ["email"],
}

# A schema that legitimately declares a "data" property (JSON field on model).
_DATA_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "data": {"type": "object"},
    },
    "required": ["name"],
}


def _noop_tool(arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
    return arguments


def _build_request() -> Any:
    req = MagicMock()
    req.user = MagicMock()
    req.user.is_authenticated = True
    req._mcp_max_tier = None  # no tier cap
    return req


@pytest.fixture()
def write_registry() -> ToolRegistry:
    """Registry with a single write tool for contacts."""
    reg = ToolRegistry()
    reg.register(
        name="contacts_create",
        fn=_noop_tool,
        description="Create a contact",
        input_schema=_CONTACT_SCHEMA,
        is_write=True,
    )
    return reg


_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "first_name": {"type": "string"},
    },
    # No required fields — ensures the only reason a rejection could fire
    # on the read tool is the nested-dict check, not missing-required-field.
}


@pytest.fixture()
def read_registry() -> ToolRegistry:
    """Registry with a read-only tool (no required fields to avoid schema noise)."""
    reg = ToolRegistry()
    reg.register(
        name="contacts_list",
        fn=_noop_tool,
        description="List contacts",
        input_schema=_READ_SCHEMA,
        is_write=False,
    )
    return reg


@pytest.fixture()
def data_field_registry() -> ToolRegistry:
    """Registry with a write tool that has a real 'data' JSON field."""
    reg = ToolRegistry()
    reg.register(
        name="events_create",
        fn=_noop_tool,
        description="Create an event",
        input_schema=_DATA_FIELD_SCHEMA,
        is_write=True,
    )
    return reg


# Schema with no required fields — used to isolate guard behaviour from
# jsonschema required-field failures.
_NO_REQUIRED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
    },
}


@pytest.fixture()
def no_required_write_registry() -> ToolRegistry:
    """Write tool with no required fields; lets guard boundary tests run clean."""
    reg = ToolRegistry()
    reg.register(
        name="contacts_create",
        fn=_noop_tool,
        description="Create a contact",
        input_schema=_NO_REQUIRED_SCHEMA,
        is_write=True,
    )
    return reg


# ---------------------------------------------------------------------------
# Rejection tests — Approach B
# ---------------------------------------------------------------------------


class TestNestedDictWrapperRejected:
    """Write tools must reject {data: {...}} and similar wrapper shapes."""

    def test_data_wrapper_raises_tool_input_error(self, write_registry: ToolRegistry) -> None:
        """Sending {data: {email: ...}} to a write tool raises ToolInputError."""
        with pytest.raises(ToolInputError) as exc_info:
            write_registry.dispatch(
                _build_request(),
                "contacts_create",
                {"data": {"email": "a@b.com", "first_name": "Alice"}},
            )
        assert '"data"' in str(exc_info.value)
        assert "flat" in str(exc_info.value).lower()

    def test_error_message_lists_expected_fields(self, write_registry: ToolRegistry) -> None:
        """The error message names the expected flat fields."""
        with pytest.raises(ToolInputError) as exc_info:
            write_registry.dispatch(
                _build_request(),
                "contacts_create",
                {"data": {"email": "a@b.com"}},
            )
        msg = str(exc_info.value)
        assert "email" in msg
        assert "first_name" in msg

    @pytest.mark.parametrize("wrapper_key", ["data", "objects", "items", "_items", "body"])
    def test_all_bulk_wrapper_keys_rejected(
        self, write_registry: ToolRegistry, wrapper_key: str
    ) -> None:
        """All _BULK_LIST_BODY_KEYS trigger rejection when value is a dict."""
        with pytest.raises(ToolInputError):
            write_registry.dispatch(
                _build_request(),
                "contacts_create",
                {wrapper_key: {"email": "a@b.com"}},
            )

    def test_empty_dict_wrapper_rejected(self, write_registry: ToolRegistry) -> None:
        """An empty dict inside the wrapper is still rejected."""
        with pytest.raises(ToolInputError):
            write_registry.dispatch(
                _build_request(),
                "contacts_create",
                {"data": {}},
            )


# ---------------------------------------------------------------------------
# Allowed passthrough tests
# ---------------------------------------------------------------------------


class TestAllowedPayloadShapes:
    """Legitimate payload shapes must not be blocked."""

    def test_flat_create_passes(self, write_registry: ToolRegistry) -> None:
        """Flat {email: ..., first_name: ...} is allowed."""
        result = write_registry.dispatch(
            _build_request(),
            "contacts_create",
            {"email": "a@b.com", "first_name": "Alice"},
        )
        assert result["email"] == "a@b.com"

    def test_bulk_list_body_passes(self, write_registry: ToolRegistry) -> None:
        """{data: [...]} bulk-create list body must not be rejected."""
        result = write_registry.dispatch(
            _build_request(),
            "contacts_create",
            {"data": [{"email": "a@b.com"}, {"email": "b@c.com"}]},
        )
        # The noop tool echoes back arguments unchanged.
        assert result == {"data": [{"email": "a@b.com"}, {"email": "b@c.com"}]}

    def test_legitimate_data_field_passes(self, data_field_registry: ToolRegistry) -> None:
        """A write tool with 'data' in its schema accepts {data: {...}} normally."""
        result = data_field_registry.dispatch(
            _build_request(),
            "events_create",
            {"name": "deploy", "data": {"key": "value"}},
        )
        assert result["name"] == "deploy"

    def test_read_tool_not_affected(self, read_registry: ToolRegistry) -> None:
        """Read tools are not subject to the write-path check."""
        result = read_registry.dispatch(
            _build_request(),
            "contacts_list",
            {"data": {"email": "a@b.com"}},
        )
        assert result == {"data": {"email": "a@b.com"}}

    def test_multi_key_payload_with_data_not_blocked(
        self, write_registry: ToolRegistry
    ) -> None:
        """A payload with multiple keys including 'data' is not the wrapper pattern."""
        result = write_registry.dispatch(
            _build_request(),
            "contacts_create",
            {"email": "a@b.com", "data": {"extra": "value"}},
        )
        assert result["email"] == "a@b.com"

    def test_non_dict_value_in_bulk_key_passthrough(
        self, no_required_write_registry: ToolRegistry
    ) -> None:
        """{data: None} is not a dict — guard must not fire; call succeeds."""
        # isinstance(None, dict) is False so the guard condition short-circuits.
        # Using a no-required schema confirms the guard is silent (not jsonschema).
        result = no_required_write_registry.dispatch(
            _build_request(),
            "contacts_create",
            {"data": None},
        )
        assert result == {"data": None}

    def test_non_bulk_set_key_with_dict_value_passthrough(
        self, no_required_write_registry: ToolRegistry
    ) -> None:
        """{payload: {...}} — key not in _BULK_LIST_BODY_KEYS — guard must not fire."""
        result = no_required_write_registry.dispatch(
            _build_request(),
            "contacts_create",
            {"payload": {"email": "a@b.com"}},
        )
        assert result == {"payload": {"email": "a@b.com"}}

    def test_camelcase_wrapper_key_normalized_then_rejected(
        self, write_registry: ToolRegistry
    ) -> None:
        """{Data: {...}} is normalized to {data: {...}} by camelCase normalization, then rejected."""
        with pytest.raises(ToolInputError) as exc_info:
            write_registry.dispatch(
                _build_request(),
                "contacts_create",
                {"Data": {"email": "a@b.com", "first_name": "Alice"}},
            )
        msg = str(exc_info.value)
        assert "flat" in msg.lower()
        assert '"data"' in msg
