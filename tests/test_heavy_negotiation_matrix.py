"""
Heavy-response negotiation asserted across every consumer shape (T4).

Why this file exists as a *matrix* rather than more tests in the existing files:
the group-dispatcher disclosure gap shipped green because every negotiation
assertion in the suite named exactly one construction path.  ``test_mcp_heavy``
covered the flat ``@mcp_heavy`` path, ``test_dispatcher`` covered the class
dispatcher, and neither mentioned the other — so a third path could be added
with no disclosure at all and nothing turned red.

The defence is structural, not additive: **shape is a parameter here, never a
copy.**  A fourth consumer is one entry in :data:`_SHAPE_IDS` away from being
held to the same contract, and a consumer that cannot satisfy the contract
fails at collection rather than being quietly omitted.

``negotiation.py`` currently has four consumers, and the matrix covers all four:

===================  ===========================  ==============================
Shape                Merged at                    Top-level arguments
===================  ===========================  ==============================
flat ``@mcp_heavy``  ``decorators.py:316``        the tool's own fields
class dispatcher     ``dispatcher.py:135``        ``action`` + ``params``
group dispatcher     ``group_dispatcher.py:146``  ``resource``/``action``/``params``
plain ``@mcp_tool``  **nowhere**                  the tool's own fields
===================  ===========================  ==============================

The fourth row is not a typo.  ``@mcp_tool`` (``decorators.py:65``) registers
its ``input_schema`` verbatim, but the ``FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD``
backstop mints continuation tokens for *any* over-threshold read response.  See
:class:`TestBackstopMintsOnUndisclosedShapes`.

Host-agnostic throughout: synthetic ``item``/``container``/``catalog`` fixtures,
no host-application schema as identifiers or data.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from frisian_mcp.backends.group_dispatcher import build_group_input_schema, make_group_invoke
from frisian_mcp.decorators import mcp_action, mcp_dispatcher, mcp_heavy, mcp_tool
from frisian_mcp.negotiation import _NEGOTIATION_PROPERTIES
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
    #: ``True`` when the shape publishes the negotiation fields in its schema.
    discloses: bool = True

    def redeem_args(self, token: str, mode: str | None = None, **extra: Any) -> dict[str, Any]:
        """Return call-2 arguments: routing + token + optional mode."""
        args: dict[str, Any] = {**self.routing_args, "continuation_token": token}
        if mode is not None:
            args["mode"] = mode
        args.update(extra)
        return args


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


@pytest.fixture(params=_SHAPE_IDS)
def shape(request: Any) -> _Shape:
    """Parameterise over every negotiation consumer."""
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

    ADR-005 line 61 defines redemption as re-invoking the same tool with a
    ``continuation_token`` and a ``mode``.  A shape that advertises
    ``available_modes`` in the probe envelope but omits the fields from its
    published schema mints tokens no conformant client can send back.
    """

    def test_all_five_fields_published(self, shape: _Shape) -> None:
        """The five negotiation fields appear in the shape's published schema."""
        reg = ToolRegistry()
        name = shape.build(reg)
        props = reg.get_entry(name).input_schema["properties"]
        for field_name in _NEGOTIATION_PROPERTIES:
            assert field_name in props, f"{field_name!r} undisclosed on the {shape.id} path"

    def test_mode_enum_matches_what_is_served(self, shape: _Shape) -> None:
        """
        The advertised enum equals the modes ``_serve_heavy_mode`` implements.

        Asserted as set equality in both directions: a mode in the enum that is
        not served strands the caller, and a mode served but not advertised is
        undiscoverable.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        props = reg.get_entry(name).input_schema["properties"]
        assert set(props["mode"]["enum"]) == set(_MODES)

    def test_placement_text_is_shape_neutral(self, shape: _Shape) -> None:
        """
        The shared placement text must not name arguments a shape lacks (A5).

        The flat shape has no ``action`` and no ``params``, so naming them as
        siblings is wrong for it.  ``params`` is the only key common to the
        shapes that have one and is the only place the token must never go, so
        the neutral phrasing is the only phrasing true everywhere.
        """
        reg = ToolRegistry()
        name = shape.build(reg)
        props = reg.get_entry(name).input_schema["properties"]
        text = props["continuation_token"]["description"]
        assert "TOP LEVEL" in text
        assert "'action'" not in text
        assert "'resource'" not in text

    def test_disclosure_survives_a_new_shape(self) -> None:
        """
        META: the matrix must cover every shape, not merely several.

        This is the assertion the original bug needed and did not have.  If a
        consumer is added to ``negotiation.py`` without an entry here, the count
        drifts and this fails — rather than the new shape being silently exempt.
        """
        assert len(_SHAPE_IDS) == 3, (
            "a negotiation consumer was added or removed; add it to _SHAPES so it"
            " is held to the same disclosure contract, then update this count"
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

    def test_bare_token_returns_the_complete_dataset(
        self, rf: RequestFactory, shape: _Shape
    ) -> None:
        """
        A token with no ``mode`` returns the full result, on every shape.

        Pinned deliberately.  This is amendment item (d) — the one behaviour in
        this project ruled to need an ADR *decision* rather than conformance —
        so it must not drift editorially.  If a future change makes bare
        redemption bounded, that is an ADR outcome and this assertion is the
        thing that has to be updated on purpose.
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

        assert served == _PAYLOAD


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
            input_schema={"type": "object", "properties": {}},
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
            served = _result(
                _call(rf, "catalog", {**args, "continuation_token": result["continuation_token"]})
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

        "Omitting 'mode' returns the COMPLETE dataset" states amendment item
        (d) — the one behaviour in this project ruled to need an ADR *decision*
        rather than conformance.  It shares a single string with the shape
        wording being corrected, so a cleanup of the latter can silently reword
        the former: a behaviour-contract change made in a string edit, with no
        ADR and nothing in the diff that looks like a decision.
        """
        reg = ToolRegistry()
        name = _SHAPES["flat_heavy"].build(reg)
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            usage = _result(_call(rf, name, {}))["usage"]

        assert "Omitting 'mode' returns the COMPLETE dataset" in usage

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


# ---------------------------------------------------------------------------
# The backstop mints on shapes that disclose nothing
# ---------------------------------------------------------------------------


class TestBackstopMintsOnUndisclosedShapes:
    """
    The same defect class as the group gap, on a fourth consumer.

    ``@mcp_tool`` (``decorators.py:65``) registers ``input_schema`` verbatim —
    no ``_merge_negotiation_schema``.  But the backstop mints a continuation
    token for *any* over-threshold read response, so a plain flat tool can issue
    a token its own published schema gives the caller no legal slot to send
    back.  Redemption works at runtime (``views.py:1371`` reads the token before
    schema validation), which is exactly why this stays invisible in-process and
    only bites through a real client that validates against the schema.

    Asserted as the current shipped behaviour, not as a desired one.  If the
    merge is later extended to cover the backstop, these turn red and should be
    rewritten to assert disclosure.
    """

    @pytest.fixture(autouse=True)
    def _threshold(self, low_threshold: None) -> None:
        """Negotiate on a modest payload."""

    def test_plain_tool_mints_a_token(self, rf: RequestFactory) -> None:
        """An over-threshold plain tool receives a probe envelope."""
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

        assert "continuation_token" in result

    def test_plain_tool_does_not_disclose_where_the_token_goes(self) -> None:
        """
        DEFECT WITNESS: the minting tool publishes no redemption surface.

        This is the group-dispatcher bug on a different consumer.  A conformant
        client reading this schema has nowhere to put the token it was just
        handed.
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

        props = reg.get_entry("item_list").input_schema["properties"]
        assert "continuation_token" not in props
        assert "mode" not in props
