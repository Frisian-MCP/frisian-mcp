"""
Tests for :mod:`frisian_mcp.route_config`.

PR-2 covers the split of the former conflated ``auto_register`` name into two
distinct flags — ``auto_discover`` (tool discovery) and ``auto_register``
(client self-enrollment).  These tests pin the parser's shape:

* Both flags default to ``False`` (secure default).
* Both are accepted independently and land on their own fields.
* An unknown near-miss key raises :class:`ImproperlyConfigured` with a
  diagnostic that mentions both flag names by design — so an operator who
  reached for the wrong one lands on the right one from the error text.
* Missing ``path`` and wrong primitive types raise cleanly at parse time.
* No legacy single-name alias is accepted — the parser rejects instead of
  silently mapping.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from django.core.exceptions import ImproperlyConfigured

from frisian_mcp.route_config import (
    PERMISSION_TIER_RANK,
    PERMISSION_TIERS,
    ROUTE_CONFIG_KEYS,
    TIER_KEYS,
    RouteConfig,
    canonical_permission_tier,
    parse_route_config,
    parse_route_configs,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestSecureDefaults:
    """Both auto_* flags must default to False."""

    def test_auto_discover_defaults_false(self) -> None:
        """A bare route config yields ``auto_discover=False`` (secure default)."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.auto_discover is False

    def test_auto_register_defaults_false(self) -> None:
        """A bare route config yields ``auto_register=False`` (secure default)."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.auto_register is False

    def test_allow_and_deny_default_empty(self) -> None:
        """A bare route config yields empty allow/deny — deny-all baseline."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.allow_list == ()
        assert cfg.deny_list == ()

    def test_highest_tier_defaults_none(self) -> None:
        """A bare route config yields ``highest_tier=None`` (no ceiling override)."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.highest_tier is None

    def test_returned_value_is_frozen(self) -> None:
        """RouteConfig instances are immutable value objects."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        with pytest.raises(FrozenInstanceError):
            cfg.auto_discover = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The split itself — auto_discover and auto_register are independent
# ---------------------------------------------------------------------------


class TestFlagSplit:
    """auto_discover and auto_register land on independent fields."""

    def test_auto_discover_true_leaves_auto_register_false(self) -> None:
        """Enabling only ``auto_discover`` must not enable client self-enrollment."""
        cfg = parse_route_config("default", {"path": "/mcp", "auto_discover": True})
        assert cfg.auto_discover is True
        assert cfg.auto_register is False

    def test_auto_register_true_leaves_auto_discover_false(self) -> None:
        """Enabling only ``auto_register`` must not enable tool discovery."""
        cfg = parse_route_config("default", {"path": "/mcp", "auto_register": True})
        assert cfg.auto_register is True
        assert cfg.auto_discover is False

    def test_both_can_be_set_independently(self) -> None:
        """Both flags may be enabled together and preserve independent state."""
        cfg = parse_route_config(
            "default",
            {"path": "/mcp", "auto_discover": True, "auto_register": True},
        )
        assert cfg.auto_discover is True
        assert cfg.auto_register is True


# ---------------------------------------------------------------------------
# Rejection of legacy / near-miss keys
# ---------------------------------------------------------------------------


class TestLegacyKeyRejection:
    """
    Reject unknown keys — no silent alias mapping.

    Plausible near-misses (a conflated ``auto`` bare name, a plural typo,
    or a drop-in of an OAuth-style setting name at the per-route level)
    must not be silently accepted.  The diagnostic must mention both real
    flag names so the operator lands on the right one.
    """

    def test_unknown_key_rejected(self) -> None:
        """A bare ``auto`` key raises with a diagnostic naming both flags."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", "auto": True})
        msg = str(excinfo.value)
        assert "unknown key" in msg
        assert "auto_discover" in msg
        assert "auto_register" in msg

    def test_plural_typo_rejected(self) -> None:
        """A plural typo (``auto_registers``) is not silently accepted."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", "auto_registers": True})
        assert "auto_discover" in str(excinfo.value)
        assert "auto_register" in str(excinfo.value)

    def test_non_string_unknown_key_raises_improperly_configured(self) -> None:
        """A non-string key yields ImproperlyConfigured, not a TypeError from sorted().

        ``sorted(unknown)`` over mixed str/int keys would raise TypeError and
        bypass the promised friendly diagnostic; the (type, repr) sort key keeps
        it well-formed.
        """
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", 1: True})
        assert "unknown key" in str(excinfo.value)

    def test_hyphen_variant_rejected(self) -> None:
        """A hyphenated alias must not silently work."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": "/mcp", "auto-discover": True})

    def test_oauth_style_setting_name_rejected_at_route_level(self) -> None:
        """A user copying an OAuth setting name to route level gets a hard error."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config(
                "default",
                {"path": "/mcp", "oauth_pkce_auto_register": True},
            )


# ---------------------------------------------------------------------------
# Required keys and primitive-type validation
# ---------------------------------------------------------------------------


class TestRequiredAndTypedFields:
    """The parser validates presence of `path` and primitive types of every value."""

    def test_missing_path_rejected(self) -> None:
        """A route block without the required ``path`` key raises."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {})
        assert "'path'" in str(excinfo.value)

    def test_empty_path_rejected(self) -> None:
        """An empty-string ``path`` is not a valid mount point."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": ""})

    def test_non_string_path_rejected(self) -> None:
        """A non-string ``path`` value is a type error at parse."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": 123})

    def test_non_bool_auto_discover_rejected(self) -> None:
        """A stringified boolean for ``auto_discover`` is a type error."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", "auto_discover": "true"})
        assert "auto_discover" in str(excinfo.value)

    def test_non_bool_auto_register_rejected(self) -> None:
        """A numeric ``auto_register`` value is a type error even if truthy."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", "auto_register": 1})
        assert "auto_register" in str(excinfo.value)

    def test_non_list_allow_list_rejected(self) -> None:
        """A bare-string ``allow_list`` is a type error — the schema requires a list."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": "/mcp", "allow_list": "catalog"})

    def test_non_string_allow_list_entry_rejected(self) -> None:
        """A non-string entry inside ``allow_list`` is a type error."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": "/mcp", "allow_list": ["catalog", 42]})

    def test_non_string_highest_tier_rejected(self) -> None:
        """A non-string ``highest_tier`` value is a type error at parse."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("default", {"path": "/mcp", "highest_tier": 3})


# ---------------------------------------------------------------------------
# Raw allow/deny content preserved verbatim (grammar validated later)
# ---------------------------------------------------------------------------


class TestRawGrammarPassThrough:
    """
    PR-2 only enforces shape — allow/deny grammar is PR-4's remit.

    The parser therefore passes raw allow/deny entries through as an
    immutable tuple without interpretation.
    """

    def test_allow_list_preserved(self) -> None:
        """``allow_list`` entries land verbatim in the returned tuple."""
        cfg = parse_route_config("default", {"path": "/mcp", "allow_list": ["*", "catalog:item"]})
        assert cfg.allow_list == ("*", "catalog:item")

    def test_deny_list_preserved(self) -> None:
        """``deny_list`` entries land verbatim in the returned tuple."""
        cfg = parse_route_config("default", {"path": "/mcp", "deny_list": ["catalog", "app:tool"]})
        assert cfg.deny_list == ("catalog", "app:tool")


# ---------------------------------------------------------------------------
# Top-level parse_route_configs
# ---------------------------------------------------------------------------


class TestParseRouteConfigs:
    """The top-level parser iterates the outer mapping."""

    def test_multi_route_dict_parsed(self) -> None:
        """A three-tier config parses to a dict of RouteConfig instances."""
        raw = {
            "default": {"path": "/mcp"},
            "elevated": {
                "path": "/mcp/elevated",
                "highest_tier": "read_write",
                "auto_discover": True,
            },
            "admin": {
                "path": "/mcp/admin",
                "highest_tier": "admin",
                "auto_register": True,
                "allow_list": ["*"],
                "deny_list": ["catalog"],
            },
        }
        parsed = parse_route_configs(raw)
        assert set(parsed) == {"default", "elevated", "admin"}
        assert isinstance(parsed["default"], RouteConfig)
        assert parsed["elevated"].auto_discover is True
        assert parsed["admin"].auto_register is True
        assert parsed["admin"].allow_list == ("*",)
        assert parsed["admin"].deny_list == ("catalog",)

    def test_non_dict_top_level_rejected(self) -> None:
        """A non-mapping top-level ``FRISIAN_MCP_ROUTES`` value is a type error."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_configs([])

    def test_nested_error_surfaces_route_name(self) -> None:
        """A nested parse error names the offending route in its message."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_configs({"admin": {"path": "/mcp", "typo": True}})
        assert "'admin'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Schema surface pinned for downstream PRs
# ---------------------------------------------------------------------------


class TestSchemaSurface:
    """Pin the accepted key set so downstream PRs share a stable schema."""

    def test_route_config_keys_exact(self) -> None:
        """``ROUTE_CONFIG_KEYS`` is the exact set PR-3..PR-9 will read from."""
        assert ROUTE_CONFIG_KEYS == frozenset(
            {
                "path",
                "highest_tier",
                "auto_discover",
                "auto_register",
                "allow_list",
                "deny_list",
            }
        )

    def test_tier_keys_exact(self) -> None:
        """``TIER_KEYS`` is the exact fixed outer-key enum for FRISIAN_MCP_ROUTES."""
        assert TIER_KEYS == frozenset({"default", "elevated", "admin"})

    def test_permission_tiers_exact(self) -> None:
        """``PERMISSION_TIERS`` is the canonical ordered enum for permission tiers."""
        assert PERMISSION_TIERS == frozenset({"read", "read_write", "admin"})
        assert PERMISSION_TIER_RANK == {"read": 0, "read_write": 1, "admin": 2}


# ---------------------------------------------------------------------------
# PR-3 — tier vocabulary
# ---------------------------------------------------------------------------


class TestTierKeyRestriction:
    """The outer ``FRISIAN_MCP_ROUTES`` mapping accepts only the fixed tier keys."""

    def test_default_accepted(self) -> None:
        """``default`` is a valid outer tier key."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.name == "default"

    def test_elevated_accepted(self) -> None:
        """``elevated`` is a valid outer tier key."""
        cfg = parse_route_config("elevated", {"path": "/mcp/elevated"})
        assert cfg.name == "elevated"

    def test_admin_accepted(self) -> None:
        """``admin`` is a valid outer tier key."""
        cfg = parse_route_config("admin", {"path": "/mcp/admin"})
        assert cfg.name == "admin"

    def test_arbitrary_outer_name_rejected(self) -> None:
        """A user-invented outer tier key (e.g. ``mytier``) is a hard error."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("mytier", {"path": "/mcp"})
        msg = str(excinfo.value)
        assert "'mytier'" in msg
        assert "'default'" in msg
        assert "'elevated'" in msg
        assert "'admin'" in msg

    def test_case_variant_outer_name_rejected(self) -> None:
        """Uppercase or title-case tier keys are not canonical and get rejected."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_config("Default", {"path": "/mcp"})

    def test_parse_route_configs_rejects_invalid_outer_key(self) -> None:
        """The top-level parser propagates the tier-key rejection."""
        with pytest.raises(ImproperlyConfigured):
            parse_route_configs({"public": {"path": "/mcp"}})


class TestCanonicalPermissionTier:
    """The ``canonical_permission_tier`` helper rejects synonyms outright."""

    @pytest.mark.parametrize("value", ["read", "read_write", "admin"])
    def test_canonical_values_accepted(self, value: str) -> None:
        """Each canonical tier name is returned verbatim."""
        assert canonical_permission_tier(value) == value

    @pytest.mark.parametrize(
        "value",
        ["readonly", "read-only", "RO", "rw", "read_only", "write", "Admin", "READ"],
    )
    def test_synonyms_rejected(self, value: str) -> None:
        """Any synonym / case variant / alias raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            canonical_permission_tier(value)
        msg = str(excinfo.value)
        assert "canonical" in msg
        assert "read" in msg
        assert "read_write" in msg
        assert "admin" in msg

    def test_non_string_rejected(self) -> None:
        """Non-string tier values raise ImproperlyConfigured cleanly."""
        with pytest.raises(ImproperlyConfigured):
            canonical_permission_tier(3)

    def test_whitespace_padded_rejected(self) -> None:
        """Whitespace-padded values are not silently trimmed."""
        with pytest.raises(ImproperlyConfigured):
            canonical_permission_tier(" read ")

    def test_field_name_appears_in_error(self) -> None:
        """The caller's ``field_name`` argument is surfaced in the error."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            canonical_permission_tier("rw", field_name="my.custom.field")
        assert "my.custom.field" in str(excinfo.value)


class TestHighestTierParsing:
    """``parse_route_config`` runs ``highest_tier`` through the canonical helper."""

    @pytest.mark.parametrize("value", ["read", "read_write", "admin"])
    def test_canonical_highest_tier_accepted(self, value: str) -> None:
        """A canonical ``highest_tier`` lands in the RouteConfig verbatim."""
        cfg = parse_route_config("default", {"path": "/mcp", "highest_tier": value})
        assert cfg.highest_tier == value

    def test_synonym_rejected(self) -> None:
        """A synonym in ``highest_tier`` triggers ImproperlyConfigured at parse."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("default", {"path": "/mcp", "highest_tier": "readonly"})
        msg = str(excinfo.value)
        assert "readonly" in msg
        assert "canonical" in msg

    def test_synonym_error_names_the_route(self) -> None:
        """The synonym error is scoped to the offending route path."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            parse_route_config("elevated", {"path": "/mcp/elevated", "highest_tier": "rw"})
        assert "FRISIAN_MCP_ROUTES['elevated']['highest_tier']" in str(excinfo.value)

    def test_missing_highest_tier_defaults_none(self) -> None:
        """Omitting ``highest_tier`` still yields ``None`` (no default synonym trip)."""
        cfg = parse_route_config("default", {"path": "/mcp"})
        assert cfg.highest_tier is None
