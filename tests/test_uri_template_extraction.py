"""Tests for URI template variable extraction in ResourceRegistry.read_resource()."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from django.test import RequestFactory

from friese_mcp.resources import (
    ResourceDefinition,
    ResourceNotFoundError,
    ResourceRegistry,
    _handler_accepts_variables,
    _match_uri_template,
)

_rf = RequestFactory()


def _req() -> Any:
    """Return a minimal GET request for use in tests."""
    return _rf.get("/")


# ---------------------------------------------------------------------------
# _match_uri_template unit tests
# ---------------------------------------------------------------------------


class TestMatchUriTemplate:
    """Unit tests for the _match_uri_template helper."""

    def test_exact_no_vars(self) -> None:
        """A template with no variables matches its own literal URI."""
        assert _match_uri_template("file://docs/intro", "file://docs/intro") == {}

    def test_single_var_matches(self) -> None:
        """A single {var} placeholder captures the matching segment."""
        result = _match_uri_template("file://docs/{name}", "file://docs/readme")
        assert result == {"name": "readme"}

    def test_two_vars_matches(self) -> None:
        """Two {var} placeholders are each captured independently."""
        result = _match_uri_template("db://{schema}/{table}", "db://public/users")
        assert result == {"schema": "public", "table": "users"}

    def test_no_match_returns_none(self) -> None:
        """A URI that does not match the template returns None."""
        assert _match_uri_template("file://docs/{name}", "file://other/readme") is None

    def test_extra_path_segment_returns_none(self) -> None:
        """A placeholder does not capture slashes, so extra segments do not match."""
        assert _match_uri_template("file://{name}", "file://a/b") is None

    def test_empty_segment_returns_none(self) -> None:
        """An empty segment after the prefix does not match a required placeholder."""
        assert _match_uri_template("file://{name}", "file://") is None

    def test_literal_only_mismatch(self) -> None:
        """Two literal URIs that differ do not match each other."""
        assert _match_uri_template("file://docs/intro", "file://docs/other") is None

    def test_invalid_identifier_returns_none(self) -> None:
        """A placeholder with a non-identifier name (e.g. {bad-name}) returns None."""
        assert _match_uri_template("file://{bad-name}", "file://something") is None


# ---------------------------------------------------------------------------
# _handler_accepts_variables unit tests
# ---------------------------------------------------------------------------


class TestHandlerAcceptsVariables:
    """Unit tests for the _handler_accepts_variables introspection helper."""

    def test_two_arg_handler_returns_false(self) -> None:
        """A handler with two positional args does not accept a variables dict."""

        def handler(_uri: str, _request: Any) -> str:
            return ""

        assert _handler_accepts_variables(handler) is False

    def test_three_arg_handler_returns_true(self) -> None:
        """A handler with three positional args accepts a variables dict."""

        def handler(_uri: str, _request: Any, _variables: dict) -> str:
            return ""

        assert _handler_accepts_variables(handler) is True

    def test_four_arg_handler_returns_true(self) -> None:
        """Extra positional args beyond three still qualify."""

        def handler(_uri: str, _request: Any, _variables: dict, _extra: Any) -> str:
            return ""

        assert _handler_accepts_variables(handler) is True

    def test_lambda_two_args(self) -> None:
        """A two-arg lambda is not considered variable-accepting."""
        assert _handler_accepts_variables(lambda u, r: "") is False

    def test_lambda_three_args(self) -> None:
        """A three-arg lambda is considered variable-accepting."""
        assert _handler_accepts_variables(lambda u, r, v: "") is True

    def test_broken_signature_returns_false(self) -> None:
        """An object that raises on inspect.signature() is treated as non-accepting."""

        class _Broken:
            """Helper whose __signature__ is None, causing inspect.signature to raise."""

            __signature__: None = None  # type: ignore[assignment]

            def __call__(self) -> str:
                return ""

        assert _handler_accepts_variables(_Broken()) is False


# ---------------------------------------------------------------------------
# ResourceRegistry.read_resource integration tests
# ---------------------------------------------------------------------------


class TestReadResourceTemplateMatching:
    """Integration tests for read_resource() URI template matching."""

    def test_exact_match_takes_priority(self) -> None:
        """An exact-match registration wins over a template match for the same URI."""
        registry = ResourceRegistry()
        request = _req()
        calls: list[str] = []

        def exact_handler(_uri: str, _request: Any) -> str:
            calls.append("exact")
            return "exact-content"

        def tmpl_handler(_uri: str, _request: Any) -> str:
            calls.append("tmpl")
            return "tmpl-content"

        registry.register(ResourceDefinition("file://docs/intro", "intro", exact_handler))
        registry.register(ResourceDefinition("file://docs/{name}", "by-name", tmpl_handler))

        result = registry.read_resource("file://docs/intro", request)
        assert result == "exact-content"
        assert calls == ["exact"]

    def test_template_match_variables_forwarded(self) -> None:
        """Extracted template variables are forwarded to a three-arg handler."""
        registry = ResourceRegistry()
        request = _req()
        captured: dict[str, Any] = {}

        def handler(uri: str, _request: Any, variables: dict[str, str]) -> str:
            captured["uri"] = uri
            captured["vars"] = variables
            return f"got {variables['name']}"

        registry.register(ResourceDefinition("file://docs/{name}", "by-name", handler))

        result = registry.read_resource("file://docs/readme", request)
        assert result == "got readme"
        assert captured["vars"] == {"name": "readme"}

    def test_two_arg_handler_called_without_variables(self) -> None:
        """A legacy two-arg handler is called with (uri, request) only."""
        registry = ResourceRegistry()
        request = _req()

        def handler(uri: str, _request: Any) -> str:
            return f"uri={uri}"

        registry.register(ResourceDefinition("file://docs/{name}", "by-name", handler))

        result = registry.read_resource("file://docs/guide", request)
        assert result == "uri=file://docs/guide"

    def test_multi_var_extraction(self) -> None:
        """Multiple template variables are all extracted and forwarded."""
        registry = ResourceRegistry()
        request = _req()

        def handler(_uri: str, _request: Any, variables: dict[str, str]) -> str:
            return f"{variables['schema']}.{variables['table']}"

        registry.register(ResourceDefinition("db://{schema}/{table}", "db-table", handler))

        result = registry.read_resource("db://public/orders", request)
        assert result == "public.orders"

    def test_non_matching_template_falls_through_to_provider(self) -> None:
        """A URI that matches no template is dispatched to a registered provider."""
        registry = ResourceRegistry()
        request = _req()
        provider_calls: list[str] = []

        def handler(_uri: str, _request: Any) -> str:
            return "handler"

        def provider_read(uri: str, _request: Any) -> str:
            provider_calls.append(uri)
            return "provider-result"

        registry.register(ResourceDefinition("file://docs/{name}", "by-name", handler))
        registry.register_provider(list_fn=lambda r: [], read_fn=provider_read)

        result = registry.read_resource("other://resource", request)
        assert result == "provider-result"
        assert provider_calls == ["other://resource"]

    def test_not_found_raises_resource_not_found_error(self) -> None:
        """ResourceNotFoundError is raised when no handler or provider matches."""
        registry = ResourceRegistry()
        request = _req()
        registry.register(
            ResourceDefinition("file://docs/{name}", "by-name", lambda _u, _r: "")
        )

        with pytest.raises(ResourceNotFoundError):
            registry.read_resource("other://completely/different", request)

    def test_no_slash_captured_by_template_var(self) -> None:
        """A {var} placeholder does not capture slashes — multi-segment URIs do not match."""
        registry = ResourceRegistry()
        request = _req()

        def handler(_uri: str, _request: Any, variables: dict[str, str]) -> str:
            return variables["id"]

        registry.register(ResourceDefinition("item://{id}", "item", handler))

        assert registry.read_resource("item://abc123", request) == "abc123"
        with pytest.raises(ResourceNotFoundError):
            registry.read_resource("item://abc/123", request)

    def test_concurrent_reads_do_not_deadlock(self) -> None:
        """Two concurrent read_resource calls on the same template do not deadlock."""
        registry = ResourceRegistry()
        request = _req()
        results: list[str] = []
        barrier = threading.Barrier(2)

        def slow_handler(_uri: str, _request: Any, variables: dict[str, str]) -> str:
            barrier.wait(timeout=5)
            results.append(variables["x"])
            return variables["x"]

        registry.register(ResourceDefinition("tmpl://{x}", "slow", slow_handler))

        def call(val: str) -> None:
            registry.read_resource(f"tmpl://{val}", request)

        t1 = threading.Thread(target=call, args=("a",))
        t2 = threading.Thread(target=call, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert sorted(results) == ["a", "b"]
