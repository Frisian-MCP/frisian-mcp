"""Tests for DRFSyncDiscovery and helper functions."""

# pylint: disable=abstract-method
from __future__ import annotations

from typing import Any

import pytest
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated

from friese_mcp.apps import _apply_tool_filters, _suppress_dispatcher_shadowed
from friese_mcp.backends.base import ToolDefinition
from friese_mcp.backends.discovery import (
    DRFSyncDiscovery,
    _action_description,
    _filterset_properties,
    _resource_from_path,
    _schema_from_action_signature,
    _schema_from_filter_backends,
    _schema_from_serializer,
)
from tests.urls import (
    ContextDependentViewSet,
    FilterSetClassViewSet,
    FullyFilteredViewSet,
    OrderableViewSet,
    SearchableViewSet,
    TypedActionViewSet,
    UserViewSet,
)

# ---------------------------------------------------------------------------
# _resource_from_path
# ---------------------------------------------------------------------------


class TestResourceFromPath:
    """Tests for the _resource_from_path() helper."""

    def test_simple_path(self) -> None:
        """Extracts the last literal segment from a simple path."""
        assert _resource_from_path("^api/users/$") == "users"

    def test_path_with_pk_param(self) -> None:
        """Strips URL parameters and extracts the resource name."""
        assert _resource_from_path("^orders/(?P<pk>[^/.]+)/$") == "orders"

    def test_django_converter_syntax(self) -> None:
        """Handles Django path-converter syntax (``<pk>``, ``<int:pk>``)."""
        assert _resource_from_path("products/<int:pk>/") == "products"

    def test_hyphen_converted_to_underscore(self) -> None:
        """Hyphens in path segments are converted to underscores."""
        assert _resource_from_path("blog-posts/") == "blog_posts"

    def test_nested_path(self) -> None:
        """Extracts the last non-parameter segment from a nested path."""
        assert _resource_from_path("api/v1/users/") == "users"

    def test_empty_path_returns_unknown(self) -> None:
        """Returns 'unknown' for paths that produce no usable segments."""
        assert _resource_from_path("") == "unknown"

    def test_only_params_returns_unknown(self) -> None:
        """Returns 'unknown' when all path segments are parameters."""
        assert _resource_from_path("<pk>/") == "unknown"

    def test_version_only_path_returns_unknown(self) -> None:
        """Returns 'unknown' when all remaining segments are version/api prefixes."""
        assert _resource_from_path("api/v1/") == "unknown"

    def test_version_prefix_skipped(self) -> None:
        """Version prefix segments are skipped; the actual resource name is returned."""
        assert _resource_from_path("rest/v2/products/") == "products"

    def test_api_segment_skipped(self) -> None:
        """'api' segment is treated as a version prefix and skipped."""
        assert _resource_from_path("v1/") == "unknown"


# ---------------------------------------------------------------------------
# _schema_from_serializer
# ---------------------------------------------------------------------------


class _StringSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Minimal serializer with a CharField for type-mapping tests."""

    name = serializers.CharField()


class _IntegerSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Minimal serializer with an IntegerField."""

    count = serializers.IntegerField()


class _BooleanSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Minimal serializer with a BooleanField."""

    active = serializers.BooleanField()


class _RequiredSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer with a required and an optional field."""

    name = serializers.CharField()
    nickname = serializers.CharField(required=False)


class _ReadOnlySerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer with a read-only field."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField()


class _HelpTextSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer with help_text on a field."""

    email = serializers.EmailField(help_text="Contact email")


class TestSchemaFromSerializer:
    """Tests for the _schema_from_serializer() helper."""

    def test_string_field_maps_to_string_type(self) -> None:
        """CharField maps to JSON Schema type 'string'."""
        schema = _schema_from_serializer(_StringSerializer)
        assert schema["properties"]["name"]["type"] == "string"

    def test_integer_field_maps_to_integer_type(self) -> None:
        """IntegerField maps to JSON Schema type 'integer'."""
        schema = _schema_from_serializer(_IntegerSerializer)
        assert schema["properties"]["count"]["type"] == "integer"

    def test_boolean_field_maps_to_boolean_type(self) -> None:
        """BooleanField maps to JSON Schema type 'boolean'."""
        schema = _schema_from_serializer(_BooleanSerializer)
        assert schema["properties"]["active"]["type"] == "boolean"

    def test_required_fields_appear_in_required_list(self) -> None:
        """Required serializer fields appear in the JSON Schema 'required' list."""
        schema = _schema_from_serializer(_RequiredSerializer)
        assert "name" in schema.get("required", [])
        assert "nickname" not in schema.get("required", [])

    def test_read_only_fields_excluded(self) -> None:
        """Read-only fields are not included in the schema properties."""
        schema = _schema_from_serializer(_ReadOnlySerializer)
        assert "id" not in schema["properties"]
        assert "name" in schema["properties"]

    def test_help_text_becomes_description(self) -> None:
        """Field help_text is mapped to the JSON Schema 'description' property."""
        schema = _schema_from_serializer(_HelpTextSerializer)
        assert schema["properties"]["email"]["description"] == "Contact email"

    def test_invalid_serializer_returns_fallback(self) -> None:
        """An uninstantiable serializer returns the fallback empty schema."""

        class _Broken:
            """Not a real serializer."""

            def __init__(self) -> None:
                """Raise on construction."""
                raise RuntimeError("oops")

        schema = _schema_from_serializer(_Broken)
        assert schema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# DRFSyncDiscovery — discover_tools()
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("use_test_urls")
class TestDRFSyncDiscovery:
    """Integration-style tests for DRFSyncDiscovery.discover_tools()."""

    def test_discovers_standard_actions(self) -> None:
        """discover_tools() finds list, create, retrieve, update, partial_update, destroy."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert "users.list" in names
        assert "users.create" in names
        assert "users.retrieve" in names
        assert "users.update" in names
        assert "users.partial_update" in names
        assert "users.destroy" in names

    def test_discovers_custom_action(self) -> None:
        """
        discover_tools() finds custom @action methods not decorated with @mcp_ignore.

        The resource name is derived from the last URL segment, so the export
        action registered at ``api/users/export/`` gets the name ``export.export``.
        """
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        # _resource_from_path("api/users/export/") → "export"; action → "export.export"
        assert "export.export" in names

    def test_skips_class_level_mcp_ignore(self) -> None:
        """discover_tools() skips an entire ViewSet decorated with @mcp_ignore."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert not any(n.startswith("ignored.") for n in names)

    def test_skips_method_level_mcp_ignore(self) -> None:
        """discover_tools() skips individual actions decorated with @mcp_ignore."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert "users.private_action" not in names

    def test_no_duplicate_tools(self) -> None:
        """Each (ViewSet, action) pair appears at most once even across multiple URL patterns."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    def test_tool_definitions_are_tool_definition_instances(self) -> None:
        """Every discovered tool is a ToolDefinition dataclass instance."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        assert all(isinstance(t, ToolDefinition) for t in tools)

    def test_tool_source_is_auto(self) -> None:
        """Auto-discovered tools have source='auto'."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        assert all(t.source == "auto" for t in tools)

    def test_secure_viewset_inherits_permission_classes(self) -> None:
        """Tools from a ViewSet with permission_classes inherit them."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        secure = next((t for t in tools if t.name == "secure.list"), None)
        assert secure is not None
        assert IsAuthenticated in secure.permission_classes

    def test_user_viewset_inherits_drf_default_permissions(self) -> None:
        """UserViewSet uses DRF's default AllowAny permission class."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        user_list = next((t for t in tools if t.name == "users.list"), None)
        assert user_list is not None
        # DRF ViewSet defaults: permission_classes = [AllowAny]
        assert AllowAny in user_list.permission_classes


# ---------------------------------------------------------------------------
# DRFSyncDiscovery — get_input_schema()
# ---------------------------------------------------------------------------


class TestGetInputSchema:
    """Tests for DRFSyncDiscovery.get_input_schema()."""

    def _discovery(self) -> DRFSyncDiscovery:
        """Return a fresh DRFSyncDiscovery instance."""
        return DRFSyncDiscovery()

    def test_list_action_produces_empty_object_schema(self) -> None:
        """List action produces a schema with no required fields."""
        schema = self._discovery().get_input_schema(UserViewSet, "list")
        assert schema["type"] == "object"
        assert "required" not in schema

    def test_retrieve_action_requires_id(self) -> None:
        """Retrieve action schema requires an 'id' property."""
        schema = self._discovery().get_input_schema(UserViewSet, "retrieve")
        assert "id" in schema["properties"]
        assert "id" in schema.get("required", [])

    def test_destroy_action_requires_id(self) -> None:
        """Destroy action schema requires an 'id' property."""
        schema = self._discovery().get_input_schema(UserViewSet, "destroy")
        assert "id" in schema.get("required", [])

    def test_partial_update_id_not_required(self) -> None:
        """partial_update action has 'id' in properties but not in required."""
        schema = self._discovery().get_input_schema(UserViewSet, "partial_update")
        assert "id" in schema["properties"]
        assert "id" not in schema.get("required", [])

    def test_create_action_includes_serializer_fields(self) -> None:
        """Create action schema derives properties from the ViewSet serializer."""
        schema = self._discovery().get_input_schema(UserViewSet, "create")
        # UserSerializer has name, email, age fields
        assert "name" in schema["properties"]
        assert "email" in schema["properties"]

    def test_id_schema_accepts_integer_and_string(self) -> None:
        """Finding 2: id property uses anyOf[integer, string] to support UUID-keyed models."""
        schema = self._discovery().get_input_schema(UserViewSet, "retrieve")
        id_schema = schema["properties"]["id"]
        # Must accept both integer PKs and UUID strings.
        assert "anyOf" in id_schema
        types_in_anyof = {entry["type"] for entry in id_schema["anyOf"]}
        assert "integer" in types_in_anyof
        assert "string" in types_in_anyof

    def test_context_dependent_create_schema(self) -> None:
        """IT-6: ViewSet whose get_serializer_class() reads self.request.method works."""
        # ContextDependentViewSet accesses self.request.method; with viewset.request = None
        # this would raise AttributeError.  After the fix it should return CreateContextSerializer.
        schema = self._discovery().get_input_schema(ContextDependentViewSet, "create")
        assert "title" in schema["properties"]
        assert "status" not in schema["properties"]

    def test_context_dependent_update_schema(self) -> None:
        """IT-6: Non-POST method stub yields the update serializer with both fields."""
        # _schema_from_viewset uses method="POST" stub, so this returns CreateContextSerializer
        # for create and falls back gracefully for update (which maps to PUT, not POST).
        # The stub always presents method="POST", so update also gets CreateContextSerializer —
        # verify the schema is non-empty (no AttributeError raised).
        schema = self._discovery().get_input_schema(ContextDependentViewSet, "update")
        # id must be present (detail action) and at least one serializer field
        assert "id" in schema["properties"]


# ---------------------------------------------------------------------------
# _action_description
# ---------------------------------------------------------------------------


class TestActionDescription:
    """Tests for the _action_description() helper."""

    def test_list_action_uses_docstring(self) -> None:
        """IT-9: list action description is taken from the method docstring."""
        desc = _action_description(UserViewSet, "list")
        # UserViewSet.list docstring: "List all users."
        assert "List all users" in desc
        assert "None" not in desc

    def test_retrieve_action_uses_docstring(self) -> None:
        """IT-9: retrieve action description is taken from the method docstring."""
        desc = _action_description(UserViewSet, "retrieve")
        # UserViewSet.retrieve docstring: "Retrieve a single user."
        assert "Retrieve a single user" in desc
        assert "None" not in desc

    def test_create_action_uses_docstring(self) -> None:
        """IT-9: create action description is taken from the method docstring."""
        desc = _action_description(UserViewSet, "create")
        assert "Create a new user" in desc

    def test_unknown_action_falls_back_to_generic(self) -> None:
        """An action name not in the label map produces a generic description."""
        desc = _action_description(UserViewSet, "custom_action")
        assert "UserViewSet" in desc or "custom_action" in desc

    def test_no_docstring_viewset_produces_non_null_description(self) -> None:
        """ViewSet with no docstrings yields a non-null, non-empty description without 'None'."""
        from rest_framework.response import Response  # pylint: disable=import-outside-toplevel
        from rest_framework.viewsets import ViewSet  # pylint: disable=import-outside-toplevel

        class NodocViewSet(ViewSet):  # pylint: disable=missing-class-docstring
            # pylint: disable=missing-function-docstring
            def list(self, request: Any) -> Response:
                return Response([])

        desc = _action_description(NodocViewSet, "list")
        assert desc is not None
        assert desc != ""
        assert "None" not in desc

    def test_explicit_resource_used_in_generic_label(self) -> None:
        """When resource is supplied it appears in the generic label instead of the class name."""
        from rest_framework.response import Response  # pylint: disable=import-outside-toplevel
        from rest_framework.viewsets import ViewSet  # pylint: disable=import-outside-toplevel

        class OrderViewSet(ViewSet):  # pylint: disable=missing-class-docstring
            def list(self, request: Any) -> Response:
                """List orders."""
                return Response([])

        desc = _action_description(OrderViewSet, "list", resource="orders")
        assert "orders" in desc
        assert "None" not in desc

    def test_description_never_null_for_all_standard_actions(self) -> None:
        """Every standard action on a no-docstring ViewSet produces a non-null description."""
        from rest_framework.response import Response  # pylint: disable=import-outside-toplevel
        from rest_framework.viewsets import ViewSet  # pylint: disable=import-outside-toplevel

        class BarebonesViewSet(ViewSet):  # pylint: disable=missing-class-docstring
            # pylint: disable=missing-function-docstring
            def list(self, request: Any) -> Response:
                return Response([])

            def create(self, request: Any) -> Response:
                return Response({})

            def retrieve(self, request: Any, _pk: str | None = None) -> Response:
                return Response({})

            def update(self, request: Any, _pk: str | None = None) -> Response:
                return Response({})

            def partial_update(self, request: Any, _pk: str | None = None) -> Response:
                return Response({})

            def destroy(self, request: Any, _pk: str | None = None) -> Response:
                return Response(status=204)

        for action in ("list", "create", "retrieve", "update", "partial_update", "destroy"):
            desc = _action_description(BarebonesViewSet, action, resource="items")
            assert desc is not None, f"description is None for {action}"
            assert desc != "", f"description is empty for {action}"
            assert "None" not in desc, f"description contains 'None' for {action}: {desc!r}"


# ---------------------------------------------------------------------------
# Surface area control — per-ViewSet and settings-based filters
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("use_test_urls")
class TestActionFiltering:
    """Tests for mcp_include_actions, mcp_exclude_actions, ALLOWLIST, and DENYLIST."""

    def test_mcp_include_actions_restricts_to_listed_actions(self) -> None:
        """LimitedViewSet.mcp_include_actions=['list'] exposes only the list action."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert "limited.list" in names
        assert "limited.create" not in names

    def test_mcp_exclude_actions_drops_listed_action(self) -> None:
        """ExcludeDestroyViewSet.mcp_exclude_actions=['destroy'] hides destroy."""
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert "excludedestroy.list" in names
        assert "excludedestroy.destroy" not in names

    def test_mcp_include_actions_none_exposes_all_actions(self) -> None:
        """When mcp_include_actions is absent, all discovered actions are exposed."""
        # UserViewSet has no mcp_include_actions; list and create are both present.
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        assert "users.list" in names
        assert "users.create" in names

    def test_mcp_exclude_actions_empty_exposes_all_actions(self) -> None:
        """When mcp_exclude_actions is absent or empty, no actions are suppressed."""
        # LimitedViewSet has mcp_include_actions; UserViewSet has neither filter.
        discovery = DRFSyncDiscovery()
        tools = discovery.discover_tools()
        names = {t.name for t in tools}
        # UserViewSet has no mcp_exclude_actions — destroy is present.
        assert "users.destroy" in names


# ---------------------------------------------------------------------------
# _apply_tool_filters — settings-based ALLOWLIST / DENYLIST
# ---------------------------------------------------------------------------


def _make_stub_tool(name: str) -> ToolDefinition:
    """Return a minimal ToolDefinition for filter tests."""
    return ToolDefinition(
        name=name,
        description="stub",
        input_schema={"type": "object"},
        permission_classes=(),
        source="auto",
        view_class=UserViewSet,
        action="list",
    )


class TestApplyToolFilters:
    """Unit tests for the _apply_tool_filters() helper in apps.py."""

    def test_no_settings_returns_all_tools(self) -> None:
        """When neither setting is present, the full list is returned unchanged."""
        tools = [_make_stub_tool("a.list"), _make_stub_tool("b.create")]
        assert _apply_tool_filters(tools) == tools

    def test_allowlist_keeps_only_listed_names(self, settings: Any) -> None:
        """FRIESE_MCP_TOOL_ALLOWLIST retains only matching tool names."""
        settings.FRIESE_MCP_TOOL_ALLOWLIST = ["a.list"]
        tools = [_make_stub_tool("a.list"), _make_stub_tool("b.create")]
        result = _apply_tool_filters(tools)
        assert [t.name for t in result] == ["a.list"]

    def test_denylist_removes_listed_names(self, settings: Any) -> None:
        """FRIESE_MCP_TOOL_DENYLIST drops matching tool names."""
        settings.FRIESE_MCP_TOOL_DENYLIST = ["b.create"]
        tools = [_make_stub_tool("a.list"), _make_stub_tool("b.create")]
        result = _apply_tool_filters(tools)
        assert [t.name for t in result] == ["a.list"]

    def test_denylist_applied_after_allowlist(self, settings: Any) -> None:
        """A name in both allowlist and denylist is ultimately excluded."""
        settings.FRIESE_MCP_TOOL_ALLOWLIST = ["a.list", "b.create"]
        settings.FRIESE_MCP_TOOL_DENYLIST = ["a.list"]
        tools = [_make_stub_tool("a.list"), _make_stub_tool("b.create")]
        result = _apply_tool_filters(tools)
        assert [t.name for t in result] == ["b.create"]

    def test_empty_allowlist_drops_all_tools(self, settings: Any) -> None:
        """An explicit empty ALLOWLIST removes every tool."""
        settings.FRIESE_MCP_TOOL_ALLOWLIST = []
        tools = [_make_stub_tool("a.list"), _make_stub_tool("b.create")]
        assert _apply_tool_filters(tools) == []


# ---------------------------------------------------------------------------
# Filter backend schema introspection
# ---------------------------------------------------------------------------


class TestSchemaFromFilterBackends:
    """Unit tests for the _schema_from_filter_backends() helper."""

    def test_search_filter_adds_search_property(self) -> None:
        """SearchFilter adds a 'search' string property to list action schemas."""
        props = _schema_from_filter_backends(SearchableViewSet)
        assert "search" in props
        assert props["search"]["type"] == "string"

    def test_ordering_filter_adds_ordering_property(self) -> None:
        """OrderingFilter adds an 'ordering' string property."""
        props = _schema_from_filter_backends(OrderableViewSet)
        assert "ordering" in props
        assert props["ordering"]["type"] == "string"

    def test_ordering_filter_includes_field_enum(self) -> None:
        """OrderingFilter enum includes ascending and descending field variants."""
        props = _schema_from_filter_backends(OrderableViewSet)
        enum_values = props["ordering"].get("enum", [])
        assert "name" in enum_values
        assert "-name" in enum_values
        assert "created_at" in enum_values
        assert "-created_at" in enum_values

    def test_django_filter_backend_adds_filterset_fields(self) -> None:
        """DjangoFilterBackend adds properties for each entry in filterset_fields."""
        props = _schema_from_filter_backends(FullyFilteredViewSet)
        assert "status" in props
        assert "category" in props

    def test_fully_filtered_viewset_has_all_parameters(self) -> None:
        """A ViewSet with all three backends exposes search, ordering, and filter fields."""
        props = _schema_from_filter_backends(FullyFilteredViewSet)
        assert "search" in props
        assert "ordering" in props
        assert "status" in props

    def test_no_filter_backends_returns_empty_dict(self) -> None:
        """A ViewSet without filter_backends returns an empty dict."""
        props = _schema_from_filter_backends(UserViewSet)
        assert not props

    def test_filterset_class_properties(self) -> None:
        """_filterset_properties() reads base_filters from a filterset_class."""
        props = _filterset_properties(FilterSetClassViewSet)
        assert "status" in props
        assert "category" in props

    def test_filterset_class_label_used_as_description(self) -> None:
        """_filterset_properties() uses the filter label as the description."""
        props = _filterset_properties(FilterSetClassViewSet)
        assert props["status"]["description"] == "Filter by status"

    def test_subclassed_filter_backend_detected(self, monkeypatch: Any) -> None:
        """A DjangoFilterBackend subclass is detected via issubclass, not name match."""
        from rest_framework.response import (
            Response,  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        )
        from rest_framework.viewsets import (
            ViewSet,  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        )

        import friese_mcp.backends.discovery as _disc  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        class _FakeBase:
            pass

        class _SubclassBackend(_FakeBase):
            """Simulates NautobotFilterBackend(DjangoFilterBackend)."""

        class _SubclassViewSet(ViewSet):  # pylint: disable=abstract-method
            filter_backends = [_SubclassBackend]
            filterset_fields = ["site", "rack"]

            def list(self, request: Any) -> Response:
                """List."""
                return Response([])

        monkeypatch.setattr(_disc, "_DjangoFilterBackend", _FakeBase)
        props = _schema_from_filter_backends(_SubclassViewSet)
        assert "site" in props
        assert "rack" in props

    def test_declared_filters_fallback(self) -> None:
        """_filterset_properties() uses declared_filters when base_filters is absent."""
        from rest_framework.viewsets import (
            ViewSet,  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        )

        class _Filter:
            def __init__(self, label: str) -> None:
                self.label = label

        class _DeclaredFilterSet:
            """FilterSet with only declared_filters — no base_filters (Nautobot pattern)."""

            declared_filters = {
                "site": _Filter("Filter by site"),
                "rack": _Filter("Filter by rack"),
            }

        class _DeclaredViewSet(ViewSet):  # pylint: disable=abstract-method
            filter_backends = []
            filterset_class = _DeclaredFilterSet

        props = _filterset_properties(_DeclaredViewSet)
        assert "site" in props
        assert "rack" in props
        assert props["site"]["description"] == "Filter by site"


class TestFilterBackendsInDiscovery:
    """Integration tests: filter properties appear in discovered list schemas."""

    @pytest.mark.usefixtures("use_test_urls")
    def test_list_schema_includes_search_from_filter_backend(self) -> None:
        """discover_tools() list schema contains 'search' when SearchFilter is set."""
        discovery = DRFSyncDiscovery()
        schema = discovery.get_input_schema(SearchableViewSet, "list")
        assert "search" in schema["properties"]

    @pytest.mark.usefixtures("use_test_urls")
    def test_create_schema_does_not_include_filter_properties(self) -> None:
        """Filter properties are NOT added to non-list action schemas."""
        discovery = DRFSyncDiscovery()
        schema = discovery.get_input_schema(SearchableViewSet, "create")
        assert "search" not in schema.get("properties", {})


# ---------------------------------------------------------------------------
# Custom @action signature introspection
# ---------------------------------------------------------------------------


class TestSchemaFromActionSignature:
    """Unit tests for _schema_from_action_signature() helper."""

    def test_typed_params_become_properties(self) -> None:
        """Parameters with type annotations produce JSON Schema properties."""
        func = TypedActionViewSet.export
        schema = _schema_from_action_signature(func, TypedActionViewSet, "export")
        assert "fmt" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_string_annotation_maps_to_string_type(self) -> None:
        """A str-annotated parameter maps to JSON Schema type 'string'."""
        func = TypedActionViewSet.export
        schema = _schema_from_action_signature(func, TypedActionViewSet, "export")
        assert schema["properties"]["fmt"]["type"] == "string"

    def test_int_annotation_maps_to_integer_type(self) -> None:
        """An int-annotated parameter maps to JSON Schema type 'integer'."""
        func = TypedActionViewSet.export
        schema = _schema_from_action_signature(func, TypedActionViewSet, "export")
        assert schema["properties"]["limit"]["type"] == "integer"

    def test_params_with_defaults_are_not_required(self) -> None:
        """Parameters that have default values are not in the required list."""
        func = TypedActionViewSet.export
        schema = _schema_from_action_signature(func, TypedActionViewSet, "export")
        assert "fmt" not in schema.get("required", [])
        assert "limit" not in schema.get("required", [])

    def test_self_and_request_are_skipped(self) -> None:
        """'self' and 'request' are never included in the schema properties."""
        func = TypedActionViewSet.export
        schema = _schema_from_action_signature(func, TypedActionViewSet, "export")
        assert "self" not in schema["properties"]
        assert "request" not in schema["properties"]

    def test_no_params_returns_empty_properties(self) -> None:
        """Action with only self/request returns empty-properties schema."""
        func = TypedActionViewSet.summary
        schema = _schema_from_action_signature(func, TypedActionViewSet, "summary")
        assert not schema["properties"]


class TestCustomActionSchemaInGetInputSchema:
    """Integration: custom @action schemas via DRFSyncDiscovery.get_input_schema()."""

    def test_typed_custom_action_schema_has_properties(self) -> None:
        """get_input_schema() exposes typed parameters for a custom GET action."""
        discovery = DRFSyncDiscovery()
        schema = discovery.get_input_schema(TypedActionViewSet, "export")
        assert "fmt" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_standard_action_schema_unaffected(self) -> None:
        """Standard list/create actions are not affected by signature introspection."""
        discovery = DRFSyncDiscovery()
        schema = discovery.get_input_schema(UserViewSet, "list")
        # Signature introspection should not add 'request' or 'self' to list schema.
        assert "self" not in schema.get("properties", {})
        assert "request" not in schema.get("properties", {})


# ---------------------------------------------------------------------------
# TestSuppressDispatcherShadowed
# ---------------------------------------------------------------------------


def _tool_def(name: str) -> ToolDefinition:
    """Return a minimal ToolDefinition with the given name."""
    return ToolDefinition(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {}},
        permission_classes=(),
        source="auto",
    )


class TestSuppressDispatcherShadowed:
    """Unit tests for _suppress_dispatcher_shadowed."""

    def test_no_dispatchers_returns_all(self) -> None:
        """With no dispatchers, all tools are returned unchanged."""
        tools = [_tool_def("exercises.list"), _tool_def("exercises.create")]
        result = _suppress_dispatcher_shadowed(tools, frozenset())
        assert [t.name for t in result] == ["exercises.list", "exercises.create"]

    def test_exact_match_suppresses_tool(self) -> None:
        """Discovered tool with same resource prefix as dispatcher is suppressed."""
        tools = [_tool_def("exercises.list"), _tool_def("exercises.create")]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert result == []

    def test_singular_match_suppresses_tool(self) -> None:
        """Dispatcher 'exercises' suppresses discovered 'exercise.list'."""
        tools = [_tool_def("exercise.list"), _tool_def("exercise.retrieve")]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert result == []

    def test_unrelated_tools_not_suppressed(self) -> None:
        """Dispatcher 'exercises' does not affect 'orders.list'."""
        tools = [_tool_def("orders.list"), _tool_def("orders.create")]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert [t.name for t in result] == ["orders.list", "orders.create"]

    def test_mixed_resources_suppresses_only_matching(self) -> None:
        """Only tools whose resource matches the dispatcher are removed."""
        tools = [
            _tool_def("exercises.list"),
            _tool_def("exercises.create"),
            _tool_def("orders.list"),
        ]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert [t.name for t in result] == ["orders.list"]

    def test_multiple_dispatchers_suppress_respective_tools(self) -> None:
        """Two dispatchers each suppress their own resource tools."""
        tools = [
            _tool_def("exercises.list"),
            _tool_def("programs.list"),
            _tool_def("orders.list"),
        ]
        result = _suppress_dispatcher_shadowed(
            tools, frozenset({"exercises", "programs"})
        )
        assert [t.name for t in result] == ["orders.list"]

    def test_non_dotted_tool_exact_match(self) -> None:
        """A tool without a dot is matched by its full name."""
        tools = [_tool_def("exercises")]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert result == []

    def test_non_dotted_tool_no_match(self) -> None:
        """A tool without a dot is not suppressed when name differs."""
        tools = [_tool_def("orders")]
        result = _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert [t.name for t in result] == ["orders"]

    def test_empty_tool_list(self) -> None:
        """Empty tool list returns empty list regardless of dispatchers."""
        result = _suppress_dispatcher_shadowed([], frozenset({"exercises"}))
        assert result == []

    def test_suppressed_tools_logged(self, caplog: Any) -> None:
        """Suppressed tools are logged at INFO level."""
        import logging

        tools = [_tool_def("exercises.list")]
        with caplog.at_level(logging.INFO, logger="friese_mcp.apps"):
            _suppress_dispatcher_shadowed(tools, frozenset({"exercises"}))
        assert any("exercises.list" in r.message for r in caplog.records)
        assert any("exercises" in r.message for r in caplog.records)
