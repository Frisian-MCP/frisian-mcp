"""
ADR-011 §4 — capability re-authorization through the **real request lifecycle**.

Why this file exists separately from ``test_adr011_redemption_reauthorization``:
that file calls ``_redemption_target_authorized()`` directly and hands it a
``_mcp_perm_entry_filter`` the test assigned itself.  That proves the helper can
refuse *given* the filter; it cannot prove the production request path ever
supplies one — and it did not.  The capability lens was inert on every real
redemption while three mutation-killed tests reported it covered.

**The rule this file enforces, and the reason it is a separate module:**

    A test that assigns ``request._mcp_*`` is testing the helper.
    Only a test that assigns *nothing* private is testing production.

``request.user`` is deliberately not in that category — authentication
middleware sets it, so a test that sets it is supplying a real input, not
fabricating internal state.

Host-agnostic throughout: synthetic ``catalog``/``item`` fixtures.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from frisian_mcp.backends.group_dispatcher import build_group_input_schema, make_group_invoke
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.views import McpView

_view = McpView.as_view()

_PAYLOAD = [{"name": f"item-{i}", "label": "x" * 120} for i in range(40)]
_THRESHOLD = 500


def _user(granted: set[str]) -> Any:
    """
    A user stub whose ``has_perm`` answers from *granted*, read live.

    ``granted`` is mutated by the tests to revoke a capability between the mint
    and the redemption, which is the whole scenario — so this must read the set
    on every call rather than snapshot it.
    """
    user = MagicMock()
    user.is_authenticated = True
    user.is_superuser = False
    user.is_active = True
    user.get_all_permissions = lambda: set(granted)
    user.has_perm = lambda perm, obj=None: perm in granted
    return user


def _registry() -> ToolRegistry:
    """A ``catalog`` group over one perm-carrying member."""
    reg = ToolRegistry()

    def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
        return _PAYLOAD

    reg.register(
        name="item_list",
        fn=_fn,
        description="flat item_list",
        input_schema={"type": "object", "properties": {}},
        permission_tier="read",
        perm_app_label="catalog",
        perm_model="item",
        perm_drf_action="list",
    )
    members = frozenset({"item_list"})
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", members, reg, frozenset({"item"})),
        description="Group dispatcher.",
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=members,
    )
    reg.set_hidden("item_list", True)
    return reg


def _call(rf: RequestFactory, user: Any, arguments: dict[str, Any]) -> Any:
    """One real JSON-RPC ``tools/call`` through ``McpView`` — nothing private set."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "catalog", "arguments": arguments},
    }
    req = rf.post("/mcp/", data=json.dumps(body), content_type="application/json")
    req.user = user
    # What ``django.test.Client`` sets on every request it issues: DRF's
    # SessionAuthentication enforces CSRF once ``request.user`` is authenticated,
    # which a RequestFactory request cannot satisfy.  This is the framework's own
    # test hook, NOT ``_mcp_*`` internal state — the distinction this module is
    # about is that the test must not fabricate the state under test.
    req._dont_enforce_csrf_checks = True  # pylint: disable=protected-access
    return _view(req)


def _result(response: Any) -> Any:
    if hasattr(response, "render") and not getattr(response, "is_rendered", True):
        response.render()
    payload = json.loads(response.content)
    if "result" not in payload:
        raise AssertionError(f"status={response.status_code} payload={payload}")
    return json.loads(payload["result"]["content"][0]["text"])


@pytest.fixture()
def perm_aware(settings: Any) -> None:
    """Permission-aware discovery on, and a threshold a modest payload crosses."""
    settings.FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
    settings.FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = _THRESHOLD


class TestCapabilityIsReEvaluatedAtRedemption:
    """
    Capability visibility is re-evaluated at redemption.

    §4 lists *"applicable capability and permission visibility"* among the
    dimensions redemption re-evaluates.  These assert it through the real
    lifecycle, which is the only place the claim can be true or false.
    """

    def test_revoking_the_capability_refuses_the_outstanding_token(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """
        Mint holding the capability, revoke it, redeem the same token.

        This is the finding: with the capability lens inert, the cached result
        is served to a caller who can no longer see the tool that produced it,
        until the TTL expires.
        """
        granted = {"catalog.view_item"}
        user = _user(granted)
        reg = _registry()

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, user, {"resource": "item", "action": "list", "params": {}}))
            token = probe["continuation_token"]
            cache.get.return_value = cache.set.call_args[0][1]

            granted.clear()  # revoked between the two requests

            served = _result(
                _call(
                    rf,
                    user,
                    {
                        "resource": "item",
                        "action": "list",
                        "continuation_token": token,
                        "mode": "full",
                    },
                )
            )

        assert "error" in served, (
            "a continuation was served after its capability was revoked — "
            "§4's capability dimension is not being evaluated on the real path"
        )

    def test_retaining_the_capability_still_serves(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """
        The control, and it matters as much as the refusal.

        A fix that refuses everything would pass the test above while breaking
        every legitimate redemption.
        """
        granted = {"catalog.view_item"}
        user = _user(granted)
        reg = _registry()

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, user, {"resource": "item", "action": "list", "params": {}}))
            cache.get.return_value = cache.set.call_args[0][1]

            served = _result(
                _call(
                    rf,
                    user,
                    {
                        "resource": "item",
                        "action": "list",
                        "continuation_token": probe["continuation_token"],
                        "mode": "full",
                    },
                )
            )

        assert served == _PAYLOAD


class TestPermissionContextIsPopulatedBeforeRedemption:
    """
    A direct guard on the ordering itself.

    The behavioural tests above would also fail for unrelated reasons; this one
    fails for exactly the reason H17 exists, so a future reordering cannot
    silently reintroduce it.
    """

    def test_the_filter_exists_when_redemption_authorizes(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """``_mcp_perm_entry_filter`` must be resolved before the continuation branch."""
        import frisian_mcp.views as views_mod  # pylint: disable=import-outside-toplevel

        granted = {"catalog.view_item"}
        user = _user(granted)
        reg = _registry()
        seen: dict[str, Any] = {}
        real = views_mod._redemption_target_authorized

        def _spy(request: Any, tool_name: str, target: str) -> bool:
            # ``ATTRIBUTE ABSENT`` is the H17 signature: not ``None`` (which
            # means "resolved, no filtering applies") but never computed.
            seen["filter"] = getattr(request, "_mcp_perm_entry_filter", "ATTRIBUTE ABSENT")
            return real(request, tool_name, target)

        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
            patch("frisian_mcp.views._redemption_target_authorized", _spy),
        ):
            cache.get.return_value = None
            probe = _result(_call(rf, user, {"resource": "item", "action": "list", "params": {}}))
            cache.get.return_value = cache.set.call_args[0][1]
            _call(
                rf,
                user,
                {
                    "resource": "item",
                    "action": "list",
                    "continuation_token": probe["continuation_token"],
                    "mode": "full",
                },
            )

        assert seen.get("filter") != "ATTRIBUTE ABSENT", (
            "the permission context had not been resolved when redemption "
            "re-authorized — the capability lens silently skips"
        )
        assert callable(seen["filter"]), (
            "a perm-aware request with a non-superuser must produce a callable "
            "entry filter, not None"
        )


def _class_registry() -> ToolRegistry:
    """A class dispatcher declaring its capability base, with two actions."""
    from frisian_mcp.decorators import mcp_action, mcp_dispatcher

    reg = ToolRegistry()

    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_dispatcher(
            name="catalog_cls", description="Class dispatcher.", capability="catalog.item"
        )
        class _Cls:
            @mcp_action("list", description="list items", params={})
            def list(self, _params: dict[str, Any], _request: Any) -> Any:
                return _PAYLOAD

    return reg


def _call_named(rf: RequestFactory, user: Any, name: str, arguments: dict[str, Any]) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    req = rf.post("/mcp/", data=json.dumps(body), content_type="application/json")
    req.user = user
    req._dont_enforce_csrf_checks = True  # pylint: disable=protected-access
    return _view(req)


class TestClassDispatcherActionIsReAuthorized:
    """
    §4 for the shape that resolves no child.

    A class dispatcher registers as ``read`` so it stays visible as a
    navigation entry-point, and its real authorization lives in the action
    lens.  Re-authorizing the dispatcher itself therefore passes trivially —
    the vacuous re-check §5 exists to prevent, one shape over.
    """

    def test_the_dispatched_action_is_recorded_at_mint(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """Without this the redemption check has only the outer name to work with."""
        user = _user({"catalog.view_item"})
        reg = _class_registry()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            _call_named(rf, user, "catalog_cls", {"action": "list", "params": {}})
            entry = cache.set.call_args[0][1]

        assert entry["resolved_action"] == "list"
        # The outer name is still what containment and the owner key bind.
        assert entry["resolved_target"] == "catalog_cls"

    def test_revoking_the_capability_refuses_the_class_dispatcher_token(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """Mint holding the capability, revoke, redeem — must refuse."""
        granted = {"catalog.view_item"}
        user = _user(granted)
        reg = _class_registry()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call_named(rf, user, "catalog_cls", {"action": "list", "params": {}}))
            cache.get.return_value = cache.set.call_args[0][1]

            granted.clear()

            served = _result(
                _call_named(
                    rf,
                    user,
                    "catalog_cls",
                    {
                        "action": "list",
                        "continuation_token": probe["continuation_token"],
                        "mode": "full",
                    },
                )
            )

        assert "error" in served

    def test_retaining_the_capability_still_serves(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """The control — the action lens must not refuse a caller who still holds it."""
        user = _user({"catalog.view_item"})
        reg = _class_registry()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call_named(rf, user, "catalog_cls", {"action": "list", "params": {}}))
            cache.get.return_value = cache.set.call_args[0][1]
            served = _result(
                _call_named(
                    rf,
                    user,
                    "catalog_cls",
                    {
                        "action": "list",
                        "continuation_token": probe["continuation_token"],
                        "mode": "full",
                    },
                )
            )

        assert served == _PAYLOAD

    def test_a_forged_action_in_the_redemption_call_is_ignored(
        self, rf: RequestFactory, perm_aware: None
    ) -> None:
        """
        The action re-checked is the one recorded at mint, never this call's.

        Reading it from the redemption arguments would let a caller name an
        action they *can* still see in order to release a payload produced by
        one they cannot.
        """
        granted = {"catalog.view_item"}
        user = _user(granted)
        reg = _class_registry()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.views.django_cache") as cache,
        ):
            cache.get.return_value = None
            probe = _result(_call_named(rf, user, "catalog_cls", {"action": "list", "params": {}}))
            entry = cache.set.call_args[0][1]
            entry["resolved_action"] = "nonexistent_action"  # what the server recorded
            cache.get.return_value = entry

            served = _result(
                _call_named(
                    rf,
                    user,
                    "catalog_cls",
                    {
                        "action": "list",  # caller claims an action they can still see
                        "continuation_token": probe["continuation_token"],
                        "mode": "full",
                    },
                )
            )

        assert "error" in served
