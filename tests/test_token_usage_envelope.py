"""Unit tests for the ``_usage`` envelope assembly (TUR-4, part of TUR-5).

Covers ``build_usage_block`` (pure counting maths) and ``maybe_attach_usage``
(the opt-in-gated seam): totals, key shape, additive-only mutation, the
zero-cost / lazy-schema guarantee when disabled, and that a system ``deny``
suppresses attachment even with a request flag on.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.test import RequestFactory, override_settings

from frisian_mcp.usage import (
    POLICY_ALLOW,
    POLICY_DENY,
    USAGE_HEADER_META,
    USAGE_IN_CONTENT_SETTING,
    USAGE_POLICY_SETTING,
    USAGE_REPORTING_SETTING,
    build_usage_block,
    count_tokens,
    count_value,
    encoding_name,
    maybe_attach_usage,
)

rf = RequestFactory()

_USAGE_KEYS = {"schema_tokens", "request_tokens", "result_tokens", "total_tokens", "encoding"}


def _request(*, header: str | None = None) -> Any:
    extra = {USAGE_HEADER_META: header} if header is not None else {}
    return rf.post("/mcp/", content_type="application/json", **extra)


def _enabled() -> Any:
    return override_settings(**{USAGE_REPORTING_SETTING: True})


# ---------------------------------------------------------------------------
# build_usage_block -- pure counting
# ---------------------------------------------------------------------------


class TestBuildUsageBlock:
    """TestBuildUsageBlock tests."""

    def test_exact_key_set(self) -> None:
        """Exact key set."""
        block = build_usage_block(schema_text="{}", arguments={}, emitted_text="x")
        assert set(block) == _USAGE_KEYS

    def test_total_is_sum_of_parts(self) -> None:
        """Total is sum of parts."""
        block = build_usage_block(
            schema_text='{"type":"object"}',
            arguments={"name": "device-01"},
            emitted_text='{"count": 3}',
        )
        assert (
            block["total_tokens"]
            == block["schema_tokens"] + block["request_tokens"] + block["result_tokens"]
        )

    def test_counts_bind_to_the_given_inputs(self) -> None:
        """Counts bind to the given inputs."""
        schema, emitted = '{"type":"object"}', '{"ok": true}'
        args = {"resource": "device", "action": "list"}
        block = build_usage_block(schema_text=schema, arguments=args, emitted_text=emitted)
        assert block["schema_tokens"] == count_tokens(schema)
        assert block["request_tokens"] == count_value(args)
        assert block["result_tokens"] == count_tokens(emitted)

    def test_encoding_provenance_present(self) -> None:
        """Encoding provenance present."""
        block = build_usage_block(schema_text="", arguments=None, emitted_text="")
        assert block["encoding"] == encoding_name()

    def test_all_counts_are_non_negative_ints(self) -> None:
        """All counts are non negative ints."""
        block = build_usage_block(schema_text="", arguments=None, emitted_text="")
        for key in ("schema_tokens", "request_tokens", "result_tokens", "total_tokens"):
            assert isinstance(block[key], int) and block[key] >= 0


# ---------------------------------------------------------------------------
# maybe_attach_usage -- opt-in gate + additive mutation
# ---------------------------------------------------------------------------


class TestMaybeAttachUsageDisabled:
    """TestMaybeAttachUsageDisabled tests."""

    def test_disabled_returns_object_untouched(self) -> None:
        """Disabled returns object untouched."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        before = dict(result)
        out = maybe_attach_usage(
            result, request=_request(), schema_json="{}", arguments={}, emitted_text="hi"
        )
        assert out is result
        assert "_usage" not in out
        assert out == before

    def test_disabled_does_not_invoke_lazy_schema(self) -> None:
        # The possibly-expensive schema build must be skipped entirely when OFF.
        """Disabled does not invoke lazy schema."""
        calls: list[int] = []

        def _schema() -> str:
            calls.append(1)
            return "{}"

        maybe_attach_usage(
            {"content": [], "isError": False},
            request=_request(),
            schema_json=_schema,
            arguments={},
            emitted_text="",
        )
        assert calls == []

    def test_system_deny_suppresses_even_with_request_on(self) -> None:
        """System deny suppresses even with request on."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with override_settings(
            **{USAGE_REPORTING_SETTING: True, USAGE_POLICY_SETTING: POLICY_DENY}
        ):
            out = maybe_attach_usage(
                result,
                request=_request(header="on"),
                schema_json="{}",
                arguments={},
                emitted_text="hi",
            )
        assert "_usage" not in out


class TestMaybeAttachUsageEnabled:
    """TestMaybeAttachUsageEnabled tests."""

    def test_enabled_adds_usage_sibling(self) -> None:
        """Enabled adds usage sibling."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with _enabled():
            out = maybe_attach_usage(
                result, request=_request(), schema_json="{}", arguments={}, emitted_text="hi"
            )
        assert set(out["_usage"]) == _USAGE_KEYS

    def test_enabled_is_additive_only(self) -> None:
        # content/isError must be untouched -- _usage is a pure sibling addition.
        """Enabled is additive only."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with _enabled():
            maybe_attach_usage(
                result, request=_request(), schema_json="{}", arguments={}, emitted_text="hi"
            )
        assert result["content"] == [{"type": "text", "text": "hi"}]
        assert result["isError"] is False

    def test_allow_policy_enables_without_global(self) -> None:
        """Allow policy enables without global."""
        result = {"content": [], "isError": False}
        with override_settings(**{USAGE_POLICY_SETTING: POLICY_ALLOW}):
            out = maybe_attach_usage(
                result, request=_request(), schema_json="{}", arguments={}, emitted_text=""
            )
        assert "_usage" in out

    def test_result_tokens_bind_to_emitted_text(self) -> None:
        """Result tokens bind to emitted text."""
        emitted = '{"devices": ["a", "b", "c"]}'
        result = {"content": [{"type": "text", "text": emitted}], "isError": False}
        with _enabled():
            maybe_attach_usage(
                result, request=_request(), schema_json="{}", arguments={}, emitted_text=emitted
            )
        assert result["_usage"]["result_tokens"] == count_tokens(emitted)

    def test_none_schema_counts_as_empty(self) -> None:
        """None schema counts as empty."""
        result = {"content": [], "isError": False}
        with _enabled():
            maybe_attach_usage(
                result, request=_request(), schema_json=lambda: None, arguments={}, emitted_text=""
            )
        assert result["_usage"]["schema_tokens"] == 0


def _raising_schema() -> str:
    """A lazy schema callable that raises, to exercise the TUR-15 boundary."""
    raise RuntimeError("schema build boom")


class TestMaybeAttachUsageNeverBreaks:
    """TUR-15: a failure on the enabled path must never break the tool response.

    An opt-in observability block must never turn a good ``tools/call`` into a
    500.  On any exception while producing ``_usage``, the response is returned
    byte-identical (no ``_usage``, no orphan content line) and a warning logged.
    """

    def test_raising_schema_callable_returns_unchanged(self) -> None:
        """A schema callable that raises -> response returned, no _usage, byte-identical."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        before = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with _enabled():
            out = maybe_attach_usage(
                result,
                request=_request(),
                schema_json=_raising_schema,
                arguments={},
                emitted_text="hi",
            )
        assert out is result
        assert "_usage" not in out
        assert out == before

    def test_non_serializable_arguments_return_unchanged(self) -> None:
        """A non-JSON-serializable argument -> response returned, no _usage."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        before = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with _enabled():
            out = maybe_attach_usage(
                result,
                request=_request(),
                schema_json="{}",
                arguments={"bad": object()},
                emitted_text="hi",
            )
        assert "_usage" not in out
        assert out == before

    def test_non_serializable_schema_value_returns_unchanged(self) -> None:
        """A schema object json.dumps cannot serialize -> response returned, no _usage."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with _enabled():
            out = maybe_attach_usage(
                result,
                request=_request(),
                schema_json={"bad": object()},
                arguments={},
                emitted_text="hi",
            )
        assert "_usage" not in out

    def test_failure_leaves_no_orphan_content_line(self) -> None:
        """With the content line ON, a failure must not append a partial line."""
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        with override_settings(**{USAGE_REPORTING_SETTING: True, USAGE_IN_CONTENT_SETTING: True}):
            out = maybe_attach_usage(
                result,
                request=_request(),
                schema_json="{}",
                arguments={"bad": object()},
                emitted_text="hi",
            )
        assert "_usage" not in out
        assert out["content"] == [{"type": "text", "text": "hi"}]

    def test_failure_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """The failure path logs a WARNING rather than silently swallowing."""
        result = {"content": [], "isError": False}
        with _enabled(), caplog.at_level(logging.WARNING, logger="frisian_mcp.usage.envelope"):
            maybe_attach_usage(
                result,
                request=_request(),
                schema_json=_raising_schema,
                arguments={},
                emitted_text="",
            )
        assert any(record.levelno == logging.WARNING for record in caplog.records)
