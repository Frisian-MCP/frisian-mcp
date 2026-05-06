"""
PKG-16 — FK and M2M field schemas in DRFSyncDiscovery.

Verifies that DRF Related/ManyRelated/Tag fields produce dispatcher schemas
that accept the input shapes host serializers actually want.  The previous
behaviour (every field → ``{"type": "string"}``) created a catch-22 where
either the dispatcher schema or the host serializer rejected the input.
"""

# pylint: disable=redefined-outer-name,abstract-method
from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from rest_framework import serializers

from friese_mcp.backends.discovery import (
    _FK_ITEM_SCHEMA,
    _field_to_schema,
    _schema_from_serializer,
)
from friese_mcp.backends.invocation import (
    _extract_list_body,
    _is_fk_property,
    _normalize_fk_arguments,
    _normalize_fk_value,
)

# ---------------------------------------------------------------------------
# Stub serializers covering each field shape
# ---------------------------------------------------------------------------


class _FKSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Single FK field via PrimaryKeyRelatedField."""

    name = serializers.CharField()
    parent = serializers.PrimaryKeyRelatedField(queryset=[], required=False)


class _SlugFKSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Single FK field via SlugRelatedField."""

    owner = serializers.SlugRelatedField(queryset=[], slug_field="username", required=False)


class _M2MSlugSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """M2M field backed by SlugRelatedField (many=True)."""

    name = serializers.CharField()
    groups = serializers.SlugRelatedField(queryset=[], slug_field="name", many=True, required=False)


class _M2MSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """M2M field via ManyRelatedField (PrimaryKeyRelatedField + many=True)."""

    name = serializers.CharField()
    tags = serializers.PrimaryKeyRelatedField(queryset=[], many=True, required=False)


class _ListSerializerSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Nested write-many via ListSerializer."""

    name = serializers.CharField()
    children = _FKSerializer(many=True, required=False)


class _ScalarOnlySerializer(serializers.Serializer):  # type: ignore[type-arg]
    """No related fields — baseline regression check."""

    name = serializers.CharField()
    count = serializers.IntegerField()


# A duck-typed TagSerializerField stand-in so we can exercise the class-name
# fallback without taking django-taggit-serializer as a hard test dep.
class TagSerializerField(serializers.Field):  # type: ignore[type-arg]
    """Stub class with the canonical name; treated as M2M-of-strings."""

    def to_representation(self, value: Any) -> Any:  # pragma: no cover
        """Return *value* unchanged (stub for serializer protocol)."""
        return value

    def to_internal_value(self, data: Any) -> Any:  # pragma: no cover
        """Return *data* unchanged (stub for serializer protocol)."""
        return data


class _TagSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer using the TagSerializerField stand-in."""

    name = serializers.CharField()
    tags = TagSerializerField(required=False)


# A duck-typed ContentTypeField stand-in — exercises the class-name fallback
# for host-app fields (e.g. Nautobot's ContentTypeField) that accept bare
# "app_label.model" strings rather than UUID/dict FK form.
class ContentTypeField(serializers.RelatedField):  # type: ignore[type-arg]
    """Stub with the canonical name; treated as a bare-string field."""

    def to_representation(self, value: Any) -> Any:  # pragma: no cover
        return str(value)

    def to_internal_value(self, data: Any) -> Any:  # pragma: no cover
        return data


class _ContentTypeSingleSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer with a single ContentTypeField (non-M2M)."""

    name = serializers.CharField()
    content_type = ContentTypeField(queryset=[], required=False)


class _ContentTypeM2MSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer with a ManyRelatedField wrapping a ContentTypeField (M2M)."""

    name = serializers.CharField()
    content_types = ContentTypeField(queryset=[], many=True, required=False)


# ---------------------------------------------------------------------------
# _field_to_schema — unit tests
# ---------------------------------------------------------------------------


class TestFieldToSchema:
    """Direct probes of _field_to_schema() for each supported field type."""

    def test_pk_related_field_emits_oneof(self) -> None:
        """PrimaryKeyRelatedField → oneOf[string, object] FK shape."""
        schema = _field_to_schema(_FKSerializer().fields["parent"])
        assert schema == _FK_ITEM_SCHEMA

    def test_slug_related_field_emits_plain_string(self) -> None:
        """SlugRelatedField → plain {"type": "string"} — bare slug is the expected form."""
        schema = _field_to_schema(_SlugFKSerializer().fields["owner"])
        assert schema == {"type": "string"}

    def test_m2m_slug_child_emits_array_of_strings(self) -> None:
        """ManyRelatedField wrapping a SlugRelatedField → array of bare strings."""
        schema = _field_to_schema(_M2MSlugSerializer().fields["groups"])
        assert schema == {"type": "array", "items": {"type": "string"}}

    def test_many_related_field_emits_array(self) -> None:
        """ManyRelatedField → array of FK items."""
        schema = _field_to_schema(_M2MSerializer().fields["tags"])
        assert schema["type"] == "array"
        assert "oneOf" in schema["items"]

    def test_list_serializer_emits_array(self) -> None:
        """ListSerializer (write-many nested) → array of objects."""
        schema = _field_to_schema(_ListSerializerSerializer().fields["children"])
        assert schema["type"] == "array"
        assert schema["items"] == {"type": "object"}

    def test_tag_serializer_field_emits_array_of_strings(self) -> None:
        """TagSerializerField (duck-typed by name) → array of strings."""
        schema = _field_to_schema(_TagSerializer().fields["tags"])
        assert schema == {"type": "array", "items": {"type": "string"}}

    def test_scalar_field_unchanged(self) -> None:
        """A plain CharField still goes through the simple table lookup."""
        schema = _field_to_schema(_ScalarOnlySerializer().fields["name"])
        assert schema == {"type": "string"}

    def test_content_type_single_field_emits_string(self) -> None:
        """Single ContentTypeField → plain {"type": "string"} — no FK oneOf wrapping."""
        schema = _field_to_schema(_ContentTypeSingleSerializer().fields["content_type"])
        assert schema == {"type": "string"}

    def test_content_type_m2m_field_emits_array_of_strings(self) -> None:
        """ManyRelatedField wrapping ContentTypeField → array of bare strings, not FK items."""
        schema = _field_to_schema(_ContentTypeM2MSerializer().fields["content_types"])
        assert schema == {"type": "array", "items": {"type": "string"}}

    def test_content_type_m2m_not_normalized(self) -> None:
        """content_types strings must NOT be wrapped as {"name": ...} by the normalization layer."""
        schema = {"type": "object", "properties": {
            "content_types": {"type": "array", "items": {"type": "string"}},
        }}
        args = {"content_types": ["dcim.device", "dcim.rack", "ipam.prefix"]}
        result = _normalize_fk_arguments(args, schema)
        assert result["content_types"] == ["dcim.device", "dcim.rack", "ipam.prefix"]


# ---------------------------------------------------------------------------
# _schema_from_serializer — integration with the helper
# ---------------------------------------------------------------------------


class TestSchemaFromSerializer:
    """End-to-end serializer → schema conversion."""

    def test_fk_field_in_full_schema(self) -> None:
        """FK field appears as oneOf in the parent-level properties."""
        schema = _schema_from_serializer(_FKSerializer)
        parent = schema["properties"]["parent"]
        assert "oneOf" in parent

    def test_m2m_field_in_full_schema(self) -> None:
        """M2M field appears as type:array in the parent-level properties."""
        schema = _schema_from_serializer(_M2MSerializer)
        tags = schema["properties"]["tags"]
        assert tags["type"] == "array"

    def test_scalar_only_serializer_unchanged(self) -> None:
        """Serializers without related fields keep their simple shape."""
        schema = _schema_from_serializer(_ScalarOnlySerializer)
        assert schema["properties"]["name"] == {"type": "string"}
        assert schema["properties"]["count"] == {"type": "integer"}

    def test_required_carries_through(self) -> None:
        """`required=True` on the underlying field still populates required[]."""
        schema = _schema_from_serializer(_FKSerializer)
        assert "name" in schema.get("required", [])
        # parent has required=False — must not be required
        assert "parent" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# jsonschema validation — the critical reproduction of the catch-22
# ---------------------------------------------------------------------------


class TestFKFormsValidate:
    """All four FK input forms host serializers accept must pass the schema."""

    @pytest.fixture()
    def fk_schema(self) -> dict[str, Any]:
        """Return the dispatcher schema for a serializer with one FK field."""
        return _schema_from_serializer(_FKSerializer)["properties"]["parent"]

    def test_bare_uuid_string_passes(self, fk_schema: dict[str, Any]) -> None:
        """Bare UUID string (the historical agent-friendly form) passes."""
        jsonschema.validate("e7dc1234-5678-90ab-cdef-1234567890ab", fk_schema)

    def test_bare_natural_key_string_passes(self, fk_schema: dict[str, Any]) -> None:
        """Bare natural-key string (e.g. slug) passes."""
        jsonschema.validate("active", fk_schema)

    def test_dict_with_pk_passes(self, fk_schema: dict[str, Any]) -> None:
        """{pk: <uuid>} — natural-key/PK hybrid form — passes."""
        jsonschema.validate({"pk": "e7dc1234-5678-90ab-cdef-1234567890ab"}, fk_schema)

    def test_dict_with_id_passes(self, fk_schema: dict[str, Any]) -> None:
        """{id: <uuid>} — standard DRF form — passes."""
        jsonschema.validate({"id": "e7dc1234-5678-90ab-cdef-1234567890ab"}, fk_schema)

    def test_dict_with_slug_passes(self, fk_schema: dict[str, Any]) -> None:
        """{slug: <slug>} form passes."""
        jsonschema.validate({"slug": "active-status"}, fk_schema)

    def test_dict_with_name_passes(self, fk_schema: dict[str, Any]) -> None:
        """{name: <natural-key>} form passes."""
        jsonschema.validate({"name": "Active"}, fk_schema)

    def test_integer_rejected(self, fk_schema: dict[str, Any]) -> None:
        """A bare integer is NOT a valid FK reference (string or dict only)."""
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(42, fk_schema)


class TestM2MFormsValidate:
    """M2M arrays of mixed-form references must pass the schema."""

    @pytest.fixture()
    def m2m_schema(self) -> dict[str, Any]:
        """Return the dispatcher schema for a serializer with one M2M field."""
        return _schema_from_serializer(_M2MSerializer)["properties"]["tags"]

    def test_array_of_uuid_strings_passes(self, m2m_schema: dict[str, Any]) -> None:
        """A flat list of UUID strings is the canonical M2M form."""
        jsonschema.validate(
            [
                "e7dc1234-5678-90ab-cdef-1234567890ab",
                "f8ed2345-6789-01bc-defa-2345678901bc",
            ],
            m2m_schema,
        )

    def test_array_of_dicts_passes(self, m2m_schema: dict[str, Any]) -> None:
        """A list of {pk: ...} dicts also passes."""
        jsonschema.validate([{"pk": "e7dc1234-5678-90ab-cdef-1234567890ab"}], m2m_schema)

    def test_mixed_array_passes(self, m2m_schema: dict[str, Any]) -> None:
        """A list mixing string and dict items passes (each element oneOf)."""
        jsonschema.validate(
            ["a-uuid", {"name": "Active"}, {"slug": "x"}],
            m2m_schema,
        )

    def test_empty_array_passes(self, m2m_schema: dict[str, Any]) -> None:
        """An empty list (clear M2M) passes."""
        jsonschema.validate([], m2m_schema)

    def test_bare_string_rejected(self, m2m_schema: dict[str, Any]) -> None:
        """A bare string is NOT a valid M2M payload — the host wants an array."""
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate("a-uuid", m2m_schema)


class TestTagFieldFormsValidate:
    """TagSerializerField stand-in produces an array-of-strings schema."""

    def test_array_of_tag_strings_passes(self) -> None:
        """Tags submit as an array of bare name strings."""
        schema = _schema_from_serializer(_TagSerializer)["properties"]["tags"]
        jsonschema.validate(["alpha", "beta"], schema)

    def test_dict_form_rejected(self) -> None:
        """Tag fields don't accept the FK dict form — strict array of strings."""
        schema = _schema_from_serializer(_TagSerializer)["properties"]["tags"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate([{"name": "alpha"}], schema)


# ---------------------------------------------------------------------------
# PKG-24 — pre-flight FK normalization helpers
# ---------------------------------------------------------------------------

# Minimal FK schema matching the dispatcher's _FK_ITEM_SCHEMA pattern.
_FK_PROP: dict[str, Any] = {
    "oneOf": [
        {"type": "string"},
        {"type": "object", "properties": {"id": {"type": "string"}}, "additionalProperties": True},
    ]
}

_M2M_PROP: dict[str, Any] = {"type": "array", "items": _FK_PROP}

_NORMALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent": _FK_PROP,
        "tags": _M2M_PROP,
        "name": {"type": "string"},
        "owner": {"type": "string"},  # SlugRelatedField — plain string, not FK oneOf
    },
}


class TestIsFkProperty:
    """Unit tests for _is_fk_property predicate."""

    def test_fk_oneof_schema_detected(self) -> None:
        """The oneOf [string, object] pattern is recognized as a FK schema."""
        assert _is_fk_property(_FK_PROP) is True

    def test_plain_string_schema_not_detected(self) -> None:
        """A plain {"type": "string"} schema (SlugRelatedField etc.) is not FK."""
        assert _is_fk_property({"type": "string"}) is False

    def test_array_schema_not_detected(self) -> None:
        """An array schema is not itself a FK schema."""
        assert _is_fk_property(_M2M_PROP) is False

    def test_empty_schema_not_detected(self) -> None:
        """An empty schema returns False without error."""
        assert _is_fk_property({}) is False


class TestNormalizeFkValue:
    """Unit tests for _normalize_fk_value."""

    def test_bare_slug_wrapped_as_name(self) -> None:
        """A human-readable name string becomes {"name": value}."""
        assert _normalize_fk_value("active") == {"name": "active"}

    def test_uuid_passes_through(self) -> None:
        """A UUID string is valid as-is and must not be wrapped."""
        uid = "12345678-1234-5678-1234-567812345678"
        assert _normalize_fk_value(uid) == uid

    def test_uuid_uppercase_passes_through(self) -> None:
        """UUID strings are case-insensitive; uppercase passes through unchanged."""
        uid = "12345678-1234-5678-1234-567812345678".upper()
        assert _normalize_fk_value(uid) == uid

    def test_dict_passes_through(self) -> None:
        """An already-dict value is returned unchanged."""
        val = {"id": "some-uuid"}
        assert _normalize_fk_value(val) is val

    def test_none_passes_through(self) -> None:
        """None is not a string; it passes through without wrapping."""
        assert _normalize_fk_value(None) is None

    def test_integer_passes_through(self) -> None:
        """Non-string values are never wrapped."""
        assert _normalize_fk_value(42) == 42


class TestNormalizeFkArguments:
    """Unit tests for _normalize_fk_arguments."""

    def test_bare_slug_for_fk_field_wrapped(self) -> None:
        """A bare name string for a FK field is wrapped as {"name": value}."""
        args = {"parent": "region", "name": "test"}
        result = _normalize_fk_arguments(args, _NORMALIZATION_SCHEMA)
        assert result["parent"] == {"name": "region"}
        assert result["name"] == "test"  # plain CharField untouched

    def test_uuid_for_fk_field_passes_through(self) -> None:
        """A UUID string for a FK field is not wrapped."""
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        result = _normalize_fk_arguments({"parent": uid}, _NORMALIZATION_SCHEMA)
        assert result["parent"] == uid

    def test_dict_for_fk_field_passes_through(self) -> None:
        """An already-dict value for a FK field is not modified."""
        val = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
        result = _normalize_fk_arguments({"parent": val}, _NORMALIZATION_SCHEMA)
        assert result["parent"] == val

    def test_m2m_items_normalized_individually(self) -> None:
        """Each item in an M2M array is normalized; UUIDs pass through."""
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        result = _normalize_fk_arguments(
            {"tags": ["alpha", uid, {"name": "gamma"}]}, _NORMALIZATION_SCHEMA
        )
        assert result["tags"] == [{"name": "alpha"}, uid, {"name": "gamma"}]

    def test_m2m_empty_list_passes_through(self) -> None:
        """An empty list (M2M clear) passes through without error."""
        result = _normalize_fk_arguments({"tags": []}, _NORMALIZATION_SCHEMA)
        assert result["tags"] == []

    def test_slug_field_string_untouched(self) -> None:
        """A plain-string schema field (SlugRelatedField) is never wrapped."""
        result = _normalize_fk_arguments({"owner": "my-slug"}, _NORMALIZATION_SCHEMA)
        assert result["owner"] == "my-slug"

    def test_unknown_field_passes_through(self) -> None:
        """An argument not in the schema properties is left unchanged."""
        result = _normalize_fk_arguments(
            {"unknown_field": "some-value"}, _NORMALIZATION_SCHEMA
        )
        assert result["unknown_field"] == "some-value"

    def test_empty_schema_returns_args_unchanged(self) -> None:
        """When schema has no properties, arguments are returned as-is."""
        args = {"parent": "region"}
        result = _normalize_fk_arguments(args, {"type": "object"})
        assert result == args

    def test_original_dict_not_mutated(self) -> None:
        """The input arguments dict is not modified in place."""
        args = {"parent": "region"}
        _normalize_fk_arguments(args, _NORMALIZATION_SCHEMA)
        assert args["parent"] == "region"  # shallow copy only


# ---------------------------------------------------------------------------
# _extract_list_body — bulk-create list unwrapping
# ---------------------------------------------------------------------------


class TestExtractListBody:
    """_extract_list_body detects the bulk-create list convention."""

    def test_objects_key_returns_list(self) -> None:
        assert _extract_list_body({"objects": [{"name": "a"}, {"name": "b"}]}) == [
            {"name": "a"},
            {"name": "b"},
        ]

    def test_data_key_returns_list(self) -> None:
        assert _extract_list_body({"data": [{"id": 1}]}) == [{"id": 1}]

    def test_items_key_returns_list(self) -> None:
        assert _extract_list_body({"items": [{"x": 1}]}) == [{"x": 1}]

    def test_underscore_items_key_returns_list(self) -> None:
        assert _extract_list_body({"_items": [{"x": 1}]}) == [{"x": 1}]

    def test_unknown_key_returns_none(self) -> None:
        assert _extract_list_body({"records": [{"name": "a"}]}) is None

    def test_two_keys_returns_none(self) -> None:
        """More than one key → not the list-body convention."""
        assert _extract_list_body({"objects": [{"name": "a"}], "extra": 1}) is None

    def test_value_not_list_returns_none(self) -> None:
        assert _extract_list_body({"objects": {"name": "a"}}) is None

    def test_empty_list_is_detected(self) -> None:
        """An empty list is still the list-body convention (bulk-clear intent)."""
        assert _extract_list_body({"objects": []}) == []

    def test_regular_create_dict_returns_none(self) -> None:
        """A normal create payload (multiple keys) is not mistaken for list-body."""
        assert _extract_list_body({"name": "dev1", "device_type": "uuid", "role": "uuid"}) is None
