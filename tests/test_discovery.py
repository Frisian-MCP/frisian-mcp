"""Tests for DRFSyncDiscovery and helper functions."""

# pylint: disable=abstract-method
from __future__ import annotations

import pytest
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated

from friese_mcp.backends.base import ToolDefinition
from friese_mcp.backends.discovery import (
    DRFSyncDiscovery,
    _action_description,
    _resource_from_path,
    _schema_from_serializer,
)
from tests.urls import UserViewSet

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


# ---------------------------------------------------------------------------
# _action_description
# ---------------------------------------------------------------------------


class TestActionDescription:
    """Tests for the _action_description() helper."""

    def test_list_action_description(self) -> None:
        """List action produces a human-readable description with the resource name."""
        desc = _action_description(UserViewSet, "list")
        # Must contain the class-derived resource name — not "None".
        # ViewSetMixin defines basename=None as a class attribute; the fallback
        # must use the class name instead.
        assert "None" not in desc
        assert "User" in desc

    def test_retrieve_action_description(self) -> None:
        """Retrieve action description contains the resource name and is not 'None'."""
        desc = _action_description(UserViewSet, "retrieve")
        assert "None" not in desc
        assert "User" in desc

    def test_unknown_action_falls_back_to_generic(self) -> None:
        """An action name not in the label map produces a generic description."""
        desc = _action_description(UserViewSet, "custom_action")
        assert "UserViewSet" in desc or "custom_action" in desc
