"""Schema derivation must survive ViewSets that branch on the request (NB-4).

Discovery runs at startup with no HTTP request in flight, so
``_schema_from_viewset`` hands the ViewSet a stand-in.  That stand-in used to
be a ``SimpleNamespace`` carrying exactly four attributes -- ``method``,
``auth``, ``META``, ``user``.  A ``get_serializer_class()`` touching a fifth
raised ``AttributeError``, a bare ``except`` swallowed it, and the tool was
advertised to agents with ``{"type": "object", "properties": {}}``.

That is worse than a missing tool: the action is offered, the agent has to
guess the payload, and nothing fails visibly -- an empty schema imposes no
validation.  The entire loss is in what an agent can PLAN.

Two real host shapes were measured, and they needed DIFFERENT attributes.
Both are pinned here, because a fix that satisfies only one leaves the other
broken and looks finished:

* one branches on ``request.query_params``
* another branches on ``request.version``

The fix builds a real DRF ``Request`` rather than imitating one, so the whole
attribute surface arrives at once instead of one host at a time.
"""

# pylint: disable=abstract-method
# Test serializers/ViewSets deliberately omit create()/update(); matches the
# module-level convention already used by tests/test_discovery.py.

from __future__ import annotations

from typing import Any

from rest_framework import serializers, viewsets

from frisian_mcp.backends.discovery import DRFSyncDiscovery


class ThingSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer whose fields must survive derivation."""

    name = serializers.CharField()
    status = serializers.CharField(required=False)


class BriefSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """The narrower branch, returned only when the request says so."""

    id = serializers.IntegerField()


class QueryParamsViewSet(viewsets.ViewSet):
    """Branches on ``request.query_params`` via ``get_serializer_context()``."""

    brief = False

    def get_serializer_context(self) -> dict[str, Any]:
        """Mirror DRF's own context builder, which is where the request is read."""
        return {"request": self.request, "format": self.format_kwarg, "view": self}

    def get_serializer_class(self) -> type[serializers.Serializer[Any]]:
        """Return the serializer, branching on a query parameter."""
        request = self.get_serializer_context()["request"]
        if self.brief or "config_context" in request.query_params.get("exclude", []):
            return BriefSerializer
        return ThingSerializer


class VersionViewSet(viewsets.ViewSet):
    """Branches on ``request.version``.

    DRF sets ``version`` in ``APIView.initialize_request()``, not in
    ``Request.__init__``, so a bare ``Request`` is not enough on its own.
    """

    def get_serializer_class(self) -> type[serializers.Serializer[Any]]:
        """Return the serializer, branching on the negotiated API version."""
        if self.request.version and int(self.request.version) < 10:
            return BriefSerializer
        return ThingSerializer


class PlainViewSet(viewsets.ViewSet):
    """No request access at all -- the case that always worked."""

    def get_serializer_class(self) -> type[serializers.Serializer[Any]]:
        """Return the serializer without consulting the request at all."""
        return ThingSerializer


def _derive(view_class: type, action: str = "update") -> dict[str, Any]:
    # pylint: disable=protected-access
    return DRFSyncDiscovery()._schema_from_viewset(view_class, action)  # noqa: SLF001


class TestRequestDependentSerializers:
    """Both measured host shapes must derive a real schema, not an empty one."""

    def test_query_params_shape_derives_real_fields(self) -> None:
        """The first host shape: branches on ``request.query_params``."""
        schema = _derive(QueryParamsViewSet)
        assert sorted(schema["properties"]) == ["name", "status"]
        assert schema["required"] == ["name"]

    def test_version_shape_derives_real_fields(self) -> None:
        """The second host shape: branches on ``request.version``.

        Covered separately because a fix that only adds ``query_params``
        satisfies the test above while leaving this one broken.
        """
        schema = _derive(VersionViewSet)
        assert sorted(schema["properties"]) == ["name", "status"]
        assert schema["required"] == ["name"]

    def test_plain_viewset_is_unaffected(self) -> None:
        """A ViewSet that never touches the request keeps working."""
        schema = _derive(PlainViewSet)
        assert sorted(schema["properties"]) == ["name", "status"]

    def test_no_shape_falls_back_to_an_empty_schema(self) -> None:
        """None of the supported shapes may produce the empty-schema fallback.

        Stated as its own assertion because the empty schema is the actual
        defect: it is indistinguishable to an agent from "this action takes no
        arguments".
        """
        for view_class in (QueryParamsViewSet, VersionViewSet, PlainViewSet):
            assert _derive(view_class)["properties"]


class TestUnderivableSerializerStillDegrades:
    """A genuinely underivable ViewSet must degrade, not crash discovery."""

    def test_raising_viewset_falls_back_to_empty_schema(self) -> None:
        """Discovery must not abort because one ViewSet cannot be resolved."""

        class ExplodingViewSet(viewsets.ViewSet):
            """A ViewSet whose serializer genuinely cannot be resolved."""

            def get_serializer_class(self) -> type[serializers.Serializer[Any]]:
                """Fail the way an unresolvable host ViewSet would."""
                raise RuntimeError("no serializer here")

        assert _derive(ExplodingViewSet) == {"type": "object", "properties": {}}

    def test_failure_log_names_the_actual_exception(self, caplog: Any) -> None:
        """The warning must identify the real cause, not assert an unproven one.

        The previous message told the reader to check the host's
        ``get_serializer_class()``. The fault was in this package's own stub
        request, so that text pointed at the wrong codebase and cost real
        investigation time.
        """

        class ExplodingViewSet(viewsets.ViewSet):
            """A ViewSet that raises a recognisable error for log assertions."""

            def get_serializer_class(self) -> type[serializers.Serializer[Any]]:
                """Raise an error whose text the warning must reproduce."""
                raise RuntimeError("distinctive-marker-text")

        with caplog.at_level("WARNING"):
            _derive(ExplodingViewSet)

        message = caplog.text
        assert "RuntimeError" in message
        assert "distinctive-marker-text" in message
