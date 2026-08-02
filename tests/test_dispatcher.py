"""Tests for @mcp_dispatcher and @mcp_action decorators."""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from rest_framework.permissions import BasePermission

from frisian_mcp.decorators import mcp_action, mcp_dispatcher, mcp_tool
from frisian_mcp.registry import ToolInputError, ToolRegistry

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
    with patch("frisian_mcp.decorators.tool_registry", reg):

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
            def list(
                self, request: Any, params: dict[str, Any]
            ) -> dict[str, Any]:  # pylint: disable=unused-argument
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

    def test_help_mode_no_action(self, isolated_registry: ToolRegistry, rf: RequestFactory) -> None:
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

    def test_create_action(self, isolated_registry: ToolRegistry, rf: RequestFactory) -> None:
        """action='create' with valid params returns {'created': 'My Task'}."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "create", "params": {"title": "My Task"}}, request)
        assert result == {"created": "My Task"}

    def test_list_action(self, isolated_registry: ToolRegistry, rf: RequestFactory) -> None:
        """action='list' with empty params returns {'tasks': []}."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        request = rf.get("/")
        result = entry.fn({"action": "list", "params": {}}, request)
        assert result == {"tasks": []}

    def test_delete_action(self, isolated_registry: ToolRegistry, rf: RequestFactory) -> None:
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

        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("probe", description="Probe dispatcher.")
            class ProbeDispatcher:  # pylint: disable=unused-variable
                """Probe dispatcher for request pass-through testing."""

                @mcp_action("check", description="Capture request.")
                def check(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:  # pylint: disable=unused-argument
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
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="plain.tool", description="A plain tool.", input_schema={})
            def _plain_tool(
                arguments: dict[str, Any], request: Any
            ) -> dict[str, Any]:  # pylint: disable=unused-argument
                """Plain tool function."""
                return {}

            @mcp_dispatcher("combo", description="Combo dispatcher.")
            class ComboDispatcher:
                """Combo dispatcher for coexistence testing."""

                @mcp_action("ping", description="Ping.")
                def ping(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:  # pylint: disable=unused-argument
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
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="flag.tool", description="Flag test.", input_schema={})
            def _flag_tool(
                arguments: dict[str, Any], request: Any
            ) -> dict[str, Any]:  # pylint: disable=unused-argument
                """Flag tool."""
                return {}

        _ = _flag_tool
        entry = reg.get_entry("flag.tool")
        assert entry is not None
        assert entry.is_dispatcher is False

    def test_mcp_dispatcher_is_dispatcher_true(self) -> None:
        """@mcp_dispatcher entry has is_dispatcher=True."""
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

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

    def test_help_action_on_plain_mcp_tool_still_raises(self, rf: RequestFactory) -> None:
        """action='help' on a regular @mcp_tool with an enum schema still raises ToolInputError."""
        reg = ToolRegistry()
        schema_with_enum = {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["go"]}},
        }

        def _plain(
            arguments: Any, request: Any
        ) -> dict[str, Any]:  # pylint: disable=unused-argument
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
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("open", description="Open dispatcher.")
            class OpenDispatcher:  # pylint: disable=unused-variable
                """Open dispatcher."""

                @mcp_action("ping", description="Ping.")
                def ping(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:  # pylint: disable=unused-argument
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
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher(
                "guarded", description="Guarded dispatcher.", permission_classes=[AllowNone]
            )
            class GuardedDispatcher:  # pylint: disable=unused-variable
                """Guarded dispatcher."""

                @mcp_action("ping", description="Ping.")
                def ping(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:  # pylint: disable=unused-argument
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
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher(
                "secured", description="Secured dispatcher.", permission_classes=[DenyAll]
            )
            class SecuredDispatcher:  # pylint: disable=unused-variable
                """Secured dispatcher."""

                @mcp_action("go", description="Go.")
                def go(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:  # pylint: disable=unused-argument
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


# ---------------------------------------------------------------------------
# V11-20 (F3) — invoke-side absence on a tier-capped route
# ---------------------------------------------------------------------------


@pytest.fixture()
def tiered_registry() -> ToolRegistry:
    """Registry with a dispatcher holding one read action and one write action."""
    reg = ToolRegistry()
    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_dispatcher("jobs", description="Manage jobs for testing.")
        class JobsDispatcher:
            """Test dispatcher with mixed-tier actions."""

            @mcp_action("list", description="List jobs.", params={})
            def list(
                self, request: Any, params: dict[str, Any]
            ) -> dict[str, Any]:  # pylint: disable=unused-argument
                """List all jobs."""
                return {"jobs": []}

            @mcp_action("submit", description="Submit a job.", params={}, write=True)
            def submit(
                self, request: Any, params: dict[str, Any]
            ) -> dict[str, Any]:  # pylint: disable=unused-argument
                """Submit a job."""
                return {"submitted": True}

    _ = JobsDispatcher  # suppress unused-variable
    return reg


class TestDispatcherCappedAbsence:
    """On a capped route an above-ceiling action is ABSENT at invoke (V11-20/F3).

    Same ruling as the group dispatcher: the route tier cap defines which
    actions exist; naming an above-ceiling action in a tier error would
    confirm it exists after discovery hid it.
    """

    @staticmethod
    def _capped_read_request(rf: RequestFactory) -> Any:
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # anonymous → read tier
        req._mcp_max_tier = "read"
        return req

    def test_above_ceiling_action_is_absent_not_permission_error(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """A capped read caller invoking the write action gets the unknown-action absence."""
        with pytest.raises(LookupError) as excinfo:
            tiered_registry.dispatch(
                self._capped_read_request(rf), "jobs", {"action": "submit", "params": {}}
            )
        assert not isinstance(excinfo.value, PermissionError)
        assert str(excinfo.value) == "Unknown action 'submit'."

    def test_absence_hint_never_names_hidden_actions(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Did-you-mean candidates come from the caller-VISIBLE set on a capped route.

        'submitx' is one edit from the hidden 'submit'; suggesting it inside
        the absence error would undo the absence one clause later.
        """
        with pytest.raises(LookupError) as excinfo:
            tiered_registry.dispatch(
                self._capped_read_request(rf), "jobs", {"action": "submitx", "params": {}}
            )
        assert "Did you mean" not in str(excinfo.value)

    def test_never_existed_and_above_ceiling_share_one_template(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Byte-parity: absent-because-capped and absent-because-nonexistent look identical."""
        with pytest.raises(LookupError) as above:
            tiered_registry.dispatch(
                self._capped_read_request(rf), "jobs", {"action": "submit", "params": {}}
            )
        with pytest.raises(LookupError) as missing:
            tiered_registry.dispatch(
                self._capped_read_request(rf), "jobs", {"action": "zzznope", "params": {}}
            )
        assert str(above.value).replace("'submit'", "'X'") == str(missing.value).replace(
            "'zzznope'", "'X'"
        )

    def test_capped_route_does_not_leak_enum_via_schema_error(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """A bogus action on a capped route must not enumerate the full action set.

        Without the capped enum-drop in registry.dispatch, jsonschema's
        enum-violation message lists every registered action — including the
        write/admin names tools/list and help hide from this caller.
        """
        with pytest.raises(LookupError) as excinfo:
            tiered_registry.dispatch(
                self._capped_read_request(rf), "jobs", {"action": "zzznope", "params": {}}
            )
        assert "submit" not in str(excinfo.value)

    def test_uncapped_mount_keeps_tier_permission_error(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Legacy uncapped mounts keep the explicit, self-describing tier error."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(PermissionError) as excinfo:
            tiered_registry.dispatch(request, "jobs", {"action": "submit", "params": {}})
        assert "requires 'read_write' permission" in str(excinfo.value)

    def test_uncapped_mount_keeps_full_map_hint(
        self, tiered_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Legacy uncapped mounts keep the full-map did-you-mean self-correction."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(ToolInputError):
            # Uncapped validation still enforces the registration-time enum,
            # so the bogus action is a schema error before invoke — unchanged
            # legacy shape.
            tiered_registry.dispatch(request, "jobs", {"action": "zzznope", "params": {}})


# ---------------------------------------------------------------------------
# TestNegotiationFieldContract (T6)
# ---------------------------------------------------------------------------


class TestNegotiationFieldContract:
    """
    The response-negotiation protocol must be discoverable on the dispatcher path.

    ADR-005 line 73 requires the protocol to be disclosed in the generated
    schema, but ``_merge_negotiation_schema`` was previously applied only on the
    ``@mcp_heavy`` path.  Dispatcher tools — and the
    ``FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD`` backstop, which reads the same
    schema — advertised ``available_modes`` in the probe envelope while never
    telling the agent where to put the fields.  All four modes were in fact
    reachable; only their placement was undiscoverable.
    """

    def test_schema_declares_all_five_negotiation_fields(
        self, isolated_registry: ToolRegistry
    ) -> None:
        """The dispatcher inputSchema declares the negotiation protocol."""
        entry = isolated_registry.get_entry("tasks")
        assert entry is not None
        props = entry.input_schema["properties"]
        for field in ("continuation_token", "mode", "page", "page_size", "filter_keys"):
            assert field in props, f"{field!r} undisclosed on the dispatcher path"

    def test_schema_keeps_action_and_params(self, isolated_registry: ToolRegistry) -> None:
        """Disclosure is additive — the dispatcher contract is unchanged."""
        props = isolated_registry.get_entry("tasks").input_schema["properties"]
        assert set(props["action"]["enum"]) == {"create", "list", "delete"}
        assert props["params"]["additionalProperties"] is True

    def test_mode_enum_is_disclosed(self, isolated_registry: ToolRegistry) -> None:
        """The agent can read the valid modes off the schema, not just the envelope."""
        props = isolated_registry.get_entry("tasks").input_schema["properties"]
        assert set(props["mode"]["enum"]) == {"summary", "paginated", "filtered", "full"}

    def test_params_description_warns_off_nesting(self, isolated_registry: ToolRegistry) -> None:
        """``params``' own description tells the agent the protocol fields go elsewhere."""
        props = isolated_registry.get_entry("tasks").input_schema["properties"]
        assert "top-level" in props["params"]["description"].lower()

    def test_continuation_token_in_params_names_correct_placement(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        A token nested in ``params`` raises an error that teaches the fix.

        Previously it was forwarded to the ViewSet, where the filterset correctly
        rejected it as an unknown filter field — accurate but useless, because it
        never revealed that the key was real and merely misplaced.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(ToolInputError) as excinfo:
            isolated_registry.dispatch(
                request,
                "tasks",
                {"action": "list", "params": {"continuation_token": "abc123"}},
            )
        msg = str(excinfo.value)
        assert "TOP LEVEL" in msg
        assert "continuation_token" in msg
        # Distinguishable from a genuine unknown-filter rejection.
        assert "filter" in msg.lower()

    def test_flat_form_still_sweeps_ordinary_params(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        REGRESSION GUARD: the flat argument form must keep working.

        ``{action, key: val}`` exists deliberately for schema-driven agents (GPT
        function-calling) that cannot nest.  T6 narrows this sweep, so this is the
        highest-risk regression in the change — breaking it would trade a
        documented annoyance for a silent client incompatibility.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = isolated_registry.dispatch(
            request, "tasks", {"action": "create", "title": "flat-form task"}
        )
        assert result == {"created": "flat-form task"}

    def test_colliding_field_names_still_reach_the_action(
        self, isolated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        ``mode``/``page``/``page_size`` are NOT reserved — they collide with real data.

        ``mode`` is a genuine model field on at least one real host application,
        and ``page``/``page_size`` are DRF ``PageNumberPagination`` query
        parameters, so treating them as protocol-only would break legitimate
        filtering and pagination.  Only ``continuation_token`` is unambiguous.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = isolated_registry.dispatch(
            request, "tasks", {"action": "delete", "params": {"id": "7", "mode": "access"}}
        )
        assert result == {"deleted": "7"}
