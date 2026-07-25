"""Token counting for the opt-in ``_usage`` envelope (TUR-2).

Pure, Django-free helpers.  Encodes text with the pinned tiktoken
``cl100k_base`` encoding when it is available and degrades to a deterministic
character-based approximation otherwise, so the usage feature can never raise
into the dispatcher request path or 500 a response.

The tiktoken dependency is OPTIONAL (installed via the ``frisian-mcp[usage]``
extra).  When it is not installed -- or the encoding object fails to load, e.g.
because there is no network to fetch the BPE ranks on first use -- counting
falls back to ``ceil(len(text) / 4)`` and :func:`encoding_name` reports
``"approx-char4"`` so an operator can always distinguish a real count from the
approximation rather than seeing the ``_usage`` block silently disappear.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

#: The pinned tiktoken encoding.  Do NOT change without regenerating the golden
#: token vectors the test-suite pins against (TUR-5 drift guard) -- a different
#: encoding shifts every count.
TOKENIZER_ENCODING = "cl100k_base"

#: Provenance value reported when tiktoken is unavailable and counting falls
#: back to the character-based approximation.
FALLBACK_ENCODING = "approx-char4"


@lru_cache(maxsize=1)
def _load_encoder() -> Any | None:
    """Return the pinned tiktoken encoder, or ``None`` when it is unavailable.

    Cached so the (potentially expensive, network-touching) load happens once
    per process rather than per request.  Any failure -- tiktoken not
    installed, or the encoding failing to load -- collapses to ``None`` so
    callers transparently use the approximation instead.
    """
    try:
        import tiktoken  # pylint: disable=import-outside-toplevel
    except Exception:  # noqa: BLE001 - any import failure means "unavailable"
        return None
    try:
        return tiktoken.get_encoding(TOKENIZER_ENCODING)
    except Exception:  # noqa: BLE001 - a load failure must not break the request
        return None


def tiktoken_available() -> bool:
    """Return ``True`` when the pinned tiktoken encoder loaded successfully."""
    return _load_encoder() is not None


def encoding_name() -> str:
    """Return the provenance name for counts produced by :func:`count_tokens`.

    ``"cl100k_base"`` when the real tokenizer is active, ``"approx-char4"`` when
    the character-based fallback is in effect.  This is the value that
    populates the ``encoding`` field of the ``_usage`` block (TUR-1 sec 3).
    """
    return TOKENIZER_ENCODING if _load_encoder() is not None else FALLBACK_ENCODING


def _approx_tokens(text: str) -> int:
    """Character-based token approximation: ``ceil(len(text) / 4)``."""
    return math.ceil(len(text) / 4)


def count_tokens(text: str) -> int:
    """Return the token count of *text* under the active encoding.

    Never raises: a falsy input counts as ``0``, and any unexpected tokenizer
    error degrades to the character-based approximation rather than propagating
    into the request path.  Deterministic -- the same input always yields the
    same count for a given encoding.
    """
    if not text:
        return 0
    encoder = _load_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:  # noqa: BLE001 - never break the request path
            return _approx_tokens(text)
    return _approx_tokens(text)


def dumps_for_count(value: Any) -> str:
    """Serialize *value* exactly as the dispatcher serializes wire payloads.

    Uses :func:`json.dumps` with CPython defaults (``ensure_ascii=True`` and
    the default separators) so that a count computed over a re-serialized
    object matches the bytes actually emitted in ``content[0].text`` (TUR-1
    sec 2).  A value that is already a ``str`` is returned unchanged -- the
    emitted result text is counted directly, never re-serialized (which would
    add quotes and change the count).
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def count_value(value: Any) -> int:
    """Serialize *value* via :func:`dumps_for_count` and count the result.

    Convenience for the counting boundary used by ``schema_tokens`` and
    ``request_tokens`` (which count serialized JSON objects).  ``result_tokens``
    counts the already-emitted text directly and can call :func:`count_tokens`.
    """
    return count_tokens(dumps_for_count(value))
