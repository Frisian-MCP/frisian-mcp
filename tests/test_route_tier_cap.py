"""Golden tests for WI-2 — effective-tier cap on discovery (PR-12).

The route ceiling caps the caller's tier at ``min(token_tier, route.ceiling)``,
and the capped tier must be applied to **discovery**, not only to invocation.
An action that a caller cannot invoke must not be visible to that caller, and
an action that is visible must be invocable.  The two directions are separate
tests because they fail separately.

One source of truth per request: ``request._mcp_effective_tier``.  Discovery,
invocation, audit logging, and error messages all read that attribute.  Nothing
recomputes it.

The error path does not enforce the tier
---------------------------------------
Discovery and invocation both enforce the tier correctly.  The **error path**
does not, and it is the surface this project's WI-2 criterion actually names:
*"no action visible on discovery that fails at invoke, and vice versa."*  An
action named in an error message is visible.

``ToolRegistry.dispatch`` validates arguments against ``entry.input_schema`` —
the schema stored at *registration* time, built with ``max_tier=None``, i.e. the
**full** action enum.  ``tools/list`` builds a separate, per-request, filtered
schema, which is why discovery looks correct and why this hides::

    registry.py  jsonschema.validate(instance=arguments, schema=entry.input_schema)
                 except ValidationError as exc: raise ToolInputError(exc.message)

jsonschema's enum-failure message enumerates the entire enum
(``'creat' is not one of ['list', 'create']``), and ``views.py`` puts
``str(exc)`` on the wire as JSON-RPC ``error.data``.  One bad action name buys
a ``read``-tier caller every action name on the tool.

Note this is *not* the ``difflib`` suggester in ``backends/dispatcher.py``.
That suggester is effectively unreachable for a bad ``action`` — jsonschema
rejects the value against the enum before dispatch reaches ``action_map`` — and
it would have named at most one close match.  The enum message names all of them.

Second amplifier: ``lite``.  ``views._lite_enrich_error`` and
``_lite_enrich_error_content`` attach ``tool_registry.get_entry(name).input_schema``
— the global, unfiltered schema — to *any* validation error when the caller
passes ``lite: true``.  Caller-triggered, tier-blind, and route-blind once
routes exist.

Both are covered by :class:`TestErrorPathRespectsTier`.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.skip(
    reason="Awaits PR-7 (effective tier cap wiring). Assertions are written; "
    "unskip when request._mcp_effective_tier is stamped from the route ceiling."
)


# ---------------------------------------------------------------------------
# Route fixtures
# ---------------------------------------------------------------------------

ROUTES: dict[str, Any] = {
    "default": {"path": "mcp", "highest_tier": "read"},
    "elevated": {"path": "mcp/elevated", "highest_tier": "read_write"},
    "admin": {"path": "mcp/admin", "highest_tier": "admin"},
}

#: Actions that must never appear to a caller capped at `read`.
WRITE_ACTIONS = frozenset({"create", "update", "partial_update", "destroy"})

#: Synthesized (not directly registered) actions — must obey the same cap.
SYNTHESIZED_ACTIONS = frozenset({"bulk_create"})


# ---------------------------------------------------------------------------
# WI-2.1 — discovery honors the cap
# ---------------------------------------------------------------------------


class TestDiscoveryRespectsRouteCeiling:
    """tools/list output is filtered by min(token_tier, route.ceiling)."""

    def test_read_write_token_on_read_route_sees_only_reads(self) -> None:
        """A `read_write` token on a `read`-ceiling route: no write actions.

        Assert on the `action` enum in the tools/list input schema — that is
        where the cap actually lands, via _build_dispatcher_input_schema.
        """
        raise NotImplementedError

    def test_read_write_token_on_read_write_route_sees_writes(self) -> None:
        """The cap must not over-restrict: same token, higher ceiling."""
        raise NotImplementedError

    def test_admin_token_on_read_route_leaks_no_admin_surface(self) -> None:
        """An `admin` token capped to `read` sees exactly what `read` sees."""
        raise NotImplementedError

    def test_synthesized_actions_hidden_when_cap_forbids(self) -> None:
        """`bulk_create` is synthesized inside list_tools; it obeys the cap too."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-2.2 — discovery and invocation agree, in both directions
# ---------------------------------------------------------------------------


class TestDiscoveryInvocationParity:
    """No action visible on discovery that fails at invoke, and vice versa."""

    def test_every_discovered_action_is_invocable(self) -> None:
        """Enumerate the capped discovery surface; each action must invoke."""
        raise NotImplementedError

    def test_no_undiscovered_action_is_invocable(self) -> None:
        """Actions absent from capped discovery must be refused at invoke.

        This is the direction that catches a cap applied to discovery but not
        to dispatch — the mirror of the bug WI-2 is named for.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-2.3 — the error path (pre-existing defect, reproduced live)
# ---------------------------------------------------------------------------


class TestErrorPathRespectsTier:
    """Errors must not name actions the caller cannot see.

    Reproduced against `feat/route-permissions`: discovery and invocation both
    pass; the validation error leaks the full enum.  See the module docstring.
    """

    def test_validation_error_does_not_enumerate_the_full_action_enum(self) -> None:
        """`action="creat"` at `read` tier must not echo `create`.

        The stored `entry.input_schema` carries the unfiltered enum; jsonschema
        names every member of it in the failure message; views.py forwards that
        message verbatim as JSON-RPC `error.data`.
        """
        raise NotImplementedError

    def test_lite_error_enrichment_does_not_return_unfiltered_schema(self) -> None:
        """`lite: true` + a bad argument must not hand back the full inputSchema.

        `_lite_enrich_error` reads `tool_registry.get_entry(...).input_schema`
        from the global registry.  It must serve the tier-filtered, route-scoped
        schema, or nothing.
        """
        raise NotImplementedError

    def test_any_action_name_the_error_path_reveals_is_in_capped_discovery(self) -> None:
        """Whatever an error can name is a subset of what discovery shows."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-2.4 — cap resolution itself
# ---------------------------------------------------------------------------


class TestEffectiveTierResolution:
    """`_min_tier(token_tier, route.ceiling)` and its single stamped source."""

    def test_effective_tier_is_min_of_token_and_ceiling(self) -> None:
        """Cap is the stricter of the two, never the looser."""
        raise NotImplementedError

    def test_ceiling_none_leaves_token_tier_uncapped(self) -> None:
        """`highest_tier` omitted => ceiling None => effective == token tier.

        Pending the ruling on whether an operator-declared route may omit
        `highest_tier` at all.  If omission becomes FATAL, this case applies
        only to the implicit `__legacy_default__` view.
        """
        raise NotImplementedError

    def test_effective_tier_computed_once_and_read_everywhere(self) -> None:
        """Discovery and invocation both read `request._mcp_effective_tier`."""
        raise NotImplementedError
