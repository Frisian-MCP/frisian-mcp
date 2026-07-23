"""
Tests for :mod:`frisian_mcp.route_paths`.

PR-5 covers watch-item 7 — route path normalisation and collision checks:

* Normalisation is slash- and whitespace-insensitive: ``/mcp``, ``mcp/``, and
  ``//mcp//`` all canonicalise to ``mcp``, and comparison only ever happens on
  the canonical form.
* Route-vs-route collision is FATAL on **exact** normalised equality only.
  Shared-prefix nesting (``mcp`` + ``mcp/elevated`` + ``mcp/admin``) is legal
  and must never be flagged.
* Route-vs-reserved collision is FATAL in **both** directions — a route may
  neither nest under a reserved path nor swallow one.
* Both collision checks respect segment boundaries, so ``oauthx`` does not
  collide with the reserved ``oauth``.
* A greedy root mount is FATAL with its own diagnostic.
* Templating braces in a path are FATAL — the mechanical enforcement of
  deferral #1 (no ``{optional_principal_id}``).
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from frisian_mcp.route_config import RouteConfig
from frisian_mcp.route_paths import (
    CODE_DUPLICATE_PATH,
    CODE_EMPTY_PATH,
    CODE_INVALID_PATH_TYPE,
    CODE_PATH_TEMPLATE,
    CODE_RESERVED_PATH,
    RESERVED_ROUTE_PATHS,
    RoutePathError,
    normalize_route_path,
    reserved_route_paths,
    validate_route_paths,
)


def _cfg(name: str, path: str) -> RouteConfig:
    """Build a minimal RouteConfig carrying only the field PR-5 reads."""
    return RouteConfig(name=name, path=path)


class TestNormalizeRoutePath:
    """Canonicalisation is total: one canonical form per equivalent input."""

    @pytest.mark.parametrize(
        "raw",
        ["/mcp", "mcp/", "/mcp/", "//mcp//", "  /mcp/  ", "mcp"],
    )
    def test_equivalent_forms_collapse(self, raw: str) -> None:
        """Leading/trailing slashes and whitespace never change identity."""
        assert normalize_route_path(raw) == "mcp"

    def test_internal_slash_run_collapsed(self) -> None:
        """Repeated internal slashes collapse to a single separator."""
        assert normalize_route_path("/mcp///elevated/") == "mcp/elevated"

    def test_nested_path_preserved(self) -> None:
        """Interior structure survives normalisation."""
        assert normalize_route_path("mcp/elevated") == "mcp/elevated"

    @pytest.mark.parametrize("raw", [None, 42, [], {}, b"mcp", True])
    def test_non_string_rejected(self, raw: object) -> None:
        """A non-string path is E200."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path(raw)
        assert excinfo.value.code == CODE_INVALID_PATH_TYPE

    @pytest.mark.parametrize("raw", ["", "/", "//", "   ", " / "])
    def test_greedy_root_mount_rejected(self, raw: str) -> None:
        """A path normalising to '' is E201, not a silent empty prefix."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path(raw)
        assert excinfo.value.code == CODE_EMPTY_PATH

    def test_greedy_root_error_names_reserved_paths(self) -> None:
        """The root-mount diagnostic explains what it would have shadowed."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path("/")
        assert "oauth" in str(excinfo.value)

    @pytest.mark.parametrize(
        "raw",
        [
            "mcp/{principal_id}",
            "{optional_principal_id}",
            "mcp/{tenant}/tools",
            "mcp/}weird{",
        ],
    )
    def test_templating_braces_rejected(self, raw: str) -> None:
        """Deferral #1: v1.1 paths are literal strings. Braces are E202."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path(raw)
        assert excinfo.value.code == CODE_PATH_TEMPLATE

    def test_template_error_states_the_deferral(self) -> None:
        """The E202 message says a path segment is never a credential."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path("mcp/{principal_id}")
        assert "never a credential" in str(excinfo.value)

    def test_route_name_propagates_into_error(self) -> None:
        """PR-9 reads route_name off the exception without extra plumbing."""
        with pytest.raises(RoutePathError) as excinfo:
            normalize_route_path("/", route_name="elevated")
        assert excinfo.value.route_name == "elevated"
        assert "elevated" in str(excinfo.value)


class TestReservedRoutePaths:
    """Reserved paths are unconditional, canonical, and de-duplicated."""

    def test_static_reserved_paths_present(self) -> None:
        """oauth, .well-known, and the bare register stub are always reserved."""
        reserved = reserved_route_paths()
        for expected in (".well-known", "oauth", "register"):
            assert expected in reserved

    def test_default_healthcheck_path_reserved(self) -> None:
        """The default healthcheck path is protected from shadowing."""
        assert "backend/healthcheck" in reserved_route_paths()

    @override_settings(FRISIAN_MCP_HEALTHCHECK_PATHS=["/ops/health/"])
    def test_relocated_healthcheck_path_reserved_and_normalized(self) -> None:
        """A host that moves its healthcheck still has that path protected."""
        assert "ops/health" in reserved_route_paths()

    @override_settings(FRISIAN_MCP_HEALTHCHECK_PATHS=["oauth", "", None, 7])
    def test_reserved_paths_deduped_and_junk_filtered(self) -> None:
        """Duplicate and non-string healthcheck entries never reach the matcher."""
        reserved = reserved_route_paths()
        assert reserved.count("oauth") == 1
        assert "" not in reserved

    @override_settings(FRISIAN_MCP_HEALTHCHECK_PATHS="/healthz")
    def test_scalar_healthcheck_path_reserved_as_one_path(self) -> None:
        """Regression: a bare string is one path, not an iterable of characters.

        Without coercion ``"/healthz"`` iterates to 'h', 'e', 'a', ... leaving
        the real ``healthz`` path claimable by a route.
        """
        reserved = reserved_route_paths()
        assert "healthz" in reserved
        assert "h" not in reserved


class TestReservedCollisions:
    """A route may neither nest under a reserved path nor swallow one."""

    @pytest.mark.parametrize("path", list(RESERVED_ROUTE_PATHS))
    def test_exact_reserved_match_is_fatal(self, path: str) -> None:
        """Claiming a reserved path outright is E204."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", path)})
        assert excinfo.value.code == CODE_RESERVED_PATH

    @pytest.mark.parametrize(
        "path",
        [
            "oauth/authorize",
            "oauth/token",
            "oauth/register",
            ".well-known/oauth-protected-resource",
        ],
    )
    def test_nesting_under_reserved_is_fatal(self, path: str) -> None:
        """A route beneath a package-owned namespace is E204."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", path)})
        assert excinfo.value.code == CODE_RESERVED_PATH

    def test_exact_reserved_message_reads_cleanly(self) -> None:
        """The exact-match diagnostic is a well-formed sentence."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", "oauth")})
        assert "path='oauth' is reserved by the package." in str(excinfo.value)

    def test_nesting_reserved_message_reads_cleanly(self) -> None:
        """The nesting diagnostic names the parent and is a well-formed sentence."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", "oauth/token")})
        assert "nests under the reserved package path 'oauth'." in str(excinfo.value)

    def test_swallowing_a_reserved_path_is_fatal(self) -> None:
        """'backend' swallows the reserved 'backend/healthcheck' — E204."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", "backend")})
        assert excinfo.value.code == CODE_RESERVED_PATH
        assert excinfo.value.conflict == "backend/healthcheck"
        assert "swallow" in str(excinfo.value)

    def test_reserved_conflict_records_the_other_party(self) -> None:
        """The conflict field carries the reserved path so PR-9 can name it."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"admin": _cfg("admin", "/oauth/token/")})
        assert excinfo.value.conflict == "oauth"
        assert excinfo.value.route_name == "admin"

    def test_reserved_match_is_slash_insensitive(self) -> None:
        """Reserved detection runs on the canonical form, not the raw string."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", "//oauth//")})
        assert excinfo.value.code == CODE_RESERVED_PATH

    @pytest.mark.parametrize(
        "path",
        ["oauthx", "registers", "oauth-tools", ".well-knownish", "backendish"],
    )
    def test_segment_boundary_prevents_false_positives(self, path: str) -> None:
        """'oauthx' is not under 'oauth' — prefix matching respects boundaries."""
        assert validate_route_paths({"default": _cfg("default", path)}) == {"default": path}

    @override_settings(FRISIAN_MCP_HEALTHCHECK_PATHS=["ops/health"])
    def test_relocated_healthcheck_is_protected(self) -> None:
        """Shadowing a relocated healthcheck is FATAL too."""
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _cfg("default", "ops/health")})
        assert excinfo.value.code == CODE_RESERVED_PATH

    @override_settings(FRISIAN_MCP_HEALTHCHECK_PATHS=["ops/health"])
    def test_non_default_healthcheck_frees_the_default_path(self) -> None:
        """Reserving is driven by settings, so the old default is claimable."""
        result = validate_route_paths({"default": _cfg("default", "backend/healthcheck")})
        assert result == {"default": "backend/healthcheck"}


class TestRouteCollisions:
    """Exact normalised equality collides; shared-prefix nesting does not."""

    def test_shared_prefix_nesting_is_legal(self) -> None:
        """The encouraged three-tier layout must validate cleanly."""
        configs = {
            "default": _cfg("default", "/mcp"),
            "elevated": _cfg("elevated", "/mcp/elevated"),
            "admin": _cfg("admin", "/mcp/admin"),
        }
        assert validate_route_paths(configs) == {
            "admin": "mcp/admin",
            "default": "mcp",
            "elevated": "mcp/elevated",
        }

    def test_deeper_nesting_is_legal(self) -> None:
        """Nesting depth is not a collision signal."""
        configs = {
            "default": _cfg("default", "mcp"),
            "admin": _cfg("admin", "mcp/internal/admin"),
        }
        assert validate_route_paths(configs)["admin"] == "mcp/internal/admin"

    def test_exact_duplicate_is_fatal(self) -> None:
        """Two routes on the same canonical path is E203."""
        configs = {
            "default": _cfg("default", "/mcp"),
            "elevated": _cfg("elevated", "mcp/"),
        }
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths(configs)
        assert excinfo.value.code == CODE_DUPLICATE_PATH

    def test_duplicate_error_names_both_routes(self) -> None:
        """The operator is told which two routes collided."""
        configs = {
            "admin": _cfg("admin", "/mcp"),
            "default": _cfg("default", "//mcp"),
        }
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths(configs)
        message = str(excinfo.value)
        assert excinfo.value.conflict == "admin"
        assert excinfo.value.route_name == "default"
        assert "admin" in message and "default" in message

    def test_duplicate_error_explains_that_nesting_is_allowed(self) -> None:
        """The diagnostic steers away from 'flatten everything' overcorrection."""
        configs = {"a": _cfg("a", "mcp"), "b": _cfg("b", "mcp")}
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths(configs)
        assert "shared-prefix nesting IS allowed" in str(excinfo.value)

    def test_duplicate_detection_is_deterministic(self) -> None:
        """Routes are visited sorted, so the same pair always reports first."""
        configs = {
            "elevated": _cfg("elevated", "mcp"),
            "default": _cfg("default", "mcp"),
            "admin": _cfg("admin", "mcp"),
        }
        for _ in range(5):
            with pytest.raises(RoutePathError) as excinfo:
                validate_route_paths(configs)
            assert excinfo.value.conflict == "admin"
            assert excinfo.value.route_name == "default"


class TestValidateRoutePathsContract:
    """The return value is the canonical mapping PR-6 mounts from."""

    def test_returns_canonical_paths(self) -> None:
        """PR-6 mounts from this mapping rather than re-deriving normalisation."""
        configs = {"default": _cfg("default", "  //mcp//  ")}
        assert validate_route_paths(configs) == {"default": "mcp"}

    def test_empty_config_is_valid(self) -> None:
        """No routes configured is not an error — nothing is mounted."""
        assert validate_route_paths({}) == {}

    def test_single_route_roundtrips(self) -> None:
        """The common single-route case needs no special handling."""
        assert validate_route_paths({"default": _cfg("default", "/mcp")}) == {"default": "mcp"}

    def test_missing_path_attribute_is_type_error(self) -> None:
        """A config object without a usable path fails as E200, not AttributeError."""

        class _NoPath:  # pylint: disable=too-few-public-methods
            """Stand-in for a malformed config object."""

        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths({"default": _NoPath()})
        assert excinfo.value.code == CODE_INVALID_PATH_TYPE

    def test_reserved_check_precedes_duplicate_check(self) -> None:
        """A reserved path is reported as reserved even when also duplicated."""
        configs = {"a": _cfg("a", "oauth"), "b": _cfg("b", "oauth")}
        with pytest.raises(RoutePathError) as excinfo:
            validate_route_paths(configs)
        assert excinfo.value.code == CODE_RESERVED_PATH
