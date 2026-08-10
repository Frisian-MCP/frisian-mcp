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

#: The response modes ``_serve_heavy_mode`` implements, in advertised order.
#: Single source of truth: the schema ``enum``, the probe envelope's
#: ``available_modes`` and the invalid-mode error all derive from this, so a
#: mode cannot be advertised without being servable, or rejected by an error
#: that disagrees with the schema the caller was handed.
NEGOTIATION_MODES: tuple[str, ...] = ("summary", "paginated", "filtered", "full")

#: The mode served when a continuation call omits ``mode`` (ADR-005 item (b),
#: ruled B2).  Bounded by default: an omission or a typo must not select the
#: most expensive possible response.  ``full`` is still available and still
#: complete — it just has to be asked for.
DEFAULT_NEGOTIATION_MODE = "paginated"

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
            " 'mode' is OPTIONAL: supply it alongside to choose how much of the"
            " response to retrieve. Omitting 'mode' returns ONE PAGE at the"
            " server default page size when the result is a list, or the whole"
            " object when it is not (a single object is already bounded)."
            " Pass mode='full' explicitly for the complete dataset."
        ),
    },
    "mode": {
        "type": "string",
        "enum": list(NEGOTIATION_MODES),
        "description": (
            "How much of the cached result to return on a continuation call."
            " OPTIONAL. Only meaningful when sent together with"
            " 'continuation_token', and must sit at the TOP LEVEL alongside it"
            " — not inside 'params'. Omitting it defaults to 'paginated', which"
            " returns one bounded page of a list result, or the whole object"
            " for a non-list result (already bounded, so nothing is chunked)."
            " Pass 'full' explicitly for the complete dataset. A value outside"
            " the enum is rejected, not served."
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


def schema_discloses_continuation(schema: Any) -> bool:
    """
    Return ``True`` when *schema* publishes the continuation-call shape.

    This is the **single source of truth for negotiation eligibility**.  The
    mint path asks this question of the same ``input_schema`` object the caller
    was handed in ``tools/list``; nothing may mint a continuation token for a
    schema this returns ``False`` for.

    Deriving eligibility from the published schema — rather than recording it
    beside the schema as a registration flag — is deliberate.  A flag is a
    *claim about* the artifact, stored separately and updated by different code,
    so disclosure and behaviour can drift apart the moment one is edited without
    the other.  That drift is exactly how continuation tokens came to be minted
    for shapes no schema-validating caller could legally return.  A derivation
    cannot drift: there is only one artifact, read twice.

    Recognises both disclosure shapes, since both put the one unambiguous
    protocol key in ``properties`` — the flat merge used by ``@mcp_heavy`` and
    the dispatchers (:func:`_merge_negotiation_schema`) and the conditional
    branch used by ordinary read tools (:func:`merge_continuation_branch`).
    """
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and NEGOTIATION_PROTOCOL_ONLY_KEY in properties


def merge_continuation_branch(base: dict[str, Any]) -> dict[str, Any]:
    """
    Disclose the continuation call on *base* without altering its first call.

    Used for tools that are eligible for the size backstop but are not
    ``@mcp_heavy`` — hand-registered ``@mcp_tool`` tools and, more importantly,
    auto-discovered ViewSet actions, which have no decorator to annotate and are
    the population the backstop exists to protect.

    Differs from :func:`_merge_negotiation_schema`, which flattens all five
    fields into ``properties``.  That is right for ``@mcp_heavy`` and the
    dispatchers, whose argument shapes are ours.  It is wrong here: ``mode``,
    ``page``, ``page_size`` and ``filter_keys`` collide with real host data (see
    :data:`NEGOTIATION_PROTOCOL_ONLY_KEY`), so declaring them unconditionally on
    an arbitrary host tool would corrupt a signature that already means
    something else.

    Instead only ``continuation_token`` — the one key that is never a legitimate
    application parameter — joins ``properties``.  The other four are declared
    in a conditional branch that applies **only** when a ``continuation_token``
    is present, so an ordinary first call validates exactly as it did before.

    The branch is appended to ``allOf`` rather than written to a top-level
    ``if``/``then``, so it composes with any conditional logic the host's own
    schema already uses instead of silently replacing it.

    Idempotent: a schema that already discloses is returned unchanged, so this
    can be applied on a registration path without checking whether some other
    path got there first.
    """
    if base.get("type") != "object":
        return base
    if schema_discloses_continuation(base):
        return base

    merged: dict[str, Any] = {**base}
    merged["properties"] = {
        **base.get("properties", {}),
        NEGOTIATION_PROTOCOL_ONLY_KEY: _NEGOTIATION_PROPERTIES[NEGOTIATION_PROTOCOL_ONLY_KEY],
    }
    # A closed schema cannot express a continuation call at all: the four
    # branch fields would be rejected as additional properties, leaving a token
    # that is minted and unredeemable — the defect this function exists to
    # prevent.  The alternative, leaving it closed and refusing to negotiate,
    # hands back the unbounded payload the backstop was added to stop.  Opening
    # it is the same trade `_merge_negotiation_schema` already makes.
    if merged.get("additionalProperties") is False:
        del merged["additionalProperties"]
    merged["allOf"] = [
        *base.get("allOf", []),
        {
            "if": {"required": [NEGOTIATION_PROTOCOL_ONLY_KEY]},
            "then": {
                "properties": {
                    name: prop
                    for name, prop in _NEGOTIATION_PROPERTIES.items()
                    if name != NEGOTIATION_PROTOCOL_ONLY_KEY
                }
            },
        },
    ]
    return merged


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
