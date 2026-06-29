"""
PKG-17 — write-action invocation envelope hygiene.

Reproduces the two write-action findings against in-process DRF ViewSets so
the bugs are pinned without needing a live host application:

* ``destroy`` returning the HTTP 204 status as a clean ``{"deleted": True}``
  envelope rather than ``None`` (which upstream wraps as ``{"error": ""}``).
* ``update`` / ``partial_update`` actually persisting body data and returning
  the post-save serialised object as ``isError=false`` content.
"""

# pylint: disable=redefined-outer-name,abstract-method,protected-access
from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest
from django.test import RequestFactory
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from frisian_mcp.backends.base import ToolDefinition
from frisian_mcp.backends.invocation import SyncInvocation

# ---------------------------------------------------------------------------
# In-memory store + ViewSet
# ---------------------------------------------------------------------------


class _ItemSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Minimal write serializer for the store."""

    name = serializers.CharField()
    value = serializers.CharField(required=False, allow_blank=True)


class _DetailActionViewSet(ViewSet):
    """ViewSet with a custom object-scoped action."""

    @action(detail=True, methods=["post"])
    def napalm(self, request: Any, pk: str) -> Response:
        """Echo the object id passed through DRF's detail-action kwarg."""
        return Response({"pk": pk, "payload": request.data})


class _StoreViewSet(ViewSet):
    """In-memory ViewSet exercising create/update/partial_update/destroy."""

    store: dict[str, dict[str, Any]] = {}  # noqa: RUF012

    def get_serializer(self, *args: Any, **kwargs: Any) -> _ItemSerializer:
        """Return the item serializer configured with passed args/kwargs."""
        return _ItemSerializer(*args, **kwargs)

    def list(self, request: Any) -> Response:
        """List all items."""
        return Response(list(self.store.values()))

    def create(self, request: Any) -> Response:
        """Create an item from the request body."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item_id = str(len(self.store) + 1)
        record = {"id": item_id, **serializer.validated_data}
        self.store[item_id] = record
        return Response(record, status=status.HTTP_201_CREATED)

    def update(self, request: Any, pk: str | None = None) -> Response:
        """Replace an item."""
        if pk is None or pk not in self.store:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = {"id": pk, **serializer.validated_data}
        self.store[pk] = record
        return Response(record)

    def partial_update(self, request: Any, pk: str | None = None) -> Response:
        """Patch an item."""
        if pk is None or pk not in self.store:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.store[pk].update(serializer.validated_data)
        return Response(self.store[pk])

    def destroy(self, request: Any, pk: str | None = None) -> Response:
        """Delete an item."""
        if pk is None or pk not in self.store:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        del self.store[pk]
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()


@pytest.fixture()
def store_request(rf: RequestFactory) -> Any:
    """Reset the in-memory store and return a base anon request."""
    _StoreViewSet.store.clear()
    req = rf.post("/mcp/", content_type="application/json")
    req.user = None  # type: ignore[attr-defined]
    req.auth = None  # type: ignore[attr-defined]
    return req


def _tool(action: str) -> ToolDefinition:
    """Return a ToolDefinition for the given _StoreViewSet action."""
    return ToolDefinition(
        name=f"item.{action}",
        description="stub",
        input_schema={"type": "object"},
        permission_classes=(),
        source="auto",
        view_class=_StoreViewSet,
        action=action,
    )


def _detail_tool() -> ToolDefinition:
    """Return a ToolDefinition for a custom @action(detail=True) handler."""
    return ToolDefinition(
        name="device.napalm",
        description="stub",
        input_schema={"type": "object"},
        permission_classes=(),
        source="auto",
        view_class=_DetailActionViewSet,
        action="napalm",
    )


# ---------------------------------------------------------------------------
# Custom detail actions — id/pk extraction
# ---------------------------------------------------------------------------


class TestCustomDetailActionInvocation:
    """Regression coverage for issue #12."""

    def test_custom_detail_action_receives_id_as_pk(self, store_request: Any) -> None:
        """@action(detail=True) receives MCP id as DRF pk kwarg."""
        result = SyncInvocation().invoke(
            _detail_tool(),
            {"id": "device-1", "command": "show version"},
            store_request,
        )

        assert result.is_error is False
        assert result.content == {"pk": "device-1", "payload": {"command": "show version"}}

    def test_custom_detail_action_receives_pk_as_pk(self, store_request: Any) -> None:
        """@action(detail=True) also accepts the MCP pk alias."""
        result = SyncInvocation().invoke(
            _detail_tool(),
            {"pk": "device-1", "command": "show version"},
            store_request,
        )

        assert result.is_error is False
        assert result.content == {"pk": "device-1", "payload": {"command": "show version"}}

    def test_custom_detail_action_missing_id_is_clean_error(self, store_request: Any) -> None:
        """Missing id returns a structured error before Python raises TypeError."""
        result = SyncInvocation().invoke(_detail_tool(), {"command": "show version"}, store_request)

        assert result.is_error is True
        assert result.content == {"error": "Missing required argument: id"}


# ---------------------------------------------------------------------------
# Bug 2 — destroy 204 returns a clean envelope
# ---------------------------------------------------------------------------


class TestDestroyEnvelope:
    """A successful destroy returns a structured envelope, never empty/None."""

    def test_destroy_returns_deleted_envelope(self, store_request: Any) -> None:
        """204 No Content surfaces as {deleted: true, status: 204}."""
        invocation = SyncInvocation()
        invocation.invoke(_tool("create"), {"name": "x"}, store_request)

        result = invocation.invoke(_tool("destroy"), {"id": "1"}, store_request)

        assert result.is_error is False
        assert result.content == {"deleted": True, "status": 204}

    def test_destroy_missing_id_surfaces_404_body(self, store_request: Any) -> None:
        """A destroy that 404s returns the response body, not an empty envelope."""
        invocation = SyncInvocation()
        result = invocation.invoke(_tool("destroy"), {"id": "999"}, store_request)
        # The action returns Response(404) with body — _extract_data yields the body.
        assert result.is_error is False
        assert result.content == {"detail": "not found"}


# ---------------------------------------------------------------------------
# Bug 1 — update / partial_update actually apply body data
# ---------------------------------------------------------------------------


class TestUpdatePersists:
    """Update + partial_update round-trip body changes through the synthetic request."""

    def test_full_update_replaces_record(self, store_request: Any) -> None:
        """PUT body reaches the serializer and the response shows the new state."""
        invocation = SyncInvocation()
        invocation.invoke(_tool("create"), {"name": "before", "value": "v1"}, store_request)

        result = invocation.invoke(
            _tool("update"),
            {"id": "1", "name": "after", "value": "v2"},
            store_request,
        )

        assert result.is_error is False
        assert result.content["name"] == "after"
        assert result.content["value"] == "v2"
        # Confirm persisted in the store, not just echoed.
        assert _StoreViewSet.store["1"]["name"] == "after"

    def test_partial_update_merges(self, store_request: Any) -> None:
        """PATCH body reaches the serializer with partial=True; only changed fields update."""
        invocation = SyncInvocation()
        invocation.invoke(_tool("create"), {"name": "before", "value": "v1"}, store_request)

        result = invocation.invoke(
            _tool("partial_update"),
            {"id": "1", "value": "v2"},  # name omitted
            store_request,
        )

        assert result.is_error is False
        assert result.content["name"] == "before"  # unchanged
        assert result.content["value"] == "v2"  # patched
        assert _StoreViewSet.store["1"]["value"] == "v2"


# ---------------------------------------------------------------------------
# _extract_data direct unit tests
# ---------------------------------------------------------------------------


class TestExtractData:
    """Focused probes against _extract_data() for each response shape."""

    def test_204_response_yields_deleted_envelope(self) -> None:
        """A bare 204 Response (no data) returns the structured envelope and status 204."""
        data, http_status = SyncInvocation._extract_data(Response(status=204))
        assert data == {"deleted": True, "status": 204}
        assert http_status == 204

    def test_response_with_data_returned_verbatim(self) -> None:
        """A 200 Response with data passes through unchanged."""
        resp = Response({"id": 1, "name": "x"}, status=200)
        data, http_status = SyncInvocation._extract_data(resp)
        assert data == {"id": 1, "name": "x"}
        assert http_status == 200

    def test_response_data_none_returns_status_envelope(self) -> None:
        """A non-204 Response with data=None returns {status: <code>} not None."""
        data, http_status = SyncInvocation._extract_data(Response(status=202))
        assert data == {"status": 202}
        assert http_status == 202

    def test_201_response_status_threaded_through(self) -> None:
        """A 201 Created response carries http_status=201 in the tuple."""
        data, http_status = SyncInvocation._extract_data(
            Response({"id": "new-uuid", "name": "x"}, status=201)
        )
        assert data == {"id": "new-uuid", "name": "x"}
        assert http_status == 201


# ---------------------------------------------------------------------------
# PKG-19 — JSONRenderer normalises DRF-native types (UUID/datetime/Decimal)
# ---------------------------------------------------------------------------


class TestJsonRendererNormalisation:
    """
    response.data must be JSON-safe by the time it leaves _extract_data.

    Generalises GAP-NAUTO-G: any DRF host app whose serializer returns
    ``uuid.UUID``, ``datetime``, ``Decimal``, or ``OrderedDict`` subclasses
    would otherwise crash the upstream stdlib ``json.dumps`` encoder.
    """

    def test_uuid_in_response_data_becomes_string(self) -> None:
        """A bare uuid.UUID in response.data renders to its canonical string form."""
        from uuid import UUID  # pylint: disable=import-outside-toplevel

        uid = UUID("11d1d2c3-4444-5555-6666-7777aaaabbbb")
        resp = Response({"id": uid, "name": "x"}, status=200)

        content, _ = SyncInvocation._extract_data(resp)
        assert content == {"id": str(uid), "name": "x"}
        # Critical: stdlib json.dumps must not raise TypeError.
        import json as _json  # pylint: disable=import-outside-toplevel

        _json.dumps(content)

    def test_datetime_in_response_data_becomes_iso_string(self) -> None:
        """A datetime in response.data renders to ISO-8601."""
        from datetime import datetime  # pylint: disable=import-outside-toplevel

        ts = datetime(2026, 5, 4, 12, 34, 56, tzinfo=UTC)
        resp = Response({"created": ts}, status=200)

        content, _ = SyncInvocation._extract_data(resp)
        assert isinstance(content["created"], str)
        assert "2026-05-04" in content["created"]

    def test_decimal_in_response_data_becomes_string_or_number(self) -> None:
        """A Decimal in response.data renders without raising TypeError."""
        from decimal import Decimal  # pylint: disable=import-outside-toplevel

        resp = Response({"amount": Decimal("19.95")}, status=200)

        content, _ = SyncInvocation._extract_data(resp)
        # DRF's JSONRenderer emits Decimals as JSON numbers (or strings if
        # COERCE_DECIMAL_TO_STRING is set in settings).  Either is fine — the
        # key requirement is no TypeError on stdlib re-encode.
        assert "amount" in content
        import json as _json  # pylint: disable=import-outside-toplevel

        _json.dumps(content)

    def test_ordereddict_subclass_in_response_data_normalises(self) -> None:
        """A nested OrderedDict (DRF serializer output convention) survives the round-trip."""
        from collections import OrderedDict  # pylint: disable=import-outside-toplevel
        from uuid import UUID  # pylint: disable=import-outside-toplevel

        nested = OrderedDict([("id", UUID("11d1d2c3-4444-5555-6666-7777aaaabbbb")), ("name", "y")])
        resp = Response({"nested": nested}, status=200)

        content, _ = SyncInvocation._extract_data(resp)
        assert content["nested"]["id"] == "11d1d2c3-4444-5555-6666-7777aaaabbbb"
        assert content["nested"]["name"] == "y"

    def test_plain_primitives_unchanged(self) -> None:
        """Pure-primitive response.data passes through unchanged (regression check)."""
        resp = Response({"a": 1, "b": "two", "c": [3, 4]}, status=200)
        data, http_status = SyncInvocation._extract_data(resp)
        assert data == {"a": 1, "b": "two", "c": [3, 4]}
        assert http_status == 200

    def test_unrenderable_payload_surfaces_as_tool_error_not_500(self, rf: RequestFactory) -> None:
        """When JSONRenderer crashes, invoke() returns is_error=True (no 500 propagates)."""

        class _UnrenderableViewSet(ViewSet):
            def list(self, request: Any) -> Response:
                """Return a Response with a payload JSONRenderer cannot serialise."""

                # An object instance is not natively JSON-encodable and DRF's
                # encoder raises TypeError.  This simulates an unrendrable
                # custom field in a real serializer.
                class _Boom:
                    def __repr__(self) -> str:
                        return "<Boom>"

                return Response({"obj": _Boom()}, status=200)

        tool = ToolDefinition(
            name="boom.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_UnrenderableViewSet,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)
        assert result.is_error is True
        assert "Failed to serialise response" in str(result.content.get("error", ""))


# ---------------------------------------------------------------------------
# PKG-20 — viewset.initial() must run so host-app queryset scoping fires
# ---------------------------------------------------------------------------


class TestInitialLifecycleHook:
    """
    SyncInvocation must invoke viewset.initial() before the action handler.

    Without this, host-app code that lives in ``initial()`` — per-user
    queryset scoping, tenancy filters, RBAC overlays, ``request.version``
    setup, throttles — is silently bypassed.  This was a P0 cross-resource
    data leak surfaced during integration testing.
    """

    def test_initial_is_called_before_action(self, rf: RequestFactory) -> None:
        """A viewset whose initial() filters the data sees the filter applied."""

        class _ScopedViewSet(ViewSet):
            """Mirror the canonical pattern: scope data inside initial()."""

            calls: dict[str, int] = {"initial": 0, "list": 0}  # noqa: RUF012

            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Stamp self.scoped_rows so list() returns only those rows."""
                self.calls["initial"] += 1
                # Simulate restrict_queryset(): narrow self.scoped_rows by user.
                # Defining a per-request attribute in initial() is the
                # canonical DRF pattern (cf. APIView.initial); not __init__.
                self.scoped_rows = [  # pylint: disable=attribute-defined-outside-init
                    {"id": 1, "public": True}
                ]

            def list(self, request: Any) -> Response:
                """Return only the rows initial() left in scope."""
                self.calls["list"] += 1
                return Response(getattr(self, "scoped_rows", [{"id": 1}, {"id": 2}]))

        tool = ToolDefinition(
            name="scoped.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_ScopedViewSet,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        # Reset class-level call counters in case earlier runs incremented them.
        _ScopedViewSet.calls = {"initial": 0, "list": 0}

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is False
        assert _ScopedViewSet.calls["initial"] == 1, "viewset.initial() was not called"
        assert _ScopedViewSet.calls["list"] == 1
        # The action saw only the row initial() left in scope.
        assert result.content == [{"id": 1, "public": True}]

    def test_permission_denied_in_initial_surfaces_as_tool_error(self, rf: RequestFactory) -> None:
        """A PermissionDenied raised in initial() becomes is_error=True, not a 500."""
        from rest_framework.exceptions import (  # pylint: disable=import-outside-toplevel
            PermissionDenied,
        )

        class _DeniedViewSet(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Always deny — simulates an RBAC gate in a host-app override."""
                raise PermissionDenied("user lacks permission")

            def list(self, request: Any) -> Response:
                """Should never be reached when initial() denies."""
                return Response([{"never": "reached"}])

        tool = ToolDefinition(
            name="denied.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_DeniedViewSet,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is True
        assert "user lacks permission" in str(result.content.get("error", ""))

    def test_initial_validation_error_re_raises(self, rf: RequestFactory) -> None:
        """A ValidationError in initial() bubbles up like one from the action."""
        from rest_framework.exceptions import (  # pylint: disable=import-outside-toplevel
            ValidationError,
        )

        class _ValidatesInInitialViewSet(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Run cross-cutting validation; raise ValidationError on rejection."""
                raise ValidationError({"version": ["unsupported"]})

            def list(self, request: Any) -> Response:
                """Unreachable for this scenario."""
                return Response([])

        tool = ToolDefinition(
            name="vinit.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_ValidatesInInitialViewSet,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        with pytest.raises(ValidationError):
            SyncInvocation().invoke(tool, {}, outer)

    def test_existing_create_path_still_works_with_initial(self, store_request: Any) -> None:
        """Regression: the standard ViewSet create path keeps working with initial() invoked."""
        invocation = SyncInvocation()
        result = invocation.invoke(_tool("create"), {"name": "x"}, store_request)
        assert result.is_error is False
        assert result.content["name"] == "x"


# ---------------------------------------------------------------------------
# Error-envelope hygiene — DRF APIException.detail must not nest as string
# ---------------------------------------------------------------------------


class TestErrorEnvelopeHygiene:
    """
    ToolResult.content['error'] is a flat string, never a stringified dict.

    The PKG-13 follow-up reported envelopes like::

        {"error": "{'error': 'You do not have permission...'}"}

    That string-in-string nesting comes from ``str()``-ing a DRF APIException
    whose ``.detail`` is itself a dict.  The fix unwraps ``.detail`` via
    :func:`frisian_mcp.backends.invocation._flatten_error_detail` so the
    envelope carries a single flat human-readable string.
    """

    def test_permission_denied_with_string_detail_unwraps_cleanly(self, rf: RequestFactory) -> None:
        """PermissionDenied with a plain-string detail produces a flat envelope."""
        from rest_framework.exceptions import (  # pylint: disable=import-outside-toplevel
            PermissionDenied,
        )

        class _DenyPlain(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Raise with a string detail — most common DRF shape."""
                raise PermissionDenied("You do not have permission")

            def list(self, request: Any) -> Response:
                """Unreachable for this scenario."""
                return Response([])

        tool = ToolDefinition(
            name="deny.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_DenyPlain,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is True
        message = result.content["error"]
        # Critical: the envelope is a flat string, NOT a stringified dict.
        assert isinstance(message, str)
        assert "You do not have permission" in message
        # No nested wrapper artefacts — no leading "{'error':", no "ErrorDetail(".
        assert not message.startswith("{")
        assert "ErrorDetail" not in message

    def test_permission_denied_with_dict_detail_unwraps_wrapper_keys(
        self, rf: RequestFactory
    ) -> None:
        r"""
        A dict-form detail like {'error': '...'} unwraps to the inner string.

        Reproduces the exact PKG-13 follow-up symptom: a host APIException
        whose ``.detail`` was a dict produced
        ``{'error': \"{'error': 'You do not have...'}\"}`` envelopes.
        """
        from rest_framework.exceptions import (  # pylint: disable=import-outside-toplevel
            PermissionDenied,
        )

        class _DenyDict(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Raise with a dict detail — mirrors a wrapped-detail host pattern."""
                raise PermissionDenied({"error": "You do not have permission"})

            def list(self, request: Any) -> Response:
                """Unreachable."""
                return Response([])

        tool = ToolDefinition(
            name="dictdeny.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_DenyDict,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is True
        message = result.content["error"]
        # The wrapper key 'error' is suppressed; only the inner string remains.
        assert message == "You do not have permission"
        # Specifically NOT the previous nested form.
        assert message != "{'error': 'You do not have permission'}"

    def test_validation_dict_detail_keeps_field_prefix(self, rf: RequestFactory) -> None:
        """
        Real field names (not 'error'/'detail') stay prefixed for clarity.

        We only suppress wrapper artefacts — a genuine field-validation dict
        like ``{'name': ['required']}`` should still render with the field
        name visible to the caller.
        """
        from rest_framework.exceptions import (  # pylint: disable=import-outside-toplevel
            PermissionDenied,
        )

        class _DenyFieldDict(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Raise with a field-keyed dict detail."""
                raise PermissionDenied({"location": "Restricted by tenant"})

            def list(self, request: Any) -> Response:
                """Unreachable."""
                return Response([])

        tool = ToolDefinition(
            name="fielddeny.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_DenyFieldDict,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is True
        # Field name is preserved as a "field: message" prefix.
        assert "location: Restricted by tenant" in result.content["error"]

    def test_plain_python_exception_falls_back_to_str(self, rf: RequestFactory) -> None:
        """Non-DRF exceptions (no .detail) fall back to str(exc) unchanged."""

        class _RaisesPlain(ViewSet):
            def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
                """Raise a plain RuntimeError so the fallback path is taken."""
                raise RuntimeError("boom on initial")

            def list(self, request: Any) -> Response:
                """Unreachable."""
                return Response([])

        tool = ToolDefinition(
            name="plain.list",
            description="stub",
            input_schema={"type": "object"},
            permission_classes=(),
            source="auto",
            view_class=_RaisesPlain,
            action="list",
        )
        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = None  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(tool, {}, outer)

        assert result.is_error is True
        assert result.content["error"] == "boom on initial"


# ---------------------------------------------------------------------------
# PKG-18 — outer-request user/auth must reach the inner serializer context
# ---------------------------------------------------------------------------


class _UserCheckSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Asserts request.user.is_authenticated inside to_internal_value."""

    name = serializers.CharField()

    def to_internal_value(self, data: Any) -> Any:
        """Reject the payload when the request user is not authenticated."""
        request = self.context.get("request")
        if request is None or not getattr(request.user, "is_authenticated", False):
            raise serializers.ValidationError({"__all__": "request.user is not authenticated"})
        validated = super().to_internal_value(data)
        validated["who"] = str(request.user)
        return validated


class _AuthCheckViewSet(ViewSet):
    """ViewSet whose serializer requires an authenticated request.user."""

    def get_serializer_context(self) -> dict[str, Any]:
        """DRF default ViewSet doesn't ship one — replicate the standard."""
        return {"request": self.request, "view": self}

    def get_serializer(self, *args: Any, **kwargs: Any) -> _UserCheckSerializer:
        """Mirror DRF's ModelViewSet contract."""
        kwargs.setdefault("context", self.get_serializer_context())
        return _UserCheckSerializer(*args, **kwargs)

    def create(self, request: Any) -> Response:
        """Pass the body through the user-check serializer."""
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(ser.validated_data, status=status.HTTP_201_CREATED)


def _auth_check_tool() -> ToolDefinition:
    """ToolDefinition pointing at _AuthCheckViewSet.create."""
    return ToolDefinition(
        name="auth.create",
        description="stub",
        input_schema={"type": "object"},
        permission_classes=(),
        source="auto",
        view_class=_AuthCheckViewSet,
        action="create",
    )


class TestOuterRequestUserPropagation:
    """The outer request's user reaches the inner DRF Request context."""

    def test_authenticated_outer_request_reaches_serializer(self, rf: RequestFactory) -> None:
        """Authenticated outer request: request.user.is_authenticated is True in the serializer."""
        from django.contrib.auth.models import (  # pylint: disable=import-outside-toplevel
            AnonymousUser,
        )

        # Stand-in user object — duck-typed, no DB needed.
        class _FakeUser:
            is_authenticated = True
            is_active = True

            def __str__(self) -> str:
                return "real-user"

        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = _FakeUser()  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        result = SyncInvocation().invoke(_auth_check_tool(), {"name": "x"}, outer)

        assert result.is_error is False, f"unexpected error: {result.content!r}"
        assert result.content["who"] == "real-user"
        # Sanity: AnonymousUser is NOT what the serializer saw.
        assert result.content["who"] != str(AnonymousUser())

    def test_anonymous_outer_request_propagates_anonymous_user(self, rf: RequestFactory) -> None:
        """
        An anonymous outer request propagates AnonymousUser into the inner request.

        The serializer rejects the payload (is_authenticated=False) and the
        ValidationError re-raises through SyncInvocation.invoke() per the
        existing protocol-layer contract — confirming the dispatcher never
        silently upgrades AnonymousUser to authenticated.
        """
        from django.contrib.auth.models import (  # pylint: disable=import-outside-toplevel
            AnonymousUser,
        )

        outer = rf.post("/mcp/", content_type="application/json")
        outer.user = AnonymousUser()  # type: ignore[attr-defined]
        outer.auth = None  # type: ignore[attr-defined]

        with pytest.raises(serializers.ValidationError) as excinfo:
            SyncInvocation().invoke(_auth_check_tool(), {"name": "x"}, outer)
        assert "request.user is not authenticated" in str(excinfo.value)

    def test_outer_request_with_no_user_attr_falls_back_to_anonymous(
        self, rf: RequestFactory
    ) -> None:
        """An outer request lacking a .user attr does not crash; uses AnonymousUser."""
        outer = rf.post("/mcp/", content_type="application/json")
        if hasattr(outer, "user"):
            del outer.user
        outer.auth = None  # type: ignore[attr-defined]
        # The auth-check serializer rejects (AnonymousUser fallback) — what
        # we are confirming here is the *absence* of AttributeError on the
        # missing .user attribute, i.e. the getattr() fallback works.
        with pytest.raises(serializers.ValidationError):
            SyncInvocation().invoke(_auth_check_tool(), {"name": "x"}, outer)
