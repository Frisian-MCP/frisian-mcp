"""Tests for @mcp_dispatcher and @mcp_action decorators."""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from rest_framework.permissions import BasePermission

from friese_mcp.decorators import mcp_action, mcp_dispatcher, mcp_tool
from friese_mcp.registry import ToolInputError, ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    """Return a Django RequestFactory."""
    return RequestFactory()


@pytest.fixture()
def isolated_registry() -> ToolRegistry:
    """Return an isolated ToolRegistry with TasksDispatcher pre-registered."""
    reg = ToolRegistry()
    with patch("friese_mcp.decorators.tool_registry", reg):

        @mcp_dispatcher("tasks", description="Manage tasks for testing.")
        class TasksDispatcher:
            """Test dispatcher class."""

            @mcp_action(
                "create",
                description="Create task.",
                params={"title": "required"},
                input_schema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
            )
            def create(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                """Create a task."""
                return {"created": params.get("title")}

            @mcp_action("list", description="List tasks.", params={})
            def list(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                """List all tasks."""
                return {"tasks": []}

            @mcp_action(
                "delete",
                description="Delete task.",
                params={"id": "required"},
            )
            def delete(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                """Delete a task."""
                return {"deleted": params.get("id")}

    _ = TasksDispatcher  # suppress unused-variable
    return reg


# ---------------------------------------------------------------------------
# TestDispatcherRegistration
# ---------------------------------------------------------------------------


class TestDispatcherRegistration:
    """Tests that @mcp_dispatcher correctly registers in ToolRegistry."""

    def test_is_dispatcher_flag_true(self, isolated_registry: ToolRegistry) -> None:
        """Dispatcher entry has is_dispatcher=True."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        assert entry.is_dispatcher is True

    def test_tool_present_in_registry(self, isolated_registry: ToolRegistry) -> None:
        """ToolRegistry contains the 'tasks' tool after class decoration."""
        tools = isolated_registry.list_tools()
        names = [t["name"] for t in tools]
        assert "tasks" in names

    def test_input_schema_action_enum(self, isolated_registry: ToolRegistry) -> None:
        """input_schema action enum contains exactly the three decorated actions."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        schema = entry.input_schema
        enum_values = schema["properties"]["action"]["enum"]
        assert set(enum_values) == {"create", "list", "delete"}
        assert len(enum_values) == 3

    def test_input_schema_params_additional_properties(
        self, isolated_registry: ToolRegistry
    ) -> None:
        """input_schema params property has additionalProperties: true."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        params_prop = entry.input_schema["properties"]["params"]
        assert params_prop["additionalProperties"] is True


# ---------------------------------------------------------------------------
# TestDispatcherHelpMode
# ---------------------------------------------------------------------------


class TestDispatcherHelpMode:
    """Tests that help mode returns the expected structured response."""

    def test_help_mode_no_action(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Calling invoke with action=None returns help:True response."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({}, request)
        assert result["help"] is True

    def test_help_mode_explicit_help(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Calling invoke with action='help' returns help:True response."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "help"}, request)
        assert result["help"] is True

    def test_help_response_includes_all_actions(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Help response includes all three actions with required keys."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({}, request)
        action_names = {a["name"] for a in result["actions"]}
        assert action_names == {"create", "list", "delete"}
        for action in result["actions"]:
            assert "name" in action
            assert "description" in action
            assert "params" in action

    def test_help_response_dispatcher_key(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Help response includes dispatcher: 'tasks' key."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({}, request)
        assert result["dispatcher"] == "tasks"


# ---------------------------------------------------------------------------
# TestDispatcherKnownAction
# ---------------------------------------------------------------------------


class TestDispatcherKnownAction:
    """Tests that known actions are dispatched correctly."""

    def test_create_action(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='create' with valid params returns {'created': 'My Task'}."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "create", "params": {"title": "My Task"}}, request)
        assert result == {"created": "My Task"}

    def test_list_action(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='list' with empty params returns {'tasks': []}."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "list", "params": {}}, request)
        assert result == {"tasks": []}

    def test_delete_action(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='delete' with id param returns {'deleted': '42'}."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "delete", "params": {"id": "42"}}, request)
        assert result == {"deleted": "42"}

    def test_request_passed_through(
        self, isolated_registry: ToolRegistry, rf: RequestFactory  # pylint: disable=unused-argument
    ) -> None:
        """The request object is passed through to the action method."""
        reg = ToolRegistry()
        received: list[Any] = []

        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("probe", description="Probe dispatcher.")
            class ProbeDispatcher:  # pylint: disable=unused-variable
                """Probe dispatcher for request pass-through testing."""

                @mcp_action("check", description="Capture request.")
                def check(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                    """Capture request."""
                    received.append(request)
                    return {}

        entry = reg.get_entry("probe")
        assert entry is not None
        request = rf.get("/mcp/")
        entry.fn({"action": "check"}, request)
        assert len(received) == 1
        assert received[0] is request


# ---------------------------------------------------------------------------
# TestDispatcherUnknownAction
# ---------------------------------------------------------------------------


class TestDispatcherUnknownAction:
    """Tests that unknown actions raise LookupError with helpful messages."""

    def test_typo_suggests_close_match(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='creat' (typo) raises LookupError with close-match hint."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        with pytest.raises(LookupError) as exc_info:
            entry.fn({"action": "creat", "params": {}}, request)
        assert "Did you mean: 'create'" in str(exc_info.value)

    def test_no_match_no_suggestion(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='zzz_no_match' raises LookupError without 'Did you mean'."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        with pytest.raises(LookupError) as exc_info:
            entry.fn({"action": "zzz_no_match"}, request)
        assert "Did you mean" not in str(exc_info.value)

    def test_unknown_action_name_in_message(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Error message includes the unknown action name."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        with pytest.raises(LookupError) as exc_info:
            entry.fn({"action": "zzz_unknown"}, request)
        assert "zzz_unknown" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestDispatcherSchemaValidation
# ---------------------------------------------------------------------------


class TestDispatcherSchemaValidation:
    """Tests per-action input_schema validation via jsonschema."""

    def test_missing_required_field_raises(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='create' with params={} (missing 'title') raises ToolInputError."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        with pytest.raises(ToolInputError):
            entry.fn({"action": "create", "params": {}}, request)

    def test_valid_params_does_not_raise(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='create' with params={'title': 'ok'} does not raise."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "create", "params": {"title": "ok"}}, request)
        assert result == {"created": "ok"}

    def test_no_schema_skips_validation(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='list' (no input_schema) skips validation entirely."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "list", "params": {}}, request)
        assert result == {"tasks": []}


# ---------------------------------------------------------------------------
# TestDispatcherCoexistence
# ---------------------------------------------------------------------------


class TestDispatcherCoexistence:
    """Tests that @mcp_tool and @mcp_dispatcher coexist in the same registry."""

    def test_both_registered_and_retrievable(self) -> None:
        """@mcp_tool and @mcp_dispatcher can coexist; both retrievable by name."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="plain.tool", description="A plain tool.", input_schema={})
            def _plain_tool(arguments: dict[str, Any], request: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
                """Plain tool function."""
                return {}

            @mcp_dispatcher("combo", description="Combo dispatcher.")
            class ComboDispatcher:
                """Combo dispatcher for coexistence testing."""

                @mcp_action("ping", description="Ping.")
                def ping(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                    """Ping action."""
                    return {"pong": True}

        _ = _plain_tool
        _ = ComboDispatcher

        plain_entry = reg.get_entry("plain.tool")
        dispatcher_entry = reg.get_entry("combo")

        assert plain_entry is not None
        assert dispatcher_entry is not None

    def test_mcp_tool_is_dispatcher_false(self) -> None:
        """@mcp_tool entry has is_dispatcher=False."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="flag.tool", description="Flag test.", input_schema={})
            def _flag_tool(arguments: dict[str, Any], request: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
                """Flag tool."""
                return {}

        _ = _flag_tool
        entry = reg.get_entry("flag.tool")
        assert entry is not None
        assert entry.is_dispatcher is False

    def test_mcp_dispatcher_is_dispatcher_true(self) -> None:
        """@mcp_dispatcher entry has is_dispatcher=True."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("flag.dispatcher", description="Flag dispatcher.")
            class FlagDispatcher:
                """Flag dispatcher."""

                @mcp_action("noop", description="No-op.")
                def noop(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    """No-op action."""
                    _ = request, params
                    return {}

        _ = FlagDispatcher
        entry = reg.get_entry("flag.dispatcher")
        assert entry is not None
        assert entry.is_dispatcher is True


# ---------------------------------------------------------------------------
# TestDispatcherHelpModeViaRegistry
# ---------------------------------------------------------------------------


class TestDispatcherHelpModeViaRegistry:
    """Tests that action='help' reaches the dispatcher invoke callable via registry.dispatch."""

    def test_help_action_returns_help_envelope(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """registry.dispatch with action='help' returns the help envelope."""
        request = rf.get("/")
        result = isolated_registry.dispatch(request, "tasks", {"action": "help"})
        assert result["help"] is True
        assert result["dispatcher"] == "tasks"

    def test_help_action_includes_all_actions(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Help envelope via registry.dispatch includes all registered actions."""
        request = rf.get("/")
        result = isolated_registry.dispatch(request, "tasks", {"action": "help"})
        action_names = {a["name"] for a in result["actions"]}
        assert action_names == {"create", "list", "delete"}

    def test_help_action_on_plain_mcp_tool_still_raises(
        self, rf: RequestFactory
    ) -> None:
        """action='help' on a regular @mcp_tool with an enum schema still raises ToolInputError."""
        reg = ToolRegistry()
        schema_with_enum = {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["go"]}},
        }

        def _plain(arguments: Any, request: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
            return {}

        reg.register(
            "plain.tool",
            _plain,
            description="Plain tool.",
            input_schema=schema_with_enum,
        )
        request = rf.get("/")
        with pytest.raises(ToolInputError):
            reg.dispatch(request, "plain.tool", {"action": "help"})

    def test_valid_action_still_dispatches(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """A valid named action dispatches normally through registry.dispatch."""
        request = rf.get("/")
        result = isolated_registry.dispatch(
            request, "tasks", {"action": "create", "params": {"title": "t"}}
        )
        assert result == {"created": "t"}

    def test_invalid_non_help_action_raises_tool_input_error(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """An invalid action (not 'help', not in enum) raises ToolInputError."""
        request = rf.get("/")
        with pytest.raises(ToolInputError):
            isolated_registry.dispatch(request, "tasks", {"action": "totally_wrong"})


# ---------------------------------------------------------------------------
# TestDispatcherPermissionClasses
# ---------------------------------------------------------------------------


class TestDispatcherPermissionClasses:
    """Tests that permission_classes kwarg on @mcp_dispatcher is enforced."""

    def test_no_permission_classes_allows_all(self) -> None:
        """Dispatcher with no permission_classes allows all callers (AllowAny default)."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("open", description="Open dispatcher.")
            class OpenDispatcher:  # pylint: disable=unused-variable
                """Open dispatcher."""

                @mcp_action("ping", description="Ping.")
                def ping(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                    """Ping action."""
                    return {"ok": True}

        entry = reg.get_entry("open")
        assert entry is not None
        assert entry.permission_classes == []

    def test_permission_classes_stored_on_entry(self) -> None:
        """permission_classes passed to @mcp_dispatcher are stored on the registry entry."""

        class AllowNone(BasePermission):
            """Deny all requests."""

            def has_permission(self, request: Any, view: Any) -> bool:
                """Deny."""
                return False

        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher(
                "guarded", description="Guarded dispatcher.", permission_classes=[AllowNone]
            )
            class GuardedDispatcher:  # pylint: disable=unused-variable
                """Guarded dispatcher."""

                @mcp_action("ping", description="Ping.")
                def ping(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                    """Ping action."""
                    return {}

        entry = reg.get_entry("guarded")
        assert entry is not None
        assert AllowNone in entry.permission_classes

    def test_permission_denied_raises_permission_error(self, rf: RequestFactory) -> None:
        """registry.dispatch raises PermissionError when permission class denies."""

        class DenyAll(BasePermission):
            """Deny all requests."""

            def has_permission(self, request: Any, view: Any) -> bool:
                """Deny."""
                return False

        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher(
                "secured", description="Secured dispatcher.", permission_classes=[DenyAll]
            )
            class SecuredDispatcher:  # pylint: disable=unused-variable
                """Secured dispatcher."""

                @mcp_action("go", description="Go.")
                def go(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=unused-argument
                    """Go action."""
                    return {}

        request = rf.get("/")
        with pytest.raises(PermissionError):
            reg.dispatch(request, "secured", {"action": "go"})

    def test_existing_dispatcher_without_permission_classes_still_works(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Regression: dispatchers declared without permission_classes still dispatch."""
        request = rf.get("/")
        result = isolated_registry.dispatch(request, "tasks", {"action": "list"})
        assert result == {"tasks": []}
