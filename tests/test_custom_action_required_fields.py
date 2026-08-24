"""
CL-14 — a custom detail action must not inherit the parent serializer's required fields.

``get_input_schema`` introspects the serializer for any action whose HTTP
mapping includes a body-carrying method.  A custom ``@action(detail=True,
methods=["post"])`` satisfies that, so ``_schema_from_viewset`` runs and its
``required`` list is merged into the action's schema — but that helper resolves
the **ViewSet's** serializer, the parent model's, not whatever the custom
action actually consumes.

So a sub-endpoint that wants nothing but an id is registered as requiring the
parent's whole create contract.

**This is a runtime defect, not only a disclosure one, and it predates the
disclosure that surfaced it.**  ``ToolRegistry.dispatch`` validates against this
same schema, so calling such an action with only an ``id`` has always failed,
demanding fields it never uses.

The rule applied here: a custom action carries inherited ``required`` fields
only when the ViewSet actually selects a different serializer for it — DRF's own
per-action mechanism, which ``_schema_from_viewset`` already drives by setting
``viewset.action``.  When the ViewSet returns the same serializer it returns for
``create``, nothing has told us what the custom action consumes, so no required
fields are inherited.  **An empty list is honest; a wrong one is not.**
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from frisian_mcp.backends.discovery import DRFSyncDiscovery

# ---------------------------------------------------------------------------
# Fixtures — a parent serializer with a real create contract, and a sub-endpoint
# ---------------------------------------------------------------------------


class _ParentSerializer(serializers.Serializer):
    """The resource's own serializer: several genuinely required fields."""

    name = serializers.CharField(required=True)
    origin = serializers.CharField(required=True)
    contact = serializers.CharField(required=True)
    comment = serializers.CharField(required=False)


class _AttachmentSerializer(serializers.Serializer):
    """What a sub-endpoint actually consumes — nothing like the parent."""

    body = serializers.CharField(required=True)


class _FixedSerializerViewSet(ViewSet):
    """
    The common shape: one serializer for everything.

    A ViewSet like this has said nothing about what its custom action consumes,
    so nothing may be inferred about it.
    """

    serializer_class = _ParentSerializer

    def get_serializer_class(self) -> type[serializers.Serializer]:
        """Return the one serializer this ViewSet declares."""
        return _ParentSerializer

    def create(self, request: Request) -> Response:
        """Standard create — legitimately takes the parent serializer."""
        return Response({}, status=201)

    def update(self, request: Request, pk: str | None = None) -> Response:
        """Standard update — also legitimately the parent serializer."""
        return Response({})

    @action(detail=True, methods=["post"])
    def annotate(self, request: Request, pk: str | None = None) -> Response:
        """A custom body-carrying detail action that wants only an id."""
        return Response({})


class _PerActionSerializerViewSet(ViewSet):
    """A ViewSet that *does* select a serializer per action, as DRF allows."""

    serializer_class = _ParentSerializer

    def get_serializer_class(self) -> type[serializers.Serializer]:
        """Return a different serializer for the custom action."""
        if getattr(self, "action", None) == "annotate":
            return _AttachmentSerializer
        return _ParentSerializer

    def create(self, request: Request) -> Response:
        """Standard create."""
        return Response({}, status=201)

    @action(detail=True, methods=["post"])
    def annotate(self, request: Request, pk: str | None = None) -> Response:
        """A custom action with a serializer of its own."""
        return Response({})


def _schema(view_class: type, action_name: str) -> dict[str, Any]:
    return DRFSyncDiscovery().get_input_schema(view_class, action_name)


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


class TestCustomActionDoesNotInheritParentRequired:
    """A custom action must not demand fields it never consumes."""

    def test_custom_action_requires_only_the_id(self) -> None:
        """
        The reported shape: ``required`` was the parent's create contract + id.

        This is the assertion that fails before the fix.
        """
        required = _schema(_FixedSerializerViewSet, "annotate").get("required", [])

        assert sorted(required) == ["id"], (
            "the custom action inherited the parent serializer's required fields; "
            f"got {sorted(required)}"
        )

    def test_the_parents_create_contract_is_not_disclosed_on_the_custom_action(
        self,
    ) -> None:
        """
        Stated as absence of the specific field names, not as a count.

        The live report was that a read-tier caller could read the resource's
        whole create contract off a read-ish action.  Naming the fields is what
        makes this fail for the right reason.
        """
        required = _schema(_FixedSerializerViewSet, "annotate").get("required", [])

        for field in ("name", "origin", "contact"):
            assert field not in required, f"{field!r} leaked onto the custom action"

    def test_action_with_its_own_serializer_keeps_its_required_fields(self) -> None:
        """
        The confident case: the ViewSet said what this action consumes.

        DRF exposes per-action serializer selection and ``_schema_from_viewset``
        already drives it, so when a host uses it we trust the answer.  Without
        this cell the fix could be "never inherit anything", which would throw
        away correct information.
        """
        required = _schema(_PerActionSerializerViewSet, "annotate").get("required", [])

        assert "body" in required, "an action-specific serializer's contract was discarded"
        assert "name" not in required


class TestStandardWriteActionsAreUnchanged:
    """
    The regression risk: ``create``/``update`` legitimately take the parent.

    Asserted on both ViewSets so the fix cannot narrow the standard write path
    while fixing the custom one.
    """

    def test_create_still_requires_the_parent_contract(self) -> None:
        """Create is the action the parent serializer is *for*."""
        required = _schema(_FixedSerializerViewSet, "create").get("required", [])

        assert set(required) >= {"name", "origin", "contact"}

    def test_update_still_requires_the_parent_contract(self) -> None:
        """Update is a detail action and also takes the parent serializer."""
        required = _schema(_FixedSerializerViewSet, "update").get("required", [])

        assert set(required) >= {"name", "origin", "contact"}
        assert "id" in required

    def test_create_unchanged_on_the_per_action_viewset(self) -> None:
        """Per-action selection must not disturb the standard actions."""
        required = _schema(_PerActionSerializerViewSet, "create").get("required", [])

        assert set(required) >= {"name", "origin", "contact"}

    def test_partial_update_still_requires_nothing(self) -> None:
        """PATCH makes every body field optional; unchanged by this fix."""
        schema = _schema(_FixedSerializerViewSet, "partial_update")

        assert "required" not in schema
