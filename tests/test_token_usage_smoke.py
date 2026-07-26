"""Smoke tests for token-usage reporting (TUR-5).

Cheap, deploy-shaped checks: the feature imports cleanly, the counter never
raises regardless of tokenizer availability, and the offline / optional-dep
guarantees hold. These are the checks that catch a broken install or a
network-dependent encoder before the heavier suites run.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke


def test_public_surface_imports() -> None:
    """The documented public API is importable from the package root."""
    from frisian_mcp import usage

    for name in (
        "count_tokens",
        "count_value",
        "dumps_for_count",
        "encoding_name",
        "tiktoken_available",
        "resolve_usage_reporting",
        "parse_flag_value",
        "parse_request_flag",
        "resolve_system_policy",
        "build_usage_block",
        "maybe_attach_usage",
    ):
        assert hasattr(usage, name), name


def test_view_imports_usage_seam() -> None:
    """views.py wires the seam -- import must not blow up if usage is present."""
    from frisian_mcp.views import _usage_success  # noqa: F401


def test_counter_never_raises_regardless_of_tokenizer() -> None:
    """The counter degrades rather than propagating -- the never-500 guarantee."""
    from frisian_mcp.usage import count_tokens, encoding_name

    # Whatever path is active, these must return a value, not raise.
    assert count_tokens("smoke test payload") >= 0
    assert isinstance(encoding_name(), str)


def test_encoding_load_is_offline_safe() -> None:
    """Loading the encoder must not require network.

    It either loads a cached encoding or falls back, but never hangs/raises on a
    missing BPE fetch.  We do not assert WHICH path (that depends on whether
    frisian-mcp[usage] and
    a TIKTOKEN_CACHE_DIR are present); we assert the load resolves to a definite,
    self-describing provenance without error.
    """
    from frisian_mcp.usage import FALLBACK_ENCODING, TOKENIZER_ENCODING, encoding_name

    assert encoding_name() in {TOKENIZER_ENCODING, FALLBACK_ENCODING}


def test_disabled_feature_has_zero_counting_cost() -> None:
    """When reporting resolves OFF, the lazy schema callable is never invoked."""
    from django.test import RequestFactory

    from frisian_mcp.usage import maybe_attach_usage

    invoked: list[int] = []
    result = {"content": [], "isError": False}
    request = RequestFactory().post("/mcp/", content_type="application/json")

    maybe_attach_usage(
        result,
        request=request,
        schema_json=lambda: invoked.append(1),
        arguments={},
        emitted_text="",
    )
    assert invoked == []
    assert "_usage" not in result
