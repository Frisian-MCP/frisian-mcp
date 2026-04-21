"""Tests for the FRIESE_MCP_TOOL_MIDDLEWARE system."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from friese_mcp.middleware import build_middleware_chain, load_middleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request() -> Any:
    return RequestFactory().get("/")


def _noop_tool(request: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Minimal tool_fn used as the chain's inner callable."""
    return {"tool": tool_name, "arguments": arguments}


class _SampleMiddleware:
    """Module-level middleware used for import-path testing in TestLoadMiddleware."""

    def __call__(
        self, request: Any, tool_name: str, arguments: dict[str, Any], call_next: Any
    ) -> Any:
        return call_next(request, tool_name, arguments)


# ---------------------------------------------------------------------------
# TestBuildMiddlewareChain
# ---------------------------------------------------------------------------


class TestBuildMiddlewareChain:
    """Tests for build_middleware_chain."""

    def test_empty_middleware_calls_tool_directly(self) -> None:
        """Empty middleware list: tool_fn is called directly."""
        chain = build_middleware_chain(_noop_tool, [])
        result = chain(_make_request(), "my.tool", {"x": 1})
        assert result == {"tool": "my.tool", "arguments": {"x": 1}}

    def test_single_middleware_call_next_invoked(self) -> None:
        """Single middleware: call_next is invoked and result returned."""
        called: list[str] = []

        class LogMiddleware:
            """Log call order."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                called.append("before")
                result = call_next(request, tool_name, arguments)
                called.append("after")
                return result

        chain = build_middleware_chain(_noop_tool, [LogMiddleware()])
        chain(_make_request(), "t", {})
        assert called == ["before", "after"]

    def test_middleware_modifies_arguments(self) -> None:
        """Middleware can inject keys into arguments before call_next."""

        class InjectMiddleware:
            """Inject a key into arguments."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                arguments = {**arguments, "injected": True}
                return call_next(request, tool_name, arguments)

        chain = build_middleware_chain(_noop_tool, [InjectMiddleware()])
        result = chain(_make_request(), "t", {"original": 1})
        assert result["arguments"] == {"original": 1, "injected": True}

    def test_middleware_modifies_result(self) -> None:
        """Middleware can modify the result after call_next returns."""

        class WrapMiddleware:
            """Wrap the result."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                result = call_next(request, tool_name, arguments)
                return {**result, "wrapped": True}

        chain = build_middleware_chain(_noop_tool, [WrapMiddleware()])
        result = chain(_make_request(), "t", {})
        assert result["wrapped"] is True

    def test_multiple_middleware_correct_order(self) -> None:
        """Multiple middleware called outermost-first."""
        order: list[str] = []

        class FirstMiddleware:
            """Records 'first'."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                order.append("first_before")
                result = call_next(request, tool_name, arguments)
                order.append("first_after")
                return result

        class SecondMiddleware:
            """Records 'second'."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                order.append("second_before")
                result = call_next(request, tool_name, arguments)
                order.append("second_after")
                return result

        chain = build_middleware_chain(_noop_tool, [FirstMiddleware(), SecondMiddleware()])
        chain(_make_request(), "t", {})
        assert order == ["first_before", "second_before", "second_after", "first_after"]

    def test_middleware_exception_propagates(self) -> None:
        """A middleware that raises propagates the exception through the chain."""

        class BoomMiddleware:
            """Always raises."""

            def __call__(
                self, request: Any, tool_name: str, arguments: dict, call_next: Any
            ) -> Any:
                raise RuntimeError("boom")

        chain = build_middleware_chain(_noop_tool, [BoomMiddleware()])
        with pytest.raises(RuntimeError, match="boom"):
            chain(_make_request(), "t", {})


# ---------------------------------------------------------------------------
# TestLoadMiddleware
# ---------------------------------------------------------------------------


class TestLoadMiddleware:
    """Tests for load_middleware."""

    @override_settings(FRIESE_MCP_TOOL_MIDDLEWARE=[])
    def test_empty_setting_returns_empty_list(self) -> None:
        """FRIESE_MCP_TOOL_MIDDLEWARE=[] → empty instances list."""
        result = load_middleware()
        assert not result

    @override_settings(FRIESE_MCP_TOOL_MIDDLEWARE=["NoModuleHere"])
    def test_invalid_dotted_path_raises(self) -> None:
        """A path with no module component raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured, match="valid dotted"):
            load_middleware()

    @override_settings(
        FRIESE_MCP_TOOL_MIDDLEWARE=["friese_mcp.nonexistent_module.SomeClass"]
    )
    def test_nonexistent_module_raises(self) -> None:
        """An import-error path raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured, match="could not import"):
            load_middleware()

    @override_settings(
        FRIESE_MCP_TOOL_MIDDLEWARE=[f"{__name__}._SampleMiddleware"]
    )
    def test_valid_path_instantiates_class(self) -> None:
        """A valid dotted path is imported and the class is instantiated."""
        result = load_middleware()
        assert len(result) == 1
        assert isinstance(result[0], _SampleMiddleware)
