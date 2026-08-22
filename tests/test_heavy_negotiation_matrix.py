"""
Heavy-response negotiation asserted across every consumer shape (T4, narrowed by CR-2).

Why this file exists as a *matrix* rather than more tests in the existing files:
the group-dispatcher disclosure gap shipped green because every negotiation
assertion in the suite named exactly one construction path.  ``test_mcp_heavy``
covered the flat ``@mcp_heavy`` path, ``test_dispatcher`` covered the class
dispatcher, and neither mentioned the other — so a third path could be added
with no disclosure at all and nothing turned red.

The defence is structural, not additive: **shape is a parameter here, never a
copy.**  A new consumer is one entry in :data:`_SHAPES` away from being held to
a contract, and a consumer that cannot satisfy one fails at collection rather
than being quietly omitted.

**There are two contracts, and every shape owes exactly one of them.**  That is
the part CR-2 changed, and the reason the matrix is no longer a list of five
things that all do the same thing:

===================  =========================  ==============================
Shape                Contract                   Top-level arguments
===================  =========================  ==============================
flat ``@mcp_heavy``  DISCLOSE (flat merge)      the tool's own fields
class dispatcher     DISCLOSE (flat merge)      ``action`` + ``params``
group dispatcher     DISCLOSE (flat merge)      ``resource``/``action``/``params``
plain ``@mcp_tool``  **PUBLISH UNCHANGED**      the tool's own fields
auto-discovered      **PUBLISH UNCHANGED**      the action's own fields
===================  =========================  ==============================

The three disclosing shapes are merged by ``_merge_negotiation_schema`` and are
driven by the :func:`shape` fixture through the positive assertions.  The two
non-disclosing shapes are driven by :func:`non_disclosing_shape` and asserted as
**negative** cases in :class:`TestOrdinaryShapesDoNotDisclose`.
:data:`_DISCLOSING_SHAPE_IDS` and :data:`_NON_DISCLOSING_SHAPE_IDS` partition
:data:`_SHAPE_IDS` exactly, and
:meth:`TestDisclosure.test_every_shape_is_classified` fails if a shape is added
without being ruled into one of them.

**Why the split, and what was tried first.**  Universal disclosure was not an
oversight — it shipped, deliberately, and was then narrowed.  H2 closed a real
defect: the size backstop minted continuation tokens for schemas that never
published the continuation call, so a schema-validating client was handed a
token it had no legal slot to return.  H2's remedy was to make **every** schema
disclose, including ordinary ``@mcp_tool`` registrations and auto-discovered
ViewSet actions.  It worked, and it cost roughly **380 tokens on every ordinary
published schema** (measured, CR-1) — paid on every ``tools/list``, by hosts
that never asked for negotiation.

CR-2 closed the same defect from the side the code already enforced it on.
``schema_discloses_continuation()`` gates the mint against the published schema,
so a shape that stays silent never mints; disclosure was the expensive half of a
belt-and-braces pair and the redundant one.  The consequence for a
non-disclosing shape — an over-threshold response "is returned whole" — is the
pre-existing H18 path, previously reached only by closed schemas.

Both halves of the negative contract are asserted, because either alone lets the
regression back: the ordinary shapes **publish nothing**, and the backstop
**mints nothing** for them.  A schema-only assertion would pass a build that
quietly kept minting against an undisclosed schema, which is the original defect.

**One disclosure style now ships.**  The flat merge declares all five negotiation
fields in ``properties``.  A second style existed — ``merge_continuation_branch``,
which declared only ``continuation_token`` there and the other four behind
``allOf`` → ``if``/``then``, because ``mode``, ``page``, ``page_size`` and
``filter_keys`` collide with real host field names and must not be injected into
every tool's signature.  That helper now has **zero call sites in ``src/``**; it
is retained for the H18 closed-schema guard and for regression coverage here,
not applied by any registration path.  The disclosure assertions still test
**reachability** rather than placement (see
:func:`_reachable_negotiation_fields`), which costs nothing and keeps the
contract honest if a second style ever returns.

**The guard is the point of the meta-tests.**  ``_CONSUMER_TO_SHAPES`` answers
"is every disclosure claimed by a shape?" — a question a re-introduction can
*pass*, by landing in a module already mapped for the other helper.
``decorators.py`` is in exactly that position: it legitimately hosts the flat
merge for ``@mcp_heavy`` while ``merge_continuation_branch`` must stay gone.  So
call sites are counted **per helper**, by an :mod:`ast` walk over real ``Call``
nodes rather than a text scan — prose naming a helper is not a call site, and an
aliased import is — and :data:`_FORBIDDEN_HELPERS` pins the forbidden one at
zero across the whole package.  See
:meth:`TestDisclosure.test_forbidden_helpers_have_no_call_sites`.

Cited by quoted phrase rather than line number throughout: the numbers this
docstring previously carried had all moved by the time anyone read them.

Host-agnostic throughout: synthetic ``item``/``container``/``catalog`` fixtures,
no host-application schema as identifiers or data.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

import frisian_mcp
from frisian_mcp.backends.group_dispatcher import build_group_input_schema, make_group_invoke
from frisian_mcp.decorators import mcp_action, mcp_dispatcher, mcp_heavy, mcp_tool
from frisian_mcp.negotiation import (
    _NEGOTIATION_PROPERTIES,
    NEGOTIATION_PROTOCOL_ONLY_KEY,
    merge_continuation_branch,
    schema_discloses_continuation,
)
from frisian_mcp.registry import ToolInputError, ToolRegistry
from frisian_mcp.views import McpView, _heavy_owner_key

_view = McpView.as_view()

#: The four modes ``_serve_heavy_mode`` actually implements.  Asserted against
#: the schema enum and the probe envelope so a mode cannot be advertised
#: without being served, or served without being advertised.
_MODES = ("summary", "paginated", "filtered", "full")

#: A payload large enough to trip a lowered ``AUTO_NEGOTIATE_THRESHOLD`` and
#: structured so every mode has something distinguishable to do with it:
#: ``summary`` truncates, ``paginated`` slices, ``filtered`` selects keys.
_PAYLOAD: list[dict[str, Any]] = [
    {"name": f"item-{i}", "label": "x" * 120, "slot": i} for i in range(40)
]

_THRESHOLD = 500


# ---------------------------------------------------------------------------
# Shape descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    """
    One consumer of the negotiation protocol, described uniformly.

    ``build`` registers the shape into a caller-supplied registry and returns
    the tool name to invoke.  Everything else the matrix needs — how to phrase a
    probe call, how to phrase a redemption, whether the shape even has a
    ``params`` key to misplace a token into — is data, so a new shape is a new
    instance rather than a new copy of the tests.
    """

    id: str
    build: Callable[[ToolRegistry], str]
    probe_args: dict[str, Any] = field(default_factory=dict)
    #: Arguments that must accompany every call, including redemption — a group
    #: call still has to say which resource it is continuing.
    routing_args: dict[str, Any] = field(default_factory=dict)
    #: ``False`` for the flat shapes, which have no nested ``params`` object and
    #: therefore no misplacement to guard against.
    has_params: bool = True

    def redeem_args(self, token: str, mode: str | None = None, **extra: Any) -> dict[str, Any]:
        """Return call-2 arguments: routing + token + optional mode."""
        args: dict[str, Any] = {**self.routing_args, "continuation_token": token}
        if mode is not None:
            args["mode"] = mode
        args.update(extra)
        return args


def _reachable_negotiation_fields(schema: dict[str, Any]) -> set[str]:
    """
    Return the negotiation fields a caller may legally send against *schema*.

    Resolves **both** disclosure styles, because both are correct:

    * the flat merge (``_merge_negotiation_schema``) declares all five in
      ``properties``;
    * the conditional branch (``merge_continuation_branch``) declares only
      ``continuation_token`` there and the other four inside an
      ``allOf`` → ``if``/``then``, so an ordinary first call keeps the tool's own
      signature.

    Placement differs on purpose; reachability is the contract every consumer
    owes the caller, so the matrix asserts that instead.
    """
    fields = set(schema.get("properties", {})) & set(_NEGOTIATION_PROPERTIES)
    for branch in schema.get("allOf", []):
        then = branch.get("then", {}) if isinstance(branch, dict) else {}
        fields |= set(then.get("properties", {})) & set(_NEGOTIATION_PROPERTIES)
    return fields


def _negotiation_property(schema: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return the JSON Schema for negotiation field *name*, from either disclosure style."""
    prop = schema.get("properties", {}).get(name)
    if prop is not None:
        return prop  # type: ignore[no-any-return]
    for branch in schema.get("allOf", []):
        then = branch.get("then", {}) if isinstance(branch, dict) else {}
        prop = then.get("properties", {}).get(name)
        if prop is not None:
            return prop  # type: ignore[no-any-return]
    return None


def _payload_fn(payload: Any) -> Callable[[dict[str, Any], Any], Any]:
    """Return a tool callable that echoes *payload* and counts its invocations."""

    def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
        _fn.calls += 1  # type: ignore[attr-defined]
        return payload

    _fn.calls = 0  # type: ignore[attr-defined]
    return _fn


def _build_flat_heavy(reg: ToolRegistry) -> str:
    """Register a flat ``@mcp_heavy`` tool — no ``action``, no ``params``."""
    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_heavy(
            name="item_list",
            description="Flat heavy tool.",
            input_schema={"type": "object", "properties": {}},
        )
        def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
            return _PAYLOAD

        _ = _fn
    return "item_list"


def _build_plain_tool(reg: ToolRegistry) -> str:
    """
    Register a plain ``@mcp_tool`` — the shape H2 added and this matrix missed.

    Discloses through the conditional branch rather than the flat merge, so it
    is the reason the disclosure assertions test reachability.
    """
    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_tool(
            name="item_list",
            description="Plain tool.",
            input_schema={"type": "object", "properties": {}},
        )
        def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
            return _PAYLOAD

        _ = _fn
    return "item_list"


def _build_auto_discovered(reg: ToolRegistry) -> str:
    """
    Register an auto-discovered ViewSet action — the fifth consumer.

    This population has no decorator to annotate and is precisely what the size
    backstop exists to protect, so it is the shape whose disclosure matters
    most.  Built from real discovery output rather than a hand-written schema,
    so the test exercises ``discovery``'s own merge call and would notice it
    being removed.
    """
    from frisian_mcp.backends.discovery import (  # pylint: disable=import-outside-toplevel
        DRFSyncDiscovery,
    )

    # Discovery walks the URL resolver, so it needs the test URLconf.  Scoped to
    # the discovery call rather than the whole module so no other shape's
    # environment changes.
    with override_settings(ROOT_URLCONF="tests.urls"):
        discovered = {t.name: t for t in DRFSyncDiscovery().discover_tools()}

    definition = discovered["users_list"]
    reg.register(
        # Registered under the matrix's usual name so the shared call helpers,
        # owner-key assertions and probe args need no special case.  The schema
        # is the real discovered one — that is the part under test.
        name="item_list",
        fn=_payload_fn(_PAYLOAD),
        description=definition.description,
        input_schema=definition.input_schema,
        permission_tier="read",
    )
    return "item_list"


def _build_class_dispatcher(reg: ToolRegistry) -> str:
    """Register a class dispatcher — ``action`` + ``params``."""
    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_dispatcher("catalog", description="Class dispatcher.")
        class _Catalog:
            """Synthetic dispatcher."""

            @mcp_action("list", description="List items.", params={})
            def list(self, request: Any, params: dict[str, Any]) -> Any:
                """Return the payload."""
                # pylint: disable=unused-argument
                return _PAYLOAD

        _ = _Catalog
    return "catalog"


def _build_group_dispatcher(reg: ToolRegistry) -> str:
    """Register a group dispatcher over flat members — ``resource`` + ``action`` + ``params``."""
    members = ["item_list", "container_list"]
    for member in members:
        reg.register(
            name=member,
            fn=_payload_fn(_PAYLOAD),
            description=f"flat {member}",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read",
        )
    prefixes = frozenset({"item", "container"})
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", frozenset(members), reg, prefixes),
        description="Group dispatcher.",
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=frozenset(members),
    )
    for member in members:
        reg.set_hidden(member, True)
    return "catalog"


_SHAPES: dict[str, _Shape] = {
    "flat_heavy": _Shape(
        id="flat_heavy",
        build=_build_flat_heavy,
        probe_args={},
        routing_args={},
        has_params=False,
    ),
    "plain_tool": _Shape(
        id="plain_tool",
        build=_build_plain_tool,
        probe_args={},
        routing_args={},
        has_params=False,
    ),
    "auto_discovered": _Shape(
        id="auto_discovered",
        build=_build_auto_discovered,
        probe_args={},
        routing_args={},
        has_params=False,
    ),
    "class_dispatcher": _Shape(
        id="class_dispatcher",
        build=_build_class_dispatcher,
        probe_args={"action": "list", "params": {}},
        routing_args={"action": "list"},
    ),
    "group_dispatcher": _Shape(
        id="group_dispatcher",
        build=_build_group_dispatcher,
        probe_args={"resource": "item", "action": "list", "params": {}},
        routing_args={"resource": "item", "action": "list"},
    ),
}

_SHAPE_IDS = tuple(_SHAPES)

#: The shapes that MUST publish the negotiation protocol.  These are the tools
#: whose argument shape is *ours* -- ``@mcp_heavy`` opts in explicitly, and both
#: dispatchers own their ``action``/``params`` envelope outright -- so declaring
#: five extra fields on them cannot collide with a host signature.  They mint,
#: so they must disclose.
_DISCLOSING_SHAPE_IDS: tuple[str, ...] = (
    "flat_heavy",
    "class_dispatcher",
    "group_dispatcher",
)

#: The shapes that MUST NOT publish it (CR-2).  An ordinary ``@mcp_tool`` and an
#: auto-discovered ViewSet action carry the **host's** schema, and H2 made both
#: disclose universally -- ~380 tokens added to every published schema, measured
#: in CR-1, paid by hosts that never asked for negotiation.
#:
#: Removing disclosure does not re-open what H2 closed.  The invariant is "never
#: mint a token the caller cannot legally return", and it is enforced on the
#: MINT side: ``schema_discloses_continuation()`` gates the backstop against
#: this same published schema, so a shape that stays silent never mints.  The
#: consequence -- an over-threshold response "is returned whole" -- is the
#: pre-existing H18 path, previously reached only by closed schemas.
#:
#: Both halves are asserted below, because either alone permits the regression
#: back: :class:`TestOrdinaryShapesDoNotDisclose` pins the schema, and the
#: behavioural tests pin that nothing is minted and nothing is cached.
_NON_DISCLOSING_SHAPE_IDS: tuple[str, ...] = (
    "plain_tool",
    "auto_discovered",
)

#: Where each **disclosing** consumer in ``src/`` lives, mapped to the shape
#: that holds it to the contract.  Keys are module paths relative to the package
#: root; :func:`_discover_disclosing_call_sites` recomputes the left-hand side
#: from the source so this mapping cannot silently fall behind.
_CONSUMER_TO_SHAPES: dict[str, tuple[str, ...]] = {
    # ``decorators.py`` now hosts exactly ONE consumer: ``@mcp_heavy``'s flat
    # merge.  It hosted two until CR-2 removed the ``@mcp_tool`` conditional
    # branch.  The multiplicity still lives in the mapping rather than in a
    # separate constant, because module granularity alone cannot see one of two
    # consumers in a module disappear -- the module still has a call site, so a
    # key-set comparison stays green.  The count is the guard.
    "decorators.py": ("flat_heavy",),
    "backends/dispatcher.py": ("class_dispatcher",),
    "backends/group_dispatcher.py": ("group_dispatcher",),
}

#: Helpers that must have **zero** call sites anywhere in ``src/``, mapped to
#: why.  This is the other half of the guard, and the half that did not exist
#: before CR-2: ``_CONSUMER_TO_SHAPES`` can only answer "is every disclosure
#: claimed?", never "did disclosure come back somewhere we ruled it out?".
#:
#: A key-set comparison cannot answer the second question either, because a
#: re-introduction on an ordinary path lands in a module that already appears
#: in the mapping for a DIFFERENT helper -- exactly the case ``decorators.py``
#: is in today, hosting the flat merge while the conditional branch must stay
#: gone.  So the count is tracked PER HELPER, not per module.
_FORBIDDEN_HELPERS: dict[str, str] = {
    "merge_continuation_branch": (
        "ordinary @mcp_tool and auto-discovered actions must publish the host's"
        " schema unchanged (CR-2); negotiation is opt-in via @mcp_heavy or a"
        " dispatcher, and the mint gate -- not disclosure -- is what keeps the"
        " backstop from issuing an unreturnable token"
    ),
}

#: The two helpers that publish the negotiation protocol.  A consumer is any
#: call site of either, outside their defining module.
_MERGE_HELPERS = ("_merge_negotiation_schema", "merge_continuation_branch")


def _call_sites_by_helper(source: str) -> dict[str, int]:
    """
    Return ``{helper: number of real call sites}`` for one module's *source*.

    Parsed with :mod:`ast` rather than scanned line-by-line.  The previous
    version counted the literal text ``helper(`` on any line not starting with
    ``from``/``import``, which cannot tell a call from prose -- and CR-2's own
    removal comments now name ``merge_continuation_branch`` in running text at
    both former call sites.  A guard that reds the suite because someone
    documented the removal is a guard people switch off.

    Import aliases are resolved, so renaming on import cannot hide a call
    either: ``from ... import merge_continuation_branch as _mcb`` followed by
    ``_mcb(schema)`` is counted against ``merge_continuation_branch``.
    """
    tree = ast.parse(source)

    # local name -> canonical helper name
    aliases: dict[str, str] = {h: h for h in _MERGE_HELPERS}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in _MERGE_HELPERS:
                    aliases[alias.asname or alias.name] = alias.name

    counts = dict.fromkeys(_MERGE_HELPERS, 0)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            local = func.id
        elif isinstance(func, ast.Attribute):
            local = func.attr
        else:
            continue
        canonical = aliases.get(local)
        if canonical is not None:
            counts[canonical] += 1
    return counts


def _discover_disclosing_call_sites() -> dict[str, dict[str, int]]:
    """
    Return ``{module path relative to src/frisian_mcp: {helper: call count}}``.

    Scans the shipped source rather than trusting a hand-maintained list, so
    that "a consumer was added" is answered by the package itself.  The
    defining module is excluded: it declares the helpers, it does not consume
    them.

    Counted **per helper**.  The previous version summed both helpers into a
    single per-module integer, which cannot distinguish "``decorators.py`` has
    one call site" (correct: ``@mcp_heavy``'s flat merge) from "``decorators.py``
    has one call site" (regression: the conditional branch came back and the
    flat merge moved).  Only modules with at least one call site appear.
    """
    package_root = Path(frisian_mcp.__file__).parent
    found: dict[str, dict[str, int]] = {}
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "negotiation.py":
            continue
        counts = _call_sites_by_helper(path.read_text(encoding="utf-8"))
        if any(counts.values()):
            found[path.relative_to(package_root).as_posix()] = counts
    return found


@pytest.fixture(params=_DISCLOSING_SHAPE_IDS)
def shape(request: Any) -> _Shape:
    """
    Parameterise over every consumer that MUST disclose.

    Deliberately not every shape in :data:`_SHAPES`.  Until CR-2 this fixture
    covered all five, which made the token regression a *required* contract:
    CI demanded that ordinary tools keep publishing the heavy protocol forever.
    The two ordinary shapes are now driven by :func:`non_disclosing_shape` and
    held to the opposite contract, so both directions are asserted rather than
    one being dropped.
    """
    return _SHAPES[request.param]


@pytest.fixture(params=_NON_DISCLOSING_SHAPE_IDS)
def non_disclosing_shape(request: Any) -> _Shape:
    """Parameterise over every consumer that MUST NOT disclose (CR-2)."""
    return _SHAPES[request.param]


# ---------------------------------------------------------------------------
# Call helpers
# ---------------------------------------------------------------------------


def _jsonrpc(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _request(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    req = rf.post(
        "/mcp/", data=json.dumps(_jsonrpc(name, arguments)), content_type="application/json"
    )
    req.user = AnonymousUser()
    return req


def _call(rf: RequestFactory, name: str, arguments: dict[str, Any]) -> Any:
    return _view(_request(rf, name, arguments))


def _result(response: Any) -> Any:
    data = json.loads(response.content)
    return json.loads(data["result"]["content"][0]["text"])


def _minted_entry(mock_cache: MagicMock) -> dict[str, Any]:
    """
    Return the SEC-3 cache entry the view actually stored.

    Read off ``django_cache.set`` rather than reconstructed, so the assertions
    describe what was minted rather than what the test expected to be minted.
    """
    assert mock_cache.set.called, "no continuation token was minted"
    return mock_cache.set.call_args[0][1]  # type: ignore[no-any-return]


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()


@pytest.fixture()
def low_threshold(settings: Any) -> None:
    """
    Lower ``AUTO_NEGOTIATE_THRESHOLD`` so a modest payload negotiates.

    Used instead of a class-level ``override_settings``, which Django refuses on
    anything that is not a ``SimpleTestCase`` subclass.
    """
    settings.FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = _THRESHOLD


# ---------------------------------------------------------------------------
# Schema disclosure — one contract, every shape
# ---------------------------------------------------------------------------


class TestDisclosure:
    """
    Every shape that can mint a token must publish where the token goes.

    ADR-005's "Decision" defines redemption as re-invoking the same tool with a
    ``continuation_token`` and a ``mode``.  A shape that advertises
    ``available_modes`` in the probe envelope but omits the fields from its
    published schema mints tokens no conformant client can send back.
    """

    def test_all_five_fields_published(self, shape: _Shape) -> None:
        """
        The five negotiation fields are REACHABLE on the shape's published schema.

        Reachability, not placement, is the contract.  Two disclosure styles are
        both correct: the flat merge puts all five in ``properties``, while the
        conditional branch publishes only ``continuation_token`` there and the
        other four behind ``if``/``then`` — deliberately, because ``mode``,
        ``page``, ``page_size`` and ``filter_keys`` collide with real host field
        names and must not be injected into every tool's signature.

        Asserting ``field in properties`` would therefore fail a shape that is
        behaving exactly as ruled.  Asserting reachability holds both styles to
        the same contract without privileging either.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        reachable = _reachable_negotiation_fields(reg.get_entry(name).input_schema)
        missing = set(_NEGOTIATION_PROPERTIES) - reachable
        assert not missing, f"{sorted(missing)} unreachable on the {shape.id} path"

    def test_mode_enum_matches_what_is_served(self, shape: _Shape) -> None:
        """
        The advertised enum equals the modes ``_serve_heavy_mode`` implements.

        Asserted as set equality in both directions: a mode in the enum that is
        not served strands the caller, and a mode served but not advertised is
        undiscoverable.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        mode = _negotiation_property(reg.get_entry(name).input_schema, "mode")
        assert mode is not None, f"'mode' unreachable on the {shape.id} path"
        assert set(mode["enum"]) == set(_MODES)

    def test_placement_text_is_shape_neutral(self, shape: _Shape) -> None:
        """
        The shared placement text must not name arguments a shape lacks (A5).

        The flat shape has no ``action`` and no ``params``, so naming them as
        siblings is wrong for it.  ``params`` is the only key common to the
        shapes that have one and is the only place the token must never go, so
        the neutral phrasing is the only phrasing true everywhere.

        CR-14 moved the *long* form of this guidance into the probe envelope and
        left only the placement line in the schema, so A5 now binds both texts.
        The envelope half is asserted in
        :meth:`test_envelope_carries_the_guidance_moved_out_of_the_schema`.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        props = reg.get_entry(name).input_schema["properties"]
        text = props["continuation_token"]["description"]
        assert "TOP LEVEL" in text
        assert "'action'" not in text
        assert "'resource'" not in text

    def test_schema_declares_but_does_not_instruct(self, shape: _Shape) -> None:
        """
        CR-14: the schema DECLARES the protocol; the probe envelope INSTRUCTS.

        The negotiation descriptions used to restate, in every ``tools/list`` on
        every call, what an agent needs only while holding a token — which it
        receives *in the envelope*.  That duplicate cost ~254 cl100k tokens per
        call on a group-dispatcher-mounted host.

        This guards the direction the cost came back from.  The envelope half is
        already covered, and covered more strongly (through a live request), by
        :class:`TestProbeEnvelopeTeachingText` — so what was missing was not
        another envelope assertion but a guard against the prose being re-added
        here by someone who assumes it was dropped by accident.

        Deliberately a *shape* assertion, not a token ceiling: it names the
        thing that must not come back rather than a number to be re-tuned.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        props = reg.get_entry(name).input_schema["properties"]

        # The four non-token fields carry no prose at all.  Their meaning is
        # machine-readable (`enum`, `default`, `type`) or lives in the envelope.
        for prop_name in ("mode", "page", "page_size", "filter_keys"):
            assert "description" not in props[prop_name], (
                f"'{prop_name}' has regained a description. The negotiation guidance"
                " belongs in the probe envelope's 'usage' field, which is paid"
                " on the calls that mint rather than on every call forever."
                " See the comment above _NEGOTIATION_PROPERTIES."
            )

        # `continuation_token` keeps ONE short line, as insurance against the
        # T6 failure: an agent that puts the token inside 'params'.
        token_desc = props["continuation_token"]["description"]
        assert "not inside 'params'" in token_desc
        assert len(token_desc) < 120, (
            f"'continuation_token' description has grown to {len(token_desc)}"
            " chars. It is a placement pointer, not the instructions."
        )

    def test_every_shape_is_classified(self) -> None:
        """
        META: every shape in ``_SHAPES`` is ruled either disclosing or not.

        The two lists partition ``_SHAPE_IDS`` exactly.  Adding a shape without
        deciding which contract it owes turns this red, rather than letting it
        drift into whichever fixture happens to pick it up.
        """
        disclosing = set(_DISCLOSING_SHAPE_IDS)
        non_disclosing = set(_NON_DISCLOSING_SHAPE_IDS)

        overlap = sorted(disclosing & non_disclosing)
        assert not overlap, f"shape(s) {overlap} claim both contracts at once"

        unclassified = sorted(set(_SHAPE_IDS) - disclosing - non_disclosing)
        assert not unclassified, (
            f"shape(s) {unclassified} are in _SHAPES but neither disclose nor"
            " refuse to; add each to _DISCLOSING_SHAPE_IDS or"
            " _NON_DISCLOSING_SHAPE_IDS"
        )

        unknown = sorted((disclosing | non_disclosing) - set(_SHAPE_IDS))
        assert not unknown, f"classified shape ids {unknown} are not in _SHAPES"

    def test_disclosure_survives_a_new_shape(self) -> None:
        """
        META: every consumer in ``src/`` is covered, counted FROM ``src/``.

        An earlier version asserted ``len(_SHAPE_IDS) == 3`` and **did not fire
        when H2 added a consumer**, because it counted entries in ``_SHAPES``
        while the change happened in ``decorators.py``.  Nothing drifted: the
        thing that changed was not the thing being counted.  A guard that can
        only detect the edit you remembered to make is not a guard, and bumping
        the literal to ``4`` would have reproduced the blind spot with a bigger
        number.

        So the source of truth is the source: every call site of either merge
        helper is discovered by scanning ``src/``, and each must be claimed by a
        shape here.  Adding a consumer anywhere now turns this red until it is
        either parameterised or explicitly recorded as covered.

        CR-2 re-aimed this rather than relaxing it.  ``decorators.py`` legitimately
        dropped from two consumers to one and ``backends/discovery.py`` to zero,
        so the mapping shrank -- but the counting moved from a per-module total
        to a per-helper breakdown, and :meth:`test_forbidden_helpers_have_no_call_sites`
        was added.  Both are tightenings: the guard can now see a re-introduction
        that lands in a module which still legitimately hosts the *other* helper,
        which the per-module total could not.
        """
        found = _discover_disclosing_call_sites()
        unclaimed = sorted(set(found) - set(_CONSUMER_TO_SHAPES))
        assert not unclaimed, (
            f"negotiation consumer(s) {unclaimed} exist in src/ but no shape covers"
            " them. If this is a genuinely NEW disclosing construction path, add a"
            " shape to _SHAPES and map it in _CONSUMER_TO_SHAPES. If it is"
            " merge_continuation_branch reappearing on an ordinary tool or"
            " auto-discovered action, that is the CR-2 regression -- do NOT map it"
            " back, remove the call site"
        )

        stale = sorted(set(_CONSUMER_TO_SHAPES) - set(found))
        assert not stale, (
            f"_CONSUMER_TO_SHAPES claims {stale}, which no longer merges the"
            " negotiation fields; drop the mapping or restore the disclosure"
        )

        # The count is the part that actually guards.  The set comparisons above
        # all stayed GREEN when one of decorators.py's two consumers was removed:
        # the module still had a call site, so no key changed.  Counted per
        # helper (not as a per-module total) so that a swap -- the conditional
        # branch returning while the flat merge moves elsewhere -- cannot net out
        # to the same number and pass.
        miscounted = {
            module: (sum(found[module].values()), len(shapes))
            for module, shapes in _CONSUMER_TO_SHAPES.items()
            if sum(found[module].values()) != len(shapes)
        }
        assert not miscounted, (
            f"call-site count disagrees with claimed shapes {miscounted} "
            "(module: found_in_src, shapes_claimed); a consumer was added or "
            "removed inside a module that still has others, which no key-set "
            "comparison can see"
        )

        uncovered = sorted(
            {shape for shapes in _CONSUMER_TO_SHAPES.values() for shape in shapes} - set(_SHAPE_IDS)
        )
        assert not uncovered, f"mapped shape ids {uncovered} are not in _SHAPES"

    def test_forbidden_helpers_have_no_call_sites(self) -> None:
        """
        META: a helper ruled out of the ordinary path has ZERO call sites in ``src/``.

        This is the assertion that actually stands between us and silently
        re-landing the token regression in six months, and it is the one the
        pre-CR-2 guard could not make. ``_CONSUMER_TO_SHAPES`` answers "is every
        disclosure claimed by a shape?" -- a question a re-introduction can pass,
        by landing in a module already mapped for the *other* helper.
        ``decorators.py`` is in exactly that position: it legitimately hosts
        ``_merge_negotiation_schema`` for ``@mcp_heavy`` while
        ``merge_continuation_branch`` must stay gone.

        Deliberately counted across the whole package, not just the two modules
        CR-2 edited: the point is that disclosure must not come back ANYWHERE on
        an ordinary path, including a registration path nobody has written yet.
        """
        found = _discover_disclosing_call_sites()
        offenders = {
            f"{module}:{helper}": counts[helper]
            for module, counts in found.items()
            for helper in _FORBIDDEN_HELPERS
            if counts.get(helper)
        }
        assert not offenders, (
            f"{sorted(offenders)} re-introduces negotiation disclosure on a path"
            " ruled non-disclosing.\n"
            + "\n".join(f"  {helper}: {why}" for helper, why in _FORBIDDEN_HELPERS.items())
            + "\nIf this is deliberate it is a contract change and needs a ruling,"
            " not a green suite."
        )


# ---------------------------------------------------------------------------
# Probe -> redeem, every shape x every mode
# ---------------------------------------------------------------------------


class TestProbeRedeemRoundTrip:
    """
    A token minted by a shape must be redeemable through that same shape.

    Minting here runs through the ``AUTO_NEGOTIATE_THRESHOLD`` backstop, which
    is the only mint path currently reachable on the two dispatcher shapes —
    see :class:`TestMcpHeavyOnDispatcherShapes`.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload."""

    def test_probe_returns_an_envelope(self, rf: RequestFactory, shape: _Shape) -> None:
        """Call 1 returns a probe envelope advertising all four modes."""
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            response = _call(rf, name, shape.probe_args)

        result = _result(response)
        assert "continuation_token" in result
        assert result["available_modes"] == list(_MODES)

    @pytest.mark.parametrize("mode", _MODES)
    def test_every_mode_redeems(self, rf: RequestFactory, shape: _Shape, mode: str) -> None:
        """
        All four advertised modes are reachable on all shapes.

        The cache entry handed to call 2 is the one call 1 actually minted, so
        this exercises the real SEC-3 binding rather than a hand-built stand-in.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, shape.probe_args))
            entry = _minted_entry(cache)

            cache.get.return_value = entry
            extra: dict[str, Any] = {"filter_keys": ["name"]} if mode == "filtered" else {}
            served = _result(
                _call(rf, name, shape.redeem_args(probe["continuation_token"], mode, **extra))
            )

        assert "error" not in served if isinstance(served, dict) else True
        if mode == "full":
            assert served == _PAYLOAD
        elif mode == "summary":
            assert served == _PAYLOAD[:5]
        elif mode == "paginated":
            assert served["total"] == len(_PAYLOAD)
            assert served["page"] == 1
        else:
            assert served == [{"name": item["name"]} for item in _PAYLOAD]

    def test_bare_token_returns_one_bounded_page(self, rf: RequestFactory, shape: _Shape) -> None:
        """
        A token with no ``mode`` returns ONE PAGE, on every shape.

        **Updated on purpose: B2 changed this.**  This cell previously pinned
        the opposite — bare redemption returning the complete dataset — as
        amendment item (b), the one behaviour in this project ruled to need an
        ADR *decision* rather than conformance.  Jeremy ruled B2, so the
        decision arrived through the ADR process exactly as the pin intended
        and this assertion is updated deliberately rather than relaxed.

        The point of the pin survives the flip: the default is still asserted
        explicitly on every shape, so it still cannot drift editorially.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, shape.probe_args))
            cache.get.return_value = _minted_entry(cache)
            served = _result(_call(rf, name, shape.redeem_args(probe["continuation_token"])))

        # Bounded, not complete: a page envelope rather than the raw payload.
        assert served != _PAYLOAD, "bare token still returned the complete dataset"
        assert served["page"] == 1
        assert served["total"] == len(_PAYLOAD)
        assert served["has_more"] is True
        assert served["items"] == _PAYLOAD[: served["page_size"]]

    def test_bare_token_on_a_single_object_returns_it_whole(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """
        T18: B2's bounded default must not mangle a single-object result.

        ``paginated`` is only coherent for a sequence.  A single object — any
        ``@mcp_heavy`` ``retrieve``, and every ADR-004 write result — was being
        sliced into fixed-width pieces of its JSON serialisation, cut at an
        arbitrary offset and usually mid-token, so the caller received neither
        the object nor anything parseable as one.

        The object is already bounded, which is why it is returned whole rather
        than given a page envelope.  Asserted per shape because the redemption
        path is shared and a fix that only reached one shape would be worse
        than none.
        """
        obj = {"id": "abc-123", "name": "single", "payload": "x" * 4000}
        reg = ToolRegistry()
        name = shape.build(reg)
        # Replace the shape's payload with a single object rather than a list.
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _result(_call(rf, name, shape.probe_args))
            entry = _minted_entry(cache)
            cache.get.return_value = {**entry, "result": obj}
            served = _result(_call(rf, name, shape.redeem_args("tok")))

        assert "chunk" not in served, "single object was chunked by the bare default"
        assert served == obj

    def test_explicit_full_still_returns_the_complete_dataset(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """
        B2 bounds the *default*; it does not remove ``full``.

        The complete dataset stays available on every shape — it just has to be
        asked for, so an omission or a typo cannot select it by accident.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, shape.probe_args))
            cache.get.return_value = _minted_entry(cache)
            served = _result(
                _call(rf, name, shape.redeem_args(probe["continuation_token"], mode="full"))
            )

        assert served == _PAYLOAD

    def test_unknown_mode_is_rejected_and_names_the_enum(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """
        B2: an unrecognised mode is a validation error, not a silent fallback.

        Serving *something* for a typo hides the mistake; under the old
        behaviour a mistyped mode quietly returned the most expensive response
        available.  The message names the public enum and nothing else — no
        caller-specific, tool-specific or deploy-state detail — so it cannot
        become the oracle this project already refused to add elsewhere.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, shape.probe_args))
            cache.get.return_value = _minted_entry(cache)
            response = _call(rf, name, shape.redeem_args(probe["continuation_token"], mode="bogus"))

        payload = json.loads(response.content)["result"]
        assert payload["isError"] is True
        error = json.loads(payload["content"][0]["text"])["error"]
        assert "bogus" in error
        for mode in _MODES:
            assert mode in error, f"error does not name supported mode {mode!r}"


# ---------------------------------------------------------------------------
# G2 — SEC-3 owner-key binding granularity
# ---------------------------------------------------------------------------


class TestOwnerKeyBinding:
    """
    SEC-3 binding is asserted as *string identity*, not round-trip success.

    A round-trip test alone is insufficient: it passes if the mint side and the
    redeem side drift to the inner tool name together, silently narrowing the
    binding from the group to one resource while every test stays green.  These
    assertions name the string.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload."""

    def test_minted_key_equals_redeem_side_key(self, rf: RequestFactory, shape: _Shape) -> None:
        """The owner key computed at redemption equals the one stored at mint."""
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call(rf, name, shape.probe_args)
            entry = _minted_entry(cache)

        expected = _heavy_owner_key(_request(rf, name, shape.probe_args), name)
        assert entry["owner_key"] == expected

    def test_group_binding_is_the_group_not_the_resource(self, rf: RequestFactory) -> None:
        """
        G2: a group call binds to the group tool, never to the inner member.

        This is the assertion a round trip cannot make.  Both ends resolving to
        ``item_list`` would still redeem successfully, while narrowing SEC-3 so
        a token minted on one resource could be replayed against another.
        """
        shape = _SHAPES["group_dispatcher"]
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call(rf, name, shape.probe_args)
            entry = _minted_entry(cache)

        probe_request = _request(rf, name, shape.probe_args)
        assert entry["owner_key"] == _heavy_owner_key(probe_request, "catalog")
        assert entry["owner_key"] != _heavy_owner_key(probe_request, "item_list")
        assert entry["tool_name"] == "catalog"

    def test_owner_key_embeds_the_tool_name(self, rf: RequestFactory, shape: _Shape) -> None:
        """
        The tool component is present verbatim, so cross-tool replay is refused.

        Named explicitly rather than inferred from a mismatch, so a refactor
        that drops the tool segment fails here rather than silently widening
        every token's replay surface.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        assert f"tool={name}:" in _heavy_owner_key(_request(rf, name, shape.probe_args), name)


# ---------------------------------------------------------------------------
# G4 — redemption short-circuits
# ---------------------------------------------------------------------------


class TestRedemptionShortCircuits:
    """
    Redemption serves from cache: no re-dispatch, no second token.

    A redemption that re-ran the query would double the cost of every
    negotiation and, worse, re-mint — pinning a second cache entry per page and
    amplifying the SEC-3 surface with tokens nobody tracks.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload."""

    def test_redemption_does_not_re_invoke_the_tool(self, rf: RequestFactory) -> None:
        """The underlying callable runs on call 1 only."""
        reg = ToolRegistry()
        fn = _payload_fn(_PAYLOAD)
        reg.register(
            name="item_list",
            fn=fn,
            description="Counting tool.",
            # This test is about re-dispatch, so it needs a shape the backstop
            # will actually mint for — and minting requires a schema that
            # discloses.  Registering straight through the registry bypasses
            # every merge, so the branch is applied here by hand.
            #
            # The rationale used to read "production, where every
            # backstop-reachable tool arrives via @mcp_tool or auto-discovery
            # and therefore discloses".  CR-2 made that false, and false in the
            # sharpest direction: those two paths are now precisely the ones
            # that do NOT disclose.  In production a disclosing shape is an
            # @mcp_heavy tool or a dispatcher.
            #
            # Kept as a hand-applied merge rather than rebuilt on @mcp_heavy
            # because this test asserts re-dispatch, not registration, and the
            # bare-registry fixture keeps that isolated.  Without the merge the
            # backstop correctly refuses to mint and the test would fail for a
            # reason that has nothing to do with re-dispatch.
            input_schema=merge_continuation_branch({"type": "object", "properties": {}}),
            permission_tier="read",
        )
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, "item_list", {}))
            assert fn.calls == 1  # type: ignore[attr-defined]

            cache.get.return_value = _minted_entry(cache)
            _call(rf, "item_list", {"continuation_token": probe["continuation_token"]})

        assert fn.calls == 1, "redemption re-dispatched to the tool"  # type: ignore[attr-defined]

    def test_redemption_does_not_mint_a_second_token(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """No ``cache.set`` and no fresh token in the served response."""
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, shape.probe_args))
            entry = _minted_entry(cache)

            cache.set.reset_mock()
            cache.get.return_value = entry
            served = _result(
                _call(rf, name, shape.redeem_args(probe["continuation_token"], "full"))
            )

        assert not cache.set.called, "redemption re-minted a continuation token"
        assert not (isinstance(served, dict) and "continuation_token" in served)


# ---------------------------------------------------------------------------
# A3 / ADR-007 — misplaced token is rejected loudly
# ---------------------------------------------------------------------------


class TestMisplacedTokenGuard:
    """
    A token nested in ``params`` is rejected with a message that teaches the fix.

    ADR-007 line 61 chose loud rejection over silent normalisation: normalising
    would let a client keep sending it wrong forever, and forwarding it to the
    action re-mints a fresh token on every call.
    """

    def test_params_nested_token_is_rejected(self, rf: RequestFactory, shape: _Shape) -> None:
        """Both dispatcher shapes raise; the flat shape has no ``params`` to misplace into."""
        if not shape.has_params:
            reg = ToolRegistry()
            name = shape.build(reg)
            props = reg.get_entry(name).input_schema["properties"]
            assert "params" not in props, (
                "the flat shape grew a 'params' key — it is now capable of"
                " misplacement and needs a real guard assertion here"
            )
            return

        reg = ToolRegistry()
        name = shape.build(reg)
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        args = {**shape.routing_args, "params": {"continuation_token": "abc123"}}
        with pytest.raises(ToolInputError) as excinfo:
            reg.dispatch(req, name, args)

        msg = str(excinfo.value)
        assert "continuation_token" in msg
        assert "TOP LEVEL" in msg

    def test_rejection_message_is_shape_neutral(self, rf: RequestFactory, shape: _Shape) -> None:
        """
        The guard is shared, so its message must not name one shape's arguments.

        The group shape carries ``resource`` and the class shape does not; a
        message enumerating either is wrong for the other.
        """
        if not shape.has_params:
            pytest.skip("flat shape has no params object and never reaches the guard")

        reg = ToolRegistry()
        name = shape.build(reg)
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        with pytest.raises(ToolInputError) as excinfo:
            reg.dispatch(req, name, {**shape.routing_args, "params": {"continuation_token": "abc"}})

        msg = str(excinfo.value)
        assert "'resource'" not in msg
        assert "sibling of 'action' and 'params'" not in msg

    def test_misplaced_token_does_not_reach_the_action(self, rf: RequestFactory) -> None:
        """
        The guard fires before dispatch, so no query runs and nothing is minted.

        Rejection that happened *after* the action ran would still leak the
        extra query the guard exists to prevent.
        """
        reg = ToolRegistry()
        shape = _SHAPES["group_dispatcher"]
        name = shape.build(reg)
        inner = reg.get_entry("item_list").fn
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        with pytest.raises(ToolInputError):
            reg.dispatch(
                req,
                name,
                {**shape.routing_args, "params": {"continuation_token": "abc"}},
            )

        assert inner.calls == 0, "the action ran before the misplaced token was rejected"


# ---------------------------------------------------------------------------
# A4 — flat argument form on a group call
# ---------------------------------------------------------------------------


class TestGroupFlatArgumentForm:
    """
    Schema-driven clients that cannot nest send ``{resource, action, key: val}``.

    The flat sweep must keep ``resource`` out of ``params`` — otherwise every
    flat-form group call breaks — while still letting the four colliding
    negotiation names through, because they are real host data outside a
    continuation call.
    """

    def test_resource_is_not_swept_into_params(self, rf: RequestFactory) -> None:
        """``resource`` is routing, not an action parameter."""
        reg = ToolRegistry()
        echo = _echo_registry(reg)
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        reg.dispatch(req, "catalog", {"resource": "item", "action": "list", "name": "x"})
        assert echo["params"] == {"name": "x"}

    @pytest.mark.parametrize("collider", ["mode", "page", "page_size", "filter_keys"])
    def test_colliding_names_reach_the_action(self, rf: RequestFactory, collider: str) -> None:
        """
        ``mode``/``page``/``page_size``/``filter_keys`` are host data here.

        Only ``continuation_token`` is unambiguously protocol.  ``mode`` is a
        genuine model field on real host applications and ``page``/``page_size``
        are DRF's own pagination parameters, so treating them as reserved would
        break ordinary filtering on a flat-form call.
        """
        reg = ToolRegistry()
        echo = _echo_registry(reg)
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        reg.dispatch(req, "catalog", {"resource": "item", "action": "list", collider: "v"})
        assert echo["params"] == {collider: "v"}

    def test_continuation_token_is_not_swept_into_params(self, rf: RequestFactory) -> None:
        """
        The one protocol-only key stays out of ``params`` on the flat form.

        Sent top-level it is consumed by the negotiation layer; it must never
        arrive at the action, where a filterset would reject it as unknown.
        """
        reg = ToolRegistry()
        echo = _echo_registry(reg)
        req = rf.post("/mcp/", content_type="application/json")
        req.auth = None  # type: ignore[attr-defined]
        reg.dispatch(
            req,
            "catalog",
            {"resource": "item", "action": "list", "continuation_token": "tok"},
        )
        assert "continuation_token" not in echo["params"]


def _echo_registry(reg: ToolRegistry) -> dict[str, Any]:
    """Register a group over a member that records the params it was handed."""
    seen: dict[str, Any] = {}

    def _echo(arguments: dict[str, Any], _request: Any) -> Any:
        seen["params"] = arguments
        return {"ok": True}

    reg.register(
        name="item_list",
        fn=_echo,
        description="Echoing member.",
        input_schema={"type": "object", "properties": {}},
        permission_tier="read",
    )
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", frozenset({"item_list"}), reg, frozenset({"item"})),
        description="Group dispatcher.",
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=frozenset({"item_list"}),
    )
    reg.set_hidden("item_list", True)
    return seen


# ---------------------------------------------------------------------------
# @mcp_heavy on the dispatcher shapes — the T2 gap
# ---------------------------------------------------------------------------


class TestMcpHeavyOnDispatcherShapes:
    """
    ``@mcp_heavy`` resolution through a dispatcher (R1/T2).

    A dispatcher entry is never itself ``is_heavy``: only ``@mcp_heavy`` sets
    the flag (``decorators.py:318``) and it marks the underlying flat tool, so
    resolving it on the outer name meant the explicit heavy path never fired for
    a grouped call.  ``views.py:1779-1785`` now resolves through to the routed
    entry via ``_dispatcher_target_entry``.

    The two shapes differ in what resolution can even mean, which is why they
    are asserted separately rather than as one parameterised cell:

    * **group** — members are real registry entries and can carry ``@mcp_heavy``,
      so there is an inner entry to resolve.
    * **class** — ``@mcp_action`` methods are not registry entries at all
      (``decorators.py:184`` registers exactly one entry per dispatcher), so
      there is no inner entry to resolve and no amount of indirection reaches
      one.  A heavy class action remains unexpressible, and
      ``_dispatcher_target_entry`` correctly returns ``None`` for it.
    """

    def test_class_dispatcher_registers_exactly_one_entry(self) -> None:
        """
        Structural premise for the asymmetry above, asserted rather than assumed.

        If actions ever become registry entries this fails, and the claim that a
        heavy class action is unexpressible has to be revisited.
        """
        reg = ToolRegistry()
        name = _build_class_dispatcher(reg)
        assert reg.get_entry(name) is not None
        assert reg.get_entry(f"{name}_list") is None
        assert reg.get_entry("list") is None

    def test_no_dispatcher_entry_is_heavy(self, shape: _Shape) -> None:
        """
        Documents the mechanism: dispatcher entries never carry ``is_heavy``.

        Not a bug on its own — the flag is about the *outer* tool — but it is
        why resolving ``is_heavy`` on ``get_entry(tool_name)`` alone can never
        be true for a grouped heavy member, and therefore why the
        ``_dispatcher_target_entry`` indirection has to exist.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        entry = reg.get_entry(name)
        assert entry.is_heavy is (shape.id == "flat_heavy")

    @override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=None)
    def test_class_dispatcher_foreign_resource_neither_probes_nor_mints(
        self, rf: RequestFactory
    ) -> None:
        """
        T15: a caller-supplied ``resource`` must not reach the global registry.

        A class dispatcher never reads ``resource`` (``backends/dispatcher.py``
        mentions it only in a comment), so ``{action, params, resource: X}``
        dispatches normally — and before T15 the heavy branch then resolved
        ``X_list`` against the **global** registry with no tier, permission or
        ``_mcp_max_tier`` check between the resolve and the mint.

        Two distinct effects are asserted, because closing only the first
        leaves the second:

        1. **No forced probe.**  The caller cannot make an unrelated tool's
           ``@mcp_heavy`` flag shape *their* response.  The probe/normal-result
           difference is also a one-bit oracle answering "does this name exist
           and is it heavy?" for any guessed name.
        2. **Nothing mints.**  This is the part that raised severity above
           cosmetic: a mint pins a 300s entry in Django's shared default cache
           for a response of *any* size, bypassing the 25,000-byte threshold
           that normally gates it.

        The backstop is disabled so a mint here could only come from the heavy
        branch, and the payload is deliberately tiny — under the real threshold
        — so a pre-T15 mint is unambiguously the bypass rather than a
        legitimate over-threshold negotiation.
        """
        reg = ToolRegistry()
        name = _build_class_dispatcher(reg)

        # A registered, genuinely heavy tool that is NOT reachable through this
        # dispatcher.  `resource="item"` + `action="list"` would resolve it.
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_heavy(
                name="item_list",
                description="Heavy tool outside the dispatcher.",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
                return {"small": "payload"}

            _ = _fn

        assert reg.get_entry("item_list").is_heavy, "premise: the foreign tool is heavy"

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, name, {"action": "list", "params": {}, "resource": "item"}))

            assert "continuation_token" not in result, "foreign resource forced a probe envelope"
            assert not cache.set.called, "foreign resource minted a cache entry"

    @override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=None)
    def test_group_call_on_heavy_member_negotiates(self, rf: RequestFactory) -> None:
        """
        G1 + G2: a sub-threshold ``@mcp_heavy`` member probes, bound to the group.

        The backstop is disabled here deliberately, so ``@mcp_heavy`` is the only
        thing that can mint — isolating the explicit heavy path from the size
        backstop that used to mask its absence.

        The owner key is asserted as a **string**, against the group name and
        against the inner name, rather than by round-tripping a redemption.  A
        round trip cannot tell the two apart: resolution that also moved the
        bound name to ``item_list`` would still redeem successfully in-process
        while narrowing SEC-3 from the group to one resource.  Redemption only
        ever knows the outer name — the continuation path never sees
        ``resource``/``action`` — so mint must stay outer or every grouped
        redemption fails owner-mismatch.
        """
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_heavy(
                name="item_list",
                description="Heavy member.",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
                return {"small": "payload"}

            _ = _fn

        reg.register(
            name="catalog",
            fn=make_group_invoke("catalog", frozenset({"item_list"}), reg, frozenset({"item"})),
            description="Group dispatcher.",
            input_schema=build_group_input_schema(),
            permission_classes=[],
            permission_tier="read",
            is_dispatcher=True,
            group_tool_names=frozenset({"item_list"}),
        )
        reg.set_hidden("item_list", True)

        args = {"resource": "item", "action": "list", "params": {}}
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, "catalog", args))
            assert "continuation_token" in result
            entry = _minted_entry(cache)

            cache.get.return_value = entry
            # B2 (ADR-005 item (b)): `mode="full"` is now explicit. This cell is
            # about the owner binding being *usable*, not about the default, so
            # it asks for the whole payload rather than asserting whatever the
            # default happens to be — that is TestProbeRedeemRoundTrip's job.
            served = _result(
                _call(
                    rf,
                    "catalog",
                    {
                        **args,
                        "continuation_token": result["continuation_token"],
                        "mode": "full",
                    },
                )
            )

        probe_request = _request(rf, "catalog", args)
        assert entry["owner_key"] == _heavy_owner_key(probe_request, "catalog")
        assert entry["owner_key"] != _heavy_owner_key(probe_request, "item_list")
        assert entry["tool_name"] == "catalog"
        # The binding is not merely consistent — it is redeemable end to end.
        assert served == {"small": "payload"}


# ---------------------------------------------------------------------------
# The probe envelope's teaching text
# ---------------------------------------------------------------------------


class TestProbeEnvelopeTeachingText:
    """
    The envelope is the *primary* placement instruction, not the fallback.

    Per the comment above ``_build_probe_envelope``: an agent mid-negotiation is
    not re-reading ``tools/list``, so the envelope that advertises the modes has
    to say where the fields go.  The schema text is what a client reads once at
    connect time; this string is what it reads at the moment it has a token in
    hand.  There is exactly one builder for all shapes and neither call site
    passes the shape, so the text must be shape-neutral.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """
        Every shape must actually reach the envelope.

        Without this the dispatcher shapes stay under the default threshold and
        return their raw payload, so the assertions below would fail on a
        ``TypeError`` from subscripting a list — a red cell that says nothing
        about the text it claims to be testing.
        """

    def test_bare_token_clause_is_unchanged(self, rf: RequestFactory) -> None:
        """
        BYTE-IDENTITY GUARD: the bare-token sentence must not drift editorially.

        **The guarded text moved once, on purpose.**  This originally pinned
        "Omitting 'mode' returns the COMPLETE dataset" as amendment item (b) —
        the one behaviour ruled to need an ADR *decision* rather than
        conformance — because it shares a single string with the shape wording
        corrected in T3, so a cleanup of the latter could silently reword the
        former: a contract change made in a string edit, with no ADR and
        nothing in the diff that looks like a decision.

        The mechanism worked.  The clause survived four code tasks, two
        reviewers and a bot review untouched, and the decision arrived through
        the ADR process as B2.  The guard is **updated, not removed** — it now
        pins the B2 wording for exactly the same reason.  Whatever this
        sentence says, changing it must be a ruling, not an edit.
        """
        reg = ToolRegistry()
        name = _SHAPES["flat_heavy"].build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            usage = _result(_call(rf, name, {}))["usage"]

        assert "Omitting 'mode' returns ONE PAGE" in usage
        assert "pass mode='full' explicitly for the complete dataset" in usage
        assert "COMPLETE dataset" not in usage, "pre-B2 wording resurfaced"

    def test_placement_text_is_shape_neutral(self, rf: RequestFactory, shape: _Shape) -> None:
        """
        A5, on the consumer that most needs it.

        The flat shape has no ``action`` and no ``params``; naming them as the
        siblings instructs the one caller who cannot comply.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            usage = _result(_call(rf, name, shape.probe_args))["usage"]

        assert "TOP LEVEL" in usage
        assert "'action'" not in usage
        assert "'params'" not in usage or "not inside 'params'" in usage

    def test_companion_fields_are_described_here_not_in_the_schema(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """
        CR-15: ``filter_keys``, ``page`` and ``page_size`` are taught here.

        CR-14 removed their schema descriptions.  This is where that guidance
        went, and the move is not merely a cost saving: these three are
        meaningful **only** on a continuation call, which requires a token,
        which only ever arrives in this envelope.  The agent therefore always
        reads this before it could use them — which the schema could never
        guarantee.

        ``filter_keys`` is the load-bearing one.  ``available_modes`` advertises
        ``filtered``; with nothing saying that mode needs a companion field or
        what it holds, the mode is visible and unusable — the T6 failure this
        class exists for, one level down.

        Parameterised by shape rather than copied: one builder serves all three,
        so a clause that is true only for the dispatchers would be a silent lie
        to the flat caller.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            usage = _result(_call(rf, name, shape.probe_args))["usage"]

        # 'filtered' is advertised in available_modes -- it must not be a mode
        # the caller can see and cannot use.
        assert "filter_keys" in usage
        assert "'filtered'" in usage

        # 'page_size' default source, otherwise undiscoverable from the contract.
        assert "FRISIAN_MCP_HEAVY_PAGE_SIZE" in usage

        # 'page' is 1-based; `default: 1` in the schema only implies it.
        assert "1-based" in usage

        # A5 again: the new clauses must stay shape-neutral like the rest.
        assert "'resource'" not in usage


# ---------------------------------------------------------------------------
# The ordinary shapes disclose nothing -- schema AND behaviour (CR-2)
# ---------------------------------------------------------------------------


class TestOrdinaryShapesDoNotDisclose:
    """
    The negative contract, parameterised exactly like the positive one.

    ``plain_tool`` and ``auto_discovered`` were parameters of every *positive*
    disclosure test until CR-2, which made the token regression a **required**
    contract: CI demanded that ordinary tools keep publishing the heavy protocol
    forever.  Deleting them from the matrix would have removed the demand and
    left nothing in its place -- so they stay parameters, held to the opposite
    contract.  Shape is still a parameter here, never a copy.

    Both halves are asserted, because either alone lets the regression back:

    * **Schema** -- nothing is published, checked through the mint gate's own
      ``schema_discloses_continuation`` so the test cannot disagree with the
      gate about what disclosure means.
    * **Behaviour** -- an over-threshold response comes back WHOLE, mints no
      token and pins nothing in the heavy cache.  A schema assertion alone would
      pass a build that quietly kept minting against an undisclosed schema,
      which is the original defect.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload -- or rather, do not."""

    def test_schema_does_not_disclose(self, non_disclosing_shape: _Shape) -> None:
        """The mint gate's own predicate returns False for the published schema."""
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        schema = reg.get_entry(name).input_schema
        assert schema_discloses_continuation(schema) is False

    def test_no_negotiation_field_is_reachable(self, non_disclosing_shape: _Shape) -> None:
        """
        Not one of the five fields is reachable, by either disclosure style.

        Uses the same reachability resolver the positive tests use, so "does not
        disclose" is the exact complement of "discloses" rather than a narrower
        claim about ``properties`` that an ``allOf`` branch could slip past.
        """
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        schema = reg.get_entry(name).input_schema
        assert _reachable_negotiation_fields(schema) == set()

    def test_no_negotiation_allof_branch(self, non_disclosing_shape: _Shape) -> None:
        """No conditional branch keyed on ``continuation_token`` survives."""
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        schema = reg.get_entry(name).input_schema
        for branch in schema.get("allOf", []):
            assert branch.get("if") != {"required": [NEGOTIATION_PROTOCOL_ONLY_KEY]}

    def test_the_fixture_would_actually_change_if_merged(
        self, non_disclosing_shape: _Shape
    ) -> None:
        """
        GUARD: the negative assertions above are not passing for a trivial reason.

        ``merge_continuation_branch`` returns a schema declaring
        ``"additionalProperties": false`` unchanged (H18).  So a fixture that
        happened to be closed would satisfy every assertion in this class **on
        the pre-CR-2 tree as well** -- a green that proves nothing and would stay
        green if CR-2 were reverted.

        Asserting the merge WOULD change these schemas pins them open, so the
        negative contract keeps costing something to satisfy.
        """
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        schema = reg.get_entry(name).input_schema
        assert schema.get("additionalProperties") is not False
        assert merge_continuation_branch(json.loads(json.dumps(schema))) != schema

    def test_over_threshold_response_returns_the_whole_payload(
        self, rf: RequestFactory, non_disclosing_shape: _Shape
    ) -> None:
        """
        The documented consequence: the response "is returned whole".

        Whole is the point, and the part worth failing over.  Silently
        truncating an over-threshold response would be a worse outcome than the
        schema bloat CR-2 removed, so this asserts the full payload rather than
        merely the absence of an envelope.
        """
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, name, non_disclosing_shape.probe_args))

        assert result == _PAYLOAD

    def test_over_threshold_response_mints_and_pins_nothing(
        self, rf: RequestFactory, non_disclosing_shape: _Shape
    ) -> None:
        """
        No token issued, and nothing written to the heavy cache.

        Read off ``django_cache.set`` rather than off the response body: a token
        minted and then dropped would still have pinned a shared-cache entry for
        the TTL, which is the cost SEC-3 cares about.
        """
        reg = ToolRegistry()
        name = non_disclosing_shape.build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, name, non_disclosing_shape.probe_args))

        assert not cache.set.called, "minted a token for a schema that cannot return it"
        assert not isinstance(result, dict) or "continuation_token" not in result


# ---------------------------------------------------------------------------
# The backstop refuses to mint on shapes that disclose nothing
# ---------------------------------------------------------------------------


class TestBackstopDoesNotMintOnUndisclosedShapes:
    """
    NO DISCLOSURE, NO MINT -- asserted from both sides (CR-2).

    History, because the name of this class has now meant three things.
    Originally ``@mcp_tool`` registered ``input_schema`` verbatim while the
    backstop minted a continuation token for *any* over-threshold read, so a
    plain flat tool issued a token its own published schema gave the caller no
    legal slot to send back.  Redemption worked in-process (the token is read
    before schema validation), which is why the defect stayed invisible to the
    suite and only bit through a real client that validates against the schema.

    H2 closed that by making **every** schema disclose.  That worked, and cost
    ~380 tokens on every ordinary published schema (CR-1) -- paid by every host,
    including those that never wanted negotiation.

    CR-2 closes the same defect from the other side, which is where the code
    already enforced it: ``schema_discloses_continuation()`` gates the mint
    against the published schema, so a tool that does not disclose does not
    mint.  Disclosure was the expensive half of a belt-and-braces pair, and the
    redundant one.

    Both halves are still checked, because either alone permits the defect to
    return: the ordinary tool **does not disclose**, and the backstop **does not
    mint** for it.  Disclosure and mint eligibility derive from the one schema
    object, so they cannot drift apart.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload."""

    def test_plain_tool_does_not_mint_a_token(self, rf: RequestFactory) -> None:
        """
        An over-threshold plain tool gets its payload WHOLE, not a probe envelope.

        The inverse of what this test asserted under H2.  ``mints nothing`` is
        read off ``django_cache.set`` rather than inferred from the response, so
        a token minted and then discarded would still fail.
        """
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_tool(
                name="item_list",
                description="Plain tool.",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
                return _PAYLOAD

            _ = _fn

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, "item_list", {}))

        assert not isinstance(result, dict) or "continuation_token" not in result
        assert not cache.set.called, "minted a token for a schema that cannot return it"
        # Whole means whole: the complete payload, not a truncated stand-in.
        assert result == _PAYLOAD

    def test_plain_tool_publishes_no_redemption_surface(self) -> None:
        """
        CR-2: the caller's schema is published exactly as handed to ``@mcp_tool``.

        Asserted through ``schema_discloses_continuation`` -- the same predicate
        the mint gate consults -- rather than by inspecting ``properties``
        directly, so this test and the gate cannot disagree about what
        "discloses" means.
        """
        reg = ToolRegistry()
        registered = {"type": "object", "properties": {}}
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_tool(
                name="item_list",
                description="Plain tool.",
                input_schema=json.loads(json.dumps(registered)),
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
                return _PAYLOAD

            _ = _fn

        schema = reg.get_entry("item_list").input_schema
        assert schema_discloses_continuation(schema) is False
        assert "continuation_token" not in schema["properties"]
        assert "allOf" not in schema
        # Byte-identical to what the host handed us -- the package's contribution
        # to an ordinary schema is zero, not merely small.
        assert json.dumps(schema) == json.dumps(registered)

    def test_host_field_collision_still_leaves_the_token_redeemable(self) -> None:
        """
        KNOWN LIMIT, pinned deliberately: selection narrows, redemption survives.

        A host field named ``mode`` narrows mode *selection* but never makes the
        token unredeemable.

        ``mode``/``page``/``page_size``/``filter_keys`` can be genuine host
        field names, which is why they are declared in the conditional branch
        rather than at the top level.  JSON Schema constraints are additive:
        the branch cannot relax an enum the host already declared, so on such a
        tool a strictly-validating client cannot send ``mode="full"``.

        What matters is that this degrades selection, not redemption.  A bare
        ``continuation_token`` still validates, and since B2 a bare redemption
        is ``paginated`` — the bounded outcome the backstop wanted anyway.  So
        the H2 invariant holds: the caller can always legally return the token.

        If this ever needs to become full selection, the fix is namespacing the
        protocol fields, which is a contract change and needs its own ruling.
        """
        jsonschema = pytest.importorskip("jsonschema")

        schema = merge_continuation_branch(
            {
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["fast", "slow"]}},
            }
        )
        validator = jsonschema.Draft7Validator(schema)

        def _valid(payload: dict[str, Any]) -> bool:
            return not list(validator.iter_errors(payload))

        # The host's own field keeps its meaning on an ordinary call.
        assert _valid({"mode": "fast"})
        assert not _valid({"mode": "nope"})

        # The token is redeemable — that is the invariant.
        assert _valid({"continuation_token": "t"})
        assert _valid({"continuation_token": "t", "page": 2, "page_size": 10})

        # ...but the host's enum still constrains `mode`, so selection narrows.
        assert not _valid({"continuation_token": "t", "mode": "full"})

    def test_backstop_does_not_mint_for_an_undisclosed_schema(self, rf: RequestFactory) -> None:
        """
        The invariant, from the other side: no disclosure, no mint.

        Registering straight through ``tool_registry`` bypasses the decorator's
        merge, standing in for any future registration path that forgets to
        disclose.  The backstop must refuse to mint rather than issue a token
        the caller cannot legally return — otherwise a new path silently
        re-opens the fourth shape.
        """
        reg = ToolRegistry()
        reg.register(
            name="item_list",
            fn=lambda _arguments, _request: _PAYLOAD,
            description="Undisclosed tool.",
            input_schema={"type": "object", "properties": {}},
        )

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            result = _result(_call(rf, "item_list", {}))

        assert "continuation_token" not in result
        assert not cache.set.called, "minted a token for a schema that cannot return it"


class TestH6HeavyCacheIsolation:
    """
    H6: continuation entries land in their own eviction domain when one is configured.

    The W016 check reports a *misconfiguration*; these assert the *behaviour*.
    Both are needed — a check that warns correctly while the code still writes
    everything to ``default`` would leave the exposure untouched and look fixed.
    """

    def test_continuation_entries_land_on_the_configured_alias_not_default(
        self, rf: RequestFactory
    ) -> None:
        """
        The isolation itself: a mint must reach the heavy alias and MISS ``default``.

        Asserted in both directions deliberately.  Checking only that the heavy
        cache received the entry would still pass if the code wrote to *both*,
        which is not isolation.
        """
        from django.core.cache import caches

        from frisian_mcp.views import _HEAVY_CACHE_PREFIX

        two_domains = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "h6-default",
            },
            "h6_heavy": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "h6-heavy",
            },
        }

        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_heavy(
                name="item_list",
                description="Heavy tool.",
                input_schema={"type": "object", "properties": {}},
            )
            def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
                return _PAYLOAD

            _ = _fn

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            override_settings(CACHES=two_domains, FRISIAN_MCP_HEAVY_CACHE_ALIAS="h6_heavy"),
        ):
            heavy_cache = caches["h6_heavy"]
            default_cache = caches["default"]
            heavy_cache.clear()
            default_cache.clear()

            result = _result(_call(rf, "item_list", {}))

            token = result["continuation_token"]
            key = f"{_HEAVY_CACHE_PREFIX}{token}"

            assert (
                heavy_cache.get(key) is not None
            ), "continuation entry did not reach the heavy alias"
            assert default_cache.get(key) is None, (
                "continuation entry also landed on 'default' — the security "
                "state's cache — so nothing was isolated"
            )

    def test_ttl_is_settings_driven(self) -> None:
        """
        H6 decision 4: TTL is a setting, not a constant.

        A blast-radius control only — a caller can mint many short-lived
        entries — but it cannot be tuned at all while it is hardcoded.
        """
        from frisian_mcp.views import _heavy_cache_ttl

        with override_settings(FRISIAN_MCP_HEAVY_CACHE_TTL=42):
            assert _heavy_cache_ttl() == 42


class TestAdr011ResolvedTarget:
    """
    ADR-011 §5 — the minted entry records the *server-resolved child*.

    These assertions exist because §4's re-authorization is silently vacuous
    without them: the outer dispatcher is always mounted, so a membership check
    against the outer name passes unconditionally while the child — the thing
    whose route containment actually matters — goes unchecked.  Reverting the
    mint-site wiring previously left the whole suite green.

    Read off ``django_cache.set`` via :func:`_minted_entry`, so this describes
    what the view stored rather than what the test hoped it would store.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """The backstop is the only mint path reachable on the group shape."""

    def test_group_mint_records_the_child_not_the_dispatcher(self, rf: RequestFactory) -> None:
        """§5: a grouped mint retains the server-resolved child, not the outer name."""
        reg = ToolRegistry()
        name = _build_group_dispatcher(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call(rf, name, {"resource": "item", "action": "list", "params": {}})
            entry = _minted_entry(cache)

        # The owner key still binds the outer group (G1) — §5 is a third fact.
        assert entry["tool_name"] == "catalog"
        assert entry["resolved_target"] == "item_list"

    def test_the_other_resource_in_the_same_group_resolves_to_itself(
        self, rf: RequestFactory
    ) -> None:
        """A per-resource fact, not a constant baked in at the group level."""
        reg = ToolRegistry()
        name = _build_group_dispatcher(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call(rf, name, {"resource": "container", "action": "list", "params": {}})
            entry = _minted_entry(cache)

        assert entry["resolved_target"] == "container_list"

    def test_flat_mint_records_itself(self, rf: RequestFactory) -> None:
        """§5: a flat mint records the tool itself as the resolved target."""
        reg = ToolRegistry()
        name = _build_flat_heavy(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call(rf, name, {})
            entry = _minted_entry(cache)

        assert entry["resolved_target"] == name


class TestH19MissingAliasDisablesNegotiation:
    """
    H19: a misconfigured alias must not silently relocate continuation state.

    The check (E009) catches this at ``manage.py check`` / ``migrate`` time.  It
    cannot be the only defence: ``get_wsgi_application()`` calls ``django.setup()``
    and **not** the system-check framework, so a gunicorn/uWSGI process starts
    without ever running it.  These assert the runtime behaviour that holds when
    the check never executed.

    Asserted on **where the bytes went**, not on the return value alone — the
    exposure is continuation state landing in the cache that holds OAuth codes
    and the brute-force counter, so that is the thing to measure.
    """

    @pytest.fixture()
    def broken_alias(self, settings: Any) -> None:
        """Point the heavy alias at a cache that ``CACHES`` does not define."""
        settings.FRISIAN_MCP_HEAVY_CACHE_ALIAS = "does_not_exist"
        settings.FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = _THRESHOLD

    def test_nothing_is_written_to_the_default_cache(
        self, rf: RequestFactory, broken_alias: None
    ) -> None:
        """The security property: the OAuth cache gains nothing from a heavy read."""
        reg = ToolRegistry()
        name = _build_flat_heavy(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as default_cache,
        ):
            default_cache.get.return_value = None
            _call(rf, name, {})

        assert not default_cache.set.called, (
            "continuation state was written to the default cache after the "
            "configured alias failed to resolve — the isolation setting reads as "
            "on while the exposure it removes is back"
        )

    def test_the_caller_gets_the_whole_response_not_a_dead_token(
        self, rf: RequestFactory, broken_alias: None
    ) -> None:
        """
        Negotiation is unavailable, not broken.

        Minting a token that was never stored would hand back a continuation no
        redemption can satisfy — the unredeemable-token shape this project has
        already paid for once.
        """
        reg = ToolRegistry()
        name = _build_flat_heavy(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as default_cache,
        ):
            default_cache.get.return_value = None
            served = _result(_call(rf, name, {}))

        assert "continuation_token" not in served
        assert served == _PAYLOAD

    def test_a_working_alias_still_negotiates(self, rf: RequestFactory, settings: Any) -> None:
        """The control — the guard must not disable negotiation for everyone."""
        settings.FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = _THRESHOLD
        reg = ToolRegistry()
        name = _build_flat_heavy(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, name, {}))

        assert "continuation_token" in probe
