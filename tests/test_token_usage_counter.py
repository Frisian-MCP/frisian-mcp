"""Unit tests for the token-usage counter (TUR-2, part of TUR-5).

Pure, Django-free helpers: pinned ``cl100k_base`` counting with a deterministic
``approx-char4`` fallback.  These tests pin the fallback maths unconditionally
and the real cl100k_base golden vectors only when the optional tiktoken extra is
installed (marked ``usage_real_tokenizer``).

NOTE on provenance: this default CI venv has NO tiktoken, so
``encoding_name()`` reports ``"approx-char4"`` and every count here runs the
character-based fallback.  The golden-vector class documents the real numbers
and asserts them the moment ``frisian-mcp[usage]`` (+ ``TIKTOKEN_CACHE_DIR`` for
offline) makes the real encoder available -- that is the drift guard.
"""

from __future__ import annotations

import math

import pytest

from frisian_mcp.usage import (
    FALLBACK_ENCODING,
    TOKENIZER_ENCODING,
    count_tokens,
    count_value,
    dumps_for_count,
    encoding_name,
    tiktoken_available,
)

# ---------------------------------------------------------------------------
# encoding_name / provenance
# ---------------------------------------------------------------------------


class TestEncodingProvenance:
    """``encoding_name`` reports which counting path is actually active."""

    def test_encoding_name_matches_availability(self) -> None:
        """The reported name flips with tiktoken availability -- never silent."""
        if tiktoken_available():
            assert encoding_name() == TOKENIZER_ENCODING
        else:
            assert encoding_name() == FALLBACK_ENCODING

    def test_encoding_name_is_one_of_the_two_known_values(self) -> None:
        """The provenance is always one of the two documented values."""
        assert encoding_name() in {TOKENIZER_ENCODING, FALLBACK_ENCODING}

    def test_pinned_encoding_constant_is_cl100k_base(self) -> None:
        """The pinned encoding must not drift without regenerating golden vectors."""
        assert TOKENIZER_ENCODING == "cl100k_base"
        assert FALLBACK_ENCODING == "approx-char4"


# ---------------------------------------------------------------------------
# count_tokens -- determinism + never-raise + empties
# ---------------------------------------------------------------------------


class TestCountTokens:
    """``count_tokens`` is deterministic and never raises into the request path."""

    def test_empty_string_is_zero(self) -> None:
        """An empty string counts as zero tokens."""
        assert count_tokens("") == 0

    @pytest.mark.parametrize("falsy", ["", None])
    def test_falsy_input_is_zero(self, falsy: object) -> None:
        """A falsy input short-circuits to 0 before the encoder is touched."""
        assert count_tokens(falsy) == 0  # type: ignore[arg-type]

    def test_deterministic_across_calls(self) -> None:
        """The same input yields the same count on repeated calls."""
        text = "the quick brown fox jumps over the lazy dog"
        assert count_tokens(text) == count_tokens(text)

    def test_non_negative(self) -> None:
        """A count is never negative."""
        assert count_tokens("anything at all") >= 0

    def test_unicode_and_emoji_do_not_raise(self) -> None:
        """Multi-byte input is handled by whichever counting path is active."""
        for text in ("café", "\U0001f9ea\U0001f512", "你好世界"):
            assert count_tokens(text) >= 1

    def test_fallback_maths_when_tokenizer_absent(self) -> None:
        """On the fallback venv, the count is exactly ``ceil(len/4)``."""
        if tiktoken_available():
            pytest.skip("real tokenizer active; fallback maths not exercised here")
        for text in ("x", "1234", "12345", "hello world"):
            assert count_tokens(text) == math.ceil(len(text) / 4)


@pytest.mark.usage_real_tokenizer
class TestGoldenVectorsRealTokenizer:
    """Real cl100k_base golden vectors -- the drift guard for TUR-2.

    Skipped unless the optional ``frisian-mcp[usage]`` extra provides a working
    encoder.  A tiktoken bump that shifts any of these counts should fail HERE
    (and the nightly canary) rather than silently changing every ``_usage``.
    """

    GOLDEN = {
        "": 0,
        "hello world": 2,
        "the quick brown fox": 4,
        '{"a": 1}': 6,
    }

    def setup_method(self) -> None:
        """Skip the whole class when the real encoder is unavailable."""
        if not tiktoken_available():
            pytest.skip("frisian-mcp[usage] not installed; real cl100k_base unavailable")

    @pytest.mark.parametrize("text, expected", list(GOLDEN.items()))
    def test_golden_vector(self, text: str, expected: int) -> None:
        """Each pinned string counts to its recorded cl100k_base value."""
        assert count_tokens(text) == expected

    def test_provenance_is_real_encoding(self) -> None:
        """With the extra installed, the reported encoding is the real one."""
        assert encoding_name() == TOKENIZER_ENCODING


# ---------------------------------------------------------------------------
# dumps_for_count / count_value -- serialization boundary
# ---------------------------------------------------------------------------


class TestSerializationBoundary:
    """Serialization matches the bytes actually emitted on the wire (TUR-1 sec 2)."""

    def test_str_passes_through_unquoted(self) -> None:
        """A string is counted as-is -- re-serializing would add quotes."""
        assert dumps_for_count("already text") == "already text"

    def test_dict_uses_cpython_default_json(self) -> None:
        """A dict is serialized with CPython-default ``json.dumps``."""
        import json

        obj = {"b": 2, "a": 1}
        assert dumps_for_count(obj) == json.dumps(obj)

    def test_count_value_equals_count_of_dumps(self) -> None:
        """``count_value`` equals counting the serialized form."""
        obj = {"name": "device-01", "status": "active"}
        assert count_value(obj) == count_tokens(dumps_for_count(obj))

    def test_count_value_is_deterministic_for_same_object(self) -> None:
        """``count_value`` is stable for the same object."""
        obj = {"z": 26, "a": 1, "m": 13}
        assert count_value(obj) == count_value(obj)
