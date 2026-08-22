"""Tests for @mcp_tool and @mcp_ignore decorators."""

# pylint: disable=protected-access
from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from rest_framework.permissions import IsAuthenticated

from frisian_mcp.decorators import mcp_ignore, mcp_tool
from frisian_mcp.negotiation import merge_continuation_branch, schema_discloses_continuation
from frisian_mcp.registry import ToolInputError, ToolRegistry, tool_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(_arguments: dict[str, Any], _request: Any) -> None:
    """No-op tool callable used in decorator tests."""


# ---------------------------------------------------------------------------
# @mcp_tool
# ---------------------------------------------------------------------------


class TestMcpTool:
    """Tests for the @mcp_tool decorator."""

    def test_registers_into_global_registry(self) -> None:
        """
        @mcp_tool registers the decorated function in the global tool_registry.

        H2 changed the ``input_schema`` argument: it is no longer passed
        verbatim.  CR-2 narrowed H2's universal disclosure: the backstop still
        mints only where the published schema discloses, but ``@mcp_tool`` no
        longer discloses, so nothing is added here at all.  The schema is
        therefore checked by IDENTITY rather than by shape — the package
        contributes nothing to an ordinary host schema, and that is exactly
        what "back to pre-H2" means.
        """
        schema: dict[str, Any] = {"type": "object", "properties": {"q": {"type": "string"}}}

        with patch.object(tool_registry, "register") as mock_register:

            @mcp_tool(name="test.decorated", description="Test", input_schema=schema)
            def _decorated(_arguments: dict[str, Any], _request: Any) -> None:
                """Decorated test tool placeholder."""

            assert mock_register.call_count == 1
            kwargs = mock_register.call_args.kwargs
            assert kwargs["name"] == "test.decorated"
            assert kwargs["fn"] is _decorated
            assert kwargs["description"] == "Test"
            assert kwargs["permission_classes"] is None
            assert kwargs["permission_tier"] == "read"

            registered = kwargs["input_schema"]
            # CR-2: the schema is published EXACTLY as handed in — not merely
            # "the author's declarations survive alongside ours".  Identity is
            # the assertion, because the package's contribution to an ordinary
            # host schema is now zero and that is a stronger, drift-proof claim
            # than checking for the absence of particular keys.
            assert registered == schema
            assert registered["properties"]["q"] == {"type": "string"}
            # No disclosure on this path: not the protocol key at the top
            # level, not the four colliding fields, not the conditional branch
            # that used to carry them.
            assert "continuation_token" not in registered["properties"]
            assert "mode" not in registered["properties"]
            assert "allOf" not in registered

    def test_does_not_mutate_the_callers_schema(self) -> None:
        """
        H2's merge is non-destructive.

        Host code commonly builds one schema dict and reuses it.  Mutating it
        in place would leak the continuation fields into unrelated tools and
        make registration order significant.
        """
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        original = deepcopy(schema)

        isolated = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="nomutate.test", description="Test", input_schema=schema)
            def _fn(_arguments: dict[str, Any], _request: Any) -> None:
                """Decorated test tool placeholder."""

            _ = _fn

        assert schema == original

    def test_returns_original_callable(self) -> None:
        """@mcp_tool returns the original function unmodified."""
        isolated = ToolRegistry()

        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="ret.test", description="Return test", input_schema={})
            def _fn(_arguments: dict[str, Any], _request: Any) -> str:
                """Test function."""
                return "result"

            assert _fn({}, None) == "result"

    def test_with_permission_classes(self) -> None:
        """@mcp_tool forwards permission_classes to the registry."""
        isolated = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_tool(
                name="perm.test",
                description="Perm test",
                input_schema={},
                permission_classes=[IsAuthenticated],
            )
            def _secured(_arguments: dict[str, Any], _request: Any) -> None:
                """Secured tool placeholder."""

        tools = isolated.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "perm.test"

    def test_tool_callable_via_dispatch(self) -> None:
        """A tool registered via @mcp_tool can be dispatched successfully."""
        isolated = ToolRegistry()

        with patch("frisian_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="dispatch.test", description="Dispatch", input_schema={})
            def _ret(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                """Return a fixed result."""
                return {"ok": True}

        # H7: pin the MCP tier attributes; a bare MagicMock fabricates a tier.
        req = MagicMock(_mcp_effective_tier=None, _mcp_max_tier=None, auth=None)
        result = isolated.dispatch(req, "dispatch.test", {})
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# @mcp_ignore
# ---------------------------------------------------------------------------


class TestMcpIgnore:
    """Tests for the @mcp_ignore decorator."""

    def test_sets_mcp_ignore_attribute_on_function(self) -> None:
        """@mcp_ignore sets _mcp_ignore = True on a function."""

        @mcp_ignore
        def _hidden(_arguments: dict[str, Any], _request: Any) -> None:
            """Hidden function."""

        assert getattr(_hidden, "_mcp_ignore", False) is True

    def test_sets_mcp_ignore_attribute_on_class(self) -> None:
        """@mcp_ignore sets _mcp_ignore = True on a class."""

        @mcp_ignore
        class _HiddenView:
            """Hidden view class."""

        assert getattr(_HiddenView, "_mcp_ignore", False) is True

    def test_returns_original_object_unchanged(self) -> None:
        """@mcp_ignore returns the original object (identity preserved)."""
        original_fn = _noop
        result = mcp_ignore(original_fn)
        assert result is original_fn

    def test_decorated_function_still_callable(self) -> None:
        """A function decorated with @mcp_ignore remains callable."""

        @mcp_ignore
        def _fn(_arguments: dict[str, Any], _request: Any) -> str:
            """Return ok."""
            return "ok"

        assert _fn({}, None) == "ok"

    def test_without_decorator_no_ignore_flag(self) -> None:
        """A plain function does not have _mcp_ignore set."""

        def _plain(_arguments: dict[str, Any], _request: Any) -> None:
            """Plain function."""

        assert getattr(_plain, "_mcp_ignore", False) is False

    @pytest.mark.parametrize("value", [True])
    def test_ignore_flag_is_true_not_truthy(self, value: bool) -> None:
        """_mcp_ignore is exactly True, not just truthy."""

        @mcp_ignore
        def _fn2(_arguments: dict[str, Any], _request: Any) -> None:
            """Fn2 placeholder."""

        assert _fn2._mcp_ignore is value  # type: ignore[attr-defined]


class TestClosedSchemaKeepsItsStrictness:
    """
    H18: a host schema declaring ``additionalProperties: false`` stays closed.

    The continuation branch used to delete that restriction so its four fields
    would validate.  ``ToolRegistry.dispatch`` validates every call against the
    published schema, so this was never a ``tools/list`` presentation detail —
    it converted the host's "reject unknown fields" into the JSON-Schema default
    of accepting them, at runtime, on an ordinary first call, automatically, for
    any schema passed to ``@mcp_tool``.

    The safe fallback is free because negotiation eligibility is *derived from
    the published schema*: a tool that does not disclose does not mint, so the
    caller is never handed a token their own schema forbids them to return.

    CR-2 GENERALISED THIS, which changes what these tests are worth.
    ``@mcp_tool`` no longer merges at all, so every schema it publishes is
    closed-or-open exactly as the host wrote it, and the registration-level
    assertions below now hold for a broader reason than H18.  That makes them
    weaker as H18 evidence, not stronger: they would pass even if
    ``merge_continuation_branch`` went back to deleting the restriction,
    because nothing routes through it from here any more.

    So H18 is pinned directly on the helper as well
    (``test_helper_still_refuses_to_open_a_closed_schema``).  Without that, CR-2
    would silently retire the only guard on the behaviour, and the next caller
    to reach for ``merge_continuation_branch`` would inherit the original
    defect with a green suite.
    """

    CLOSED: dict[str, Any] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    }

    def _register(self, schema: dict[str, Any]) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="item_search", description="t", input_schema=deepcopy(schema))
            def _fn(arguments: dict[str, Any], _request: Any) -> Any:
                return {"got": sorted(arguments)}

            _ = _fn
        return reg

    def test_published_schema_stays_closed(self) -> None:
        """The restriction survives registration."""
        entry = self._register(self.CLOSED).get_entry("item_search")
        assert entry is not None
        assert entry.input_schema.get("additionalProperties") is False

    def test_unknown_field_still_rejected_on_call_one(self) -> None:
        """
        The regression this task named, asserted at the layer that enforces it.

        A schema assertion alone would not have caught the original defect's
        consequence — ``dispatch`` is where the weakening actually bit.
        """
        reg = self._register(self.CLOSED)
        request = RequestFactory().post("/mcp/")
        request.user = None

        with pytest.raises(ToolInputError) as exc:
            reg.dispatch(request, "item_search", {"q": "x", "unexpected_field": "smuggled"})
        assert "unexpected_field" in str(exc.value)

        # The host's own declared field still works — strictness, not breakage.
        assert reg.dispatch(request, "item_search", {"q": "x"}) == {"got": ["q"]}

    def test_closed_schema_does_not_disclose_and_so_cannot_mint(self) -> None:
        """
        The fallback: no safe transformation means no negotiation, not a weakened one.

        Paired with the mint gate, which reads this same predicate, this is what
        makes refusing to transform safe rather than merely conservative.
        """
        entry = self._register(self.CLOSED).get_entry("item_search")
        assert entry is not None
        assert schema_discloses_continuation(entry.input_schema) is False

    def test_open_schema_also_does_not_disclose(self) -> None:
        """
        CR-2 inverted this: the open case does not disclose either.

        It used to assert the opposite — that an open schema still disclosed,
        so H18's refusal could not be over-reaching into the common case.  That
        guard is obsolete rather than violated: ``@mcp_tool`` declines for every
        schema now, open or closed, so there is no narrow fallback left for a
        broad one to swallow.

        Keeping the assertion inverted rather than deleting the test preserves
        the record that this distinction once existed on this path, and fails
        loudly if universal disclosure is ever reintroduced here.
        """
        entry = self._register({"type": "object", "properties": {}}).get_entry("item_search")
        assert entry is not None
        assert schema_discloses_continuation(entry.input_schema) is False

    def test_helper_still_refuses_to_open_a_closed_schema(self) -> None:
        """
        H18 asserted where it now actually lives: on the helper itself.

        Every other test in this class reaches the behaviour through
        ``@mcp_tool``, which post-CR-2 does not call ``merge_continuation_branch``
        — so they no longer exercise H18 at all and would stay green if it
        regressed.  The helper is still exported, still called from the tests
        that cover the branch shape, and is the obvious thing a future
        contributor reaches for when adding negotiation to a new path.  It keeps
        its own guard.
        """
        closed = deepcopy(self.CLOSED)
        merged = merge_continuation_branch(closed)

        # Returned unchanged: the restriction survives and nothing is disclosed.
        assert merged == self.CLOSED
        assert merged["additionalProperties"] is False
        assert schema_discloses_continuation(merged) is False
        # ...and the caller's dict was not mutated on the way through.
        assert closed == self.CLOSED

        # The contrast that makes it a refusal rather than a no-op helper: an
        # open schema of the same shape does still get the branch.
        opened = merge_continuation_branch(
            {"type": "object", "properties": {"q": {"type": "string"}}}
        )
        assert schema_discloses_continuation(opened) is True
