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


# ---------------------------------------------------------------------------
# _field_to_schema — unit tests
# ---------------------------------------------------------------------------


class TestFieldToSchema:
    """Direct probes of _field_to_schema() for each supported field type."""

    def test_pk_related_field_emits_oneof(self) -> None:
        """PrimaryKeyRelatedField → oneOf[string, object] FK shape."""
        schema = _field_to_schema(_FKSerializer().fields["parent"])
        assert schema == _FK_ITEM_SCHEMA

    def test_slug_related_field_emits_oneof(self) -> None:
        """SlugRelatedField is also a RelatedField → same FK shape."""
        schema = _field_to_schema(_SlugFKSerializer().fields["owner"])
        assert "oneOf" in schema
        assert any(branch.get("type") == "string" for branch in schema["oneOf"])
        assert any(branch.get("type") == "object" for branch in schema["oneOf"])

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
        """{pk: <uuid>} — Nautobot NaturalKeyOrPK form — passes."""
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
