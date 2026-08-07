"""
Response-negotiation protocol definitions (ADR-005).

Deliberately a **leaf module**: it imports nothing from the rest of the package.
The negotiation field set is needed by ``decorators`` (to merge into
``@mcp_heavy`` schemas), by ``backends.dispatcher`` (to disclose the protocol
and to police the one unambiguous protocol key), and by ``registry`` (to strip
the fields before top-level argument validation).  ``decorators`` already
imports ``registry``, so hosting these definitions in ``decorators`` and
reaching back from ``registry`` closes a ``decorators <-> registry`` import
cycle.  Keeping them here means every consumer depends on this module and this
module depends on none of them.
"""

from __future__ import annotations

from typing import Any

#: The five response-negotiation fields, as JSON Schema properties.  Single
#: source of truth: consumers derive names from this mapping rather than
#: restating them, so a field cannot be added to the protocol without every
#: placement/validation rule seeing it.
#:
#: Placement wording here is deliberately **shape-neutral**.  These descriptions
#: are merged into three different argument shapes — a flat ``@mcp_heavy`` tool
#: (the tool's own fields, no ``action`` and no ``params``), a class dispatcher
#: (``action`` + ``params``), and a group dispatcher (``resource`` + ``action``
#: + ``params``).  Naming the sibling keys would therefore be wrong for at least
#: one consumer.  ``params`` is the only key common to the shapes that have one,
#: and it is the only place the protocol fields must never go, so "top level,
#: not inside 'params'" is both sufficient and true everywhere.
_NEGOTIATION_PROPERTIES: dict[str, Any] = {
    "continuation_token": {
        "type": "string",
        "description": (
            "Token from a prior probe call, used to fetch the cached result."
            " Place at the TOP LEVEL of arguments — NOT inside 'params'."
            " Supply 'mode' alongside it to choose how much of the response to"
            " retrieve; omitting 'mode' returns the COMPLETE dataset, which for"
            " a large result may be very expensive."
        ),
    },
    "mode": {
        "type": "string",
        "enum": ["summary", "paginated", "filtered", "full"],
        "description": (
            "How much of the cached result to return on a continuation call."
            " Only meaningful when sent together with 'continuation_token', and"
            " must sit at the TOP LEVEL alongside it — not inside 'params'."
            " Omitting it defaults to 'full' (the complete dataset); use"
            " 'summary' or 'paginated' to bound the response size."
        ),
    },
    "page": {
        "type": "integer",
        "description": "Page number (1-based) for 'paginated' mode. Default: 1.",
        "default": 1,
    },
    "page_size": {
        "type": "integer",
        "description": "Items per page for 'paginated' mode. Defaults to FRISIAN_MCP_HEAVY_PAGE_SIZE.",  # noqa: E501  # pylint: disable=line-too-long
    },
    "filter_keys": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Top-level keys to retain in 'filtered' mode.",
    },
}

#: The protocol key that is *never* a legitimate action parameter, so the
#: dispatcher can recognise it unambiguously wherever it appears (T6).
#:
#: Only ``continuation_token`` qualifies.  The other four negotiation fields
#: collide with real host data: ``mode`` is a genuine model field on at least
#: one real host application, and ``page``/``page_size`` are DRF's own
#: ``PageNumberPagination`` query parameters.  Those four are protocol fields
#: only *in the context of a continuation call* — i.e. when accompanied by a
#: ``continuation_token`` — and must otherwise be passed through to the action
#: untouched.  See ``_make_dispatcher_invoke``.
NEGOTIATION_PROTOCOL_ONLY_KEY = "continuation_token"


def _merge_negotiation_schema(base: dict[str, Any]) -> dict[str, Any]:
    """
    Merge the response-negotiation protocol fields into *base* input schema.

    Only modifies schemas with ``"type": "object"``; returns *base* unchanged
    otherwise.  Removes ``"additionalProperties": false`` if present, since the
    merged negotiation fields would violate it.
    """
    if base.get("type") != "object":
        return base
    merged: dict[str, Any] = {**base}
    merged["properties"] = {**base.get("properties", {}), **_NEGOTIATION_PROPERTIES}
    if merged.get("additionalProperties") is False:
        del merged["additionalProperties"]
    return merged
