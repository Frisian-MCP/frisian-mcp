"""Attach the opt-in ``_usage`` block to dispatcher responses (TUR-4).

The ``_usage`` block is an additive sibling of ``content``/``isError`` in the
``tools/call`` result (TUR-1 sec 1).  It carries four integer token counts plus
an ``encoding`` provenance field:

    {"schema_tokens": int, "request_tokens": int, "result_tokens": int,
     "total_tokens": int, "encoding": "cl100k_base" | "approx-char4"}

Counting boundary (TUR-1 sec 2):

* ``schema_tokens``  -- the tool ``inputSchema`` actually surfaced to *this*
  caller (tier-filtered for dispatchers), serialized with ``json.dumps``.
* ``request_tokens`` -- the original inbound tool arguments, serialized.
* ``result_tokens``  -- the exact emitted ``content[0].text`` for this exit
  path (lean envelope, probe, full result, ...), counted directly.
* ``total_tokens``   -- the integer sum of the three.

The whole feature is opt-in and OFF by default: :func:`maybe_attach_usage`
resolves the layered opt-in first (TUR-3) and, when disabled, returns the
result object untouched so the response is byte-identical to today.  The
possibly-nontrivial schema build is passed as a lazy callable so it is skipped
entirely when reporting is disabled -- there is no measurable cost on the
default path.
"""

from __future__ import annotations

import logging
from typing import Any

from frisian_mcp.usage.counter import count_tokens, count_value, dumps_for_count, encoding_name
from frisian_mcp.usage.resolver import resolve_usage_in_content, resolve_usage_reporting

logger = logging.getLogger(__name__)


def build_usage_block(*, schema_text: str, arguments: Any, emitted_text: str) -> dict[str, Any]:
    """Return the ``_usage`` dict for the given counting inputs.

    Pure: a function of its arguments and the active encoding only.  All three
    inputs are counted at the TUR-1 boundary -- ``schema_text`` and
    ``emitted_text`` are already the exact strings to count; ``arguments`` is
    serialized via the shared :func:`~frisian_mcp.usage.counter.count_value`
    boundary so request bytes match the wire.
    """
    schema_tokens = count_tokens(schema_text)
    request_tokens = count_value(arguments)
    result_tokens = count_tokens(emitted_text)
    return {
        "schema_tokens": schema_tokens,
        "request_tokens": request_tokens,
        "result_tokens": result_tokens,
        "total_tokens": schema_tokens + request_tokens + result_tokens,
        "encoding": encoding_name(),
    }


def _resolve_schema_text(schema_json: Any) -> str:
    """Materialize *schema_json* into the exact string to count.

    *schema_json* may be a zero-arg callable (invoked lazily here, after the
    opt-in gate, so a possibly non-trivial schema build is skipped when the
    feature is disabled), a plain string (counted as-is), or any
    JSON-serializable object (serialized with the shared CPython-default
    boundary).  ``None`` counts as empty.
    """
    schema = schema_json() if callable(schema_json) else schema_json
    if schema is None:
        return ""
    if isinstance(schema, str):
        return schema
    return dumps_for_count(schema)


def maybe_attach_usage(
    result_obj: dict[str, Any],
    *,
    request: Any,
    schema_json: Any,
    arguments: Any,
    emitted_text: str,
) -> dict[str, Any]:
    """Attach ``_usage`` to *result_obj* in place when reporting is enabled.

    Resolves the layered opt-in (TUR-3) for *request* first.  When it resolves
    OFF -- the default, and unconditionally under a system-level ``deny`` -- the
    object is returned unchanged with **no** ``_usage`` key and no token
    counting performed, so the response is byte-identical to the pre-feature
    output.  Only success (``isError: false``) results should be passed here;
    error results are out of scope for v1 (TUR-1 sec 6).

    *emitted_text* must be the exact ``content[0].text`` string already placed
    on *result_obj* so ``result_tokens`` reflects the real bytes on the wire.

    **Never breaks the tool response (TUR-15).** This is an opt-in observability
    feature; a failure while producing it must not turn an otherwise-successful
    ``tools/call`` into a 500.  :func:`~frisian_mcp.usage.counter.count_tokens`
    already guarantees never-raise, but the surrounding attach path can still
    raise -- a lazy ``schema_json`` build callable may fail, and ``json.dumps``
    (via ``dumps_for_count``/``count_value``) can raise on a non-serializable
    argument or schema.  All such work is therefore done inside an exception
    boundary that computes the full ``_usage`` block **before** mutating
    *result_obj*; on any exception it logs a warning and returns *result_obj*
    byte-identical to its input (no half-applied ``_usage`` or content line).
    """
    if not resolve_usage_reporting(request):
        return result_obj

    # Compute everything BEFORE mutating result_obj so any failure leaves the
    # response byte-identical (atomic attach -- no partial _usage, no orphan
    # content line).  count_tokens never raises; the raisable work is the lazy
    # schema resolve and json.dumps of schema/arguments -- all inside this try.
    try:
        schema_text = _resolve_schema_text(schema_json)
        # Compute the block ONCE; it feeds both the caller-side sibling and the
        # optional model-visible content line -- no second tokenization (TUR-11 sec 4).
        block = build_usage_block(
            schema_text=schema_text, arguments=arguments, emitted_text=emitted_text
        )
        # Optional content-visible line (TUR-12): a SEPARATE, subordinate opt-in
        # only reached once the master gate resolved ON, so a system ``deny`` (or
        # any OFF) already suppressed BOTH surfaces.  Materialize the line item
        # here (still pre-mutation) so the append below cannot fail.
        content_list = None
        line_item = None
        if resolve_usage_in_content(request):
            content = result_obj.get("content")
            if isinstance(content, list):
                content_list = content
                line_item = {"type": "text", "text": "_usage: " + dumps_for_count(block)}
    except Exception:  # noqa: BLE001 - observability must never break the tool response
        logger.warning(
            "frisian-mcp token-usage reporting failed; returning the tool response "
            "without a _usage block",
            exc_info=True,
        )
        return result_obj

    # All computation succeeded -- attach atomically.  These mutations do not
    # raise (dict assignment; list.append of a pre-built item onto a list we
    # already confirmed is a list), so the response can never be left partial.
    result_obj["_usage"] = block
    if content_list is not None and line_item is not None:
        # Never mutate content[0], whose text is what result_tokens measured and
        # what the agent parses as the tool payload; the line is a NEW item.
        content_list.append(line_item)
    return result_obj
