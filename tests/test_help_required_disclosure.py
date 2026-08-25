"""CL-8 / GH #65 defect B -- ``help`` discloses what an action REQUIRES.

Before this, ``help`` published *which actions exist* and let a host hand-write
prose about them, but never derived *what an action needs* from the schema it
was about to validate against.  A host could document a required field; the
package could not tell you one.  Combined with defect A (a validation failure
that does not name the missing field) a schema-blind client had no legal path to
a create: told a field was missing, not told which, and unable to look it up.

Two properties are load-bearing here and each has its own test:

1. **The disclosure is derived, not restated.**  ``help`` reads the registered
   ``input_schema`` -- the same object ``ToolRegistry.dispatch`` validates
   against.  A second, independent derivation could disagree with the
   validator, and "help said one thing, validation wanted another" is worse
   than silence.
2. **The disclosure is filtered exactly like the action list.**  Required-field
   names for an action the caller may not see would be a disclosure leak
   wearing a usability hat.

Host-agnostic throughout: fixture resources and field names are the package's
own, per the standing constraint.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import json
from typing import Any

import pytest

from frisian_mcp.backends.group_dispatcher import build_group_help
from frisian_mcp.backends.invocation import _LIST_BODY_KEYS
from frisian_mcp.registry import _BULK_LIST_BODY_KEYS, ToolRegistry

_GROUP = "catalog"
_TOOLS = [
    "item_list",
    "item_create",
    "item_bulk_create",
    "container_list",
]


def _stub(name: str) -> Any:
    def _fn(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        return {"called": name, "arguments": arguments}

    return _fn


def _schema(required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    if required:
        schema["required"] = list(required)
    return schema


@pytest.fixture()
def registry() -> ToolRegistry:
    """A group whose write actions declare required fields and whose reads do not."""
    reg = ToolRegistry()
    specs: dict[str, tuple[list[str] | None, str]] = {
        # name: (required fields, tier)
        "item_list": (None, "read"),
        "item_create": (["label", "container"], "read_write"),
        "item_bulk_create": (["label"], "read_write"),
        "container_list": (None, "read"),
    }
    for name, (required, tier) in specs.items():
        reg.register(name, _stub(name), "stub", _schema(required), permission_tier=tier)
    return reg


def _help(registry: ToolRegistry, **kwargs: Any) -> dict[str, Any]:
    return build_group_help(_GROUP, list(_TOOLS), registry, resource="item", **kwargs)


class TestRequiredFieldsAreDisclosed:
    """The defect itself: ``help`` now names what an action needs."""

    def test_required_fields_are_named(self, registry: ToolRegistry) -> None:
        """The exact failure GH #65 describes: a create whose fields are knowable."""
        payload = _help(registry)
        assert payload["requires"]["create"] == ["container", "label"]

    def test_actions_without_required_fields_are_omitted(self, registry: ToolRegistry) -> None:
        """
        An absent key already means "nothing required" -- do not pay for padding.

        ``help`` is opt-in but it is not free, and a map of every action to an
        empty list is bytes that carry no information.
        """
        payload = _help(registry)
        assert "list" not in payload["requires"]

    def test_disclosure_tracks_the_registered_schema_not_a_restatement(self) -> None:
        """
        THE load-bearing property: derived from the validator's own artifact.

        Re-registering the same action with different required fields must move
        the disclosure with it.  If this test can be made to pass by a hardcoded
        list somewhere, the disclosure has become a second source that can drift
        from what ``dispatch`` actually enforces.
        """
        reg = ToolRegistry()
        reg.register(
            "item_create", _stub("c"), "stub", _schema(["alpha"]), permission_tier="read_write"
        )
        assert build_group_help(_GROUP, ["item_create"], reg, resource="item")["requires"] == {
            "create": ["alpha"]
        }

        reg2 = ToolRegistry()
        reg2.register(
            "item_create",
            _stub("c"),
            "stub",
            _schema(["beta", "gamma"]),
            permission_tier="read_write",
        )
        assert build_group_help(_GROUP, ["item_create"], reg2, resource="item")["requires"] == {
            "create": ["beta", "gamma"]
        }


class TestDisclosureIsFilteredLikeTheActionList:
    """A required-field list is still a disclosure, and obeys the same gates."""

    def test_hidden_action_leaks_no_required_fields(self, registry: ToolRegistry) -> None:
        """
        A read-tier caller must not learn the shape of a write it cannot call.

        The action list already hides ``create`` at this tier; if ``requires``
        were built from the unfiltered tool list instead of the visible actions,
        it would hand back exactly the field names the tier gate is withholding.
        """
        payload = _help(registry, max_tier="read")
        assert "create" not in payload["actions"]
        assert "create" not in payload.get("requires", {})
        assert "bulk_create" not in payload.get("requires", {})

    def test_entry_filter_is_honoured_too(self, registry: ToolRegistry) -> None:
        """Permission-aware discovery hides the fields along with the action."""
        payload = _help(registry, entry_filter=lambda e: not e.name.endswith("_create"))
        assert payload.get("requires", {}) == {}


class TestFactAndOpinionStaySeparable:
    """A host hint is an opinion; a derived required list is a fact."""

    def test_hints_and_requires_are_distinct_keys(self, registry: ToolRegistry) -> None:
        """Both channels coexist, and neither is mistakable for the other."""
        payload = _help(registry, hints={"item_create": "Create a container first."})
        assert payload["hints"] == {"item_create": "Create a container first."}
        assert payload["requires"]["create"] == ["container", "label"]
        # A reader can tell which is which without knowing a convention.
        assert payload["hints"] != payload["requires"]


class TestBulkParamsAreDiscoverable:
    """``bulk_*`` was advertised on every group and effectively uncallable."""

    def test_wrapper_keys_are_named(self, registry: ToolRegistry) -> None:
        """
        The wrapper key is named, so a bulk call is reachable.

        ``params`` is typed ``object``, so the natural array form is rejected
        before the host is reached.  Nothing named the wrapper key until now.
        """
        payload = _help(registry)
        assert payload["bulk_params"]["wrap_list_in_one_of"] == sorted(_LIST_BODY_KEYS)
        assert "objects" in payload["bulk_params"]["example"]

    def test_absent_when_the_resource_has_no_bulk_action(self, registry: ToolRegistry) -> None:
        """Do not describe a convention this resource cannot use."""
        payload = build_group_help(_GROUP, list(_TOOLS), registry, resource="container")
        assert "bulk_params" not in payload

    def test_absent_when_the_bulk_action_is_hidden(self, registry: ToolRegistry) -> None:
        """Do not advertise a calling convention for something you are hiding."""
        payload = _help(registry, max_tier="read")
        assert "bulk_params" not in payload

    def test_disclosed_keys_match_the_set_the_request_path_uses(self) -> None:
        """
        DRIFT GUARD -- the accepted set is declared TWICE, in two modules.

        ``registry._BULK_LIST_BODY_KEYS`` decides whether required-field
        validation is skipped; ``invocation._LIST_BODY_KEYS`` decides whether
        the list is unwrapped into the JSON array body.  They are joined only by
        a comment.  Divergence breaks both ways: a key in the registry set alone
        skips validation but is never unwrapped, so the host serializer receives
        ``{"key": [...]}`` and rejects it; a key in the invocation set alone is
        validated as a single object and rejected before the host is reached.

        ``help`` now publishes one of them as fact, so a drift would make the
        package authoritative and wrong.  This is the cheapest possible pin.
        """
        assert _LIST_BODY_KEYS == _BULK_LIST_BODY_KEYS


class TestFullGroupViewIsUnchanged:
    """Scope decision, recorded as a test rather than a comment."""

    def test_group_view_carries_no_per_action_disclosure(self, registry: ToolRegistry) -> None:
        """
        Disclosure is scoped to the RESOURCE view deliberately.

        The full-group view lists every resource in the group; attaching
        required fields and bulk shapes to each would multiply the one payload
        that is already the largest, to answer a question the caller has not
        asked yet.  The resource view is the one on the path to a create.

        If this is ever widened it should be a ruling with a measurement, not a
        drive-by -- hence a test that fails loudly rather than a comment nobody
        reads.
        """
        payload = build_group_help(_GROUP, list(_TOOLS), registry)
        assert "requires" not in payload
        assert "bulk_params" not in payload
        assert set(payload["resources"]) == {"item", "container"}


class TestDisclosureCost:
    """``help`` is opt-in, but measure it anyway rather than assuming."""

    def test_resource_help_stays_small(self, registry: ToolRegistry) -> None:
        """
        A disclosed help body stays cheap enough to be worth asking for.

        The measured cost of NOT disclosing was four blind round-trips.  This
        bounds the replacement so a future addition cannot quietly turn an
        opt-in help body into a payload of its own.
        """
        payload = _help(registry, hints={"item_create": "Create a container first."})
        assert len(json.dumps(payload)) < 600
