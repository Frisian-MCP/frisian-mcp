"""
Django system check tests for frisian-mcp configuration safety.

W001 — FRISIAN_MCP_PERMISSION_CLASSES empty in production.
W002 — FRISIAN_MCP_API_KEYS contains unhashed (plaintext) keys.
W003 — FRISIAN_MCP_SERVICE_ACCOUNT_USER set in production.
W012 — FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False while contrib.oauth is installed.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.core.checks import Error
from django.test import modify_settings, override_settings

from frisian_mcp.checks import (
    E007_INVALID_UNAUTHENTICATED_TIER,
    E008_DISPATCHER_WITHOUT_MEMBERSHIP,
    E009_HEAVY_CACHE_ALIAS_MISSING,
    E010_INVALID_MAX_TIER,
    W001_NO_PERMISSION_CLASSES,
    W002_PLAINTEXT_API_KEYS,
    W003_PRIVILEGED_SERVICE_ACCOUNT,
    W012_OAUTH_DISCOVERY_HIDDEN,
    W013_MALFORMED_RATE_LIMIT,
    W016_HEAVY_CACHE_NOT_ISOLATED,
    check_api_keys_are_hashed,
    check_dispatcher_membership,
    check_heavy_cache_isolation,
    check_max_tier_value,
    check_oauth_discovery_not_hidden,
    check_oauth_token_rate_limit_format,
    check_permission_classes_in_production,
    check_service_account_user,
    check_unauthenticated_tier_value,
)

# ---------------------------------------------------------------------------
# DEBUG=False, no permission classes → warning
# ---------------------------------------------------------------------------


class TestProductionMisconfiguration:
    """The W001 warning fires when DEBUG=False AND PERMISSION_CLASSES is empty."""

    @override_settings(DEBUG=False, FRISIAN_MCP_PERMISSION_CLASSES=[])
    def test_warning_fires_when_setting_is_empty_list(self, settings: Any) -> None:
        """Explicit ``[]`` in production is the misconfigured case."""
        if hasattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRISIAN_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert len(warnings) == 1
        assert warnings[0].id == W001_NO_PERMISSION_CLASSES

    @override_settings(DEBUG=False)
    def test_warning_fires_when_setting_is_absent(self, settings: Any) -> None:
        """An absent setting is treated identically to ``[]``."""
        for attr in ("FRISIAN_MCP_PERMISSION_CLASSES", "FRISIAN_MCP_ALLOW_UNAUTHENTICATED"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        warnings = check_permission_classes_in_production()
        assert len(warnings) == 1
        assert warnings[0].id == W001_NO_PERMISSION_CLASSES

    @override_settings(DEBUG=False, FRISIAN_MCP_PERMISSION_CLASSES=[])
    def test_warning_message_mentions_frisian_mcp_setting(self, settings: Any) -> None:
        """The warning message names the setting so operators can find it."""
        if hasattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRISIAN_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert "FRISIAN_MCP_PERMISSION_CLASSES" in warnings[0].msg

    @override_settings(DEBUG=False, FRISIAN_MCP_PERMISSION_CLASSES=[])
    def test_warning_hint_mentions_opt_in(self, settings: Any) -> None:
        """The hint shows the opt-out / opt-in path explicitly."""
        if hasattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRISIAN_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert "FRISIAN_MCP_ALLOW_UNAUTHENTICATED" in warnings[0].hint


# ---------------------------------------------------------------------------
# Silenced cases
# ---------------------------------------------------------------------------


class TestSilencedScenarios:
    """The check stays quiet for legitimate configurations."""

    @override_settings(DEBUG=True, FRISIAN_MCP_PERMISSION_CLASSES=[])
    def test_silent_in_debug_mode(self) -> None:
        """Developers running runserver should not get nagged."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRISIAN_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"],
    )
    def test_silent_when_classes_configured(self) -> None:
        """A non-empty list is the supported production shape."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRISIAN_MCP_PERMISSION_CLASSES=[],
        FRISIAN_MCP_ALLOW_UNAUTHENTICATED=True,
    )
    def test_silent_when_explicit_opt_in_set(self) -> None:
        """Operators who deliberately want an open gateway opt in to silence."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRISIAN_MCP_PERMISSION_CLASSES=[],
        FRISIAN_MCP_ALLOW_UNAUTHENTICATED=False,
    )
    def test_explicit_false_does_not_silence(self) -> None:
        """``FRISIAN_MCP_ALLOW_UNAUTHENTICATED=False`` is the same as not set."""
        warnings = check_permission_classes_in_production()
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Registration in Django's checks framework
# ---------------------------------------------------------------------------


class TestRegistration:
    """The check function is registered with Django's checks framework."""

    def test_check_is_registered(self) -> None:
        """``django.core.checks.run_checks(tags=['security'])`` includes our function."""
        from django.core.checks.registry import (  # pylint: disable=import-outside-toplevel
            registry,
        )

        registered = list(registry.get_checks(include_deployment_checks=True))
        # Our function should be in the registered set (identity match).
        assert check_permission_classes_in_production in registered


# ---------------------------------------------------------------------------
# Integration with manage.py check via Django's run_checks()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRunChecksIntegration:
    """End-to-end probe via django.core.checks.run_checks()."""

    @override_settings(DEBUG=False, FRISIAN_MCP_PERMISSION_CLASSES=[])
    def test_run_checks_surfaces_w001(self, settings: Any) -> None:
        """W001 appears in run_checks() output when configured insecurely."""
        from django.core.checks import (  # pylint: disable=import-outside-toplevel
            run_checks,
        )

        if hasattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRISIAN_MCP_ALLOW_UNAUTHENTICATED
        results = run_checks()
        ids = {w.id for w in results if hasattr(w, "id")}
        assert W001_NO_PERMISSION_CLASSES in ids


# ---------------------------------------------------------------------------
# W002 — FRISIAN_MCP_API_KEYS plaintext key detection
# ---------------------------------------------------------------------------


class TestApiKeysHashedCheck:
    """W002 fires when FRISIAN_MCP_API_KEYS contains non-hex-64 keys."""

    def test_warning_fires_for_raw_key(self, settings: Any) -> None:
        """A short human-readable key triggers W002."""
        settings.FRISIAN_MCP_API_KEYS = {"my-secret-key": "read"}
        warnings = check_api_keys_are_hashed()
        assert len(warnings) == 1
        assert warnings[0].id == W002_PLAINTEXT_API_KEYS

    def test_warning_fires_for_multiple_raw_keys(self, settings: Any) -> None:
        """Multiple unhashed keys → one warning with the count."""
        settings.FRISIAN_MCP_API_KEYS = {"raw1": "read", "raw2": "read_write"}
        warnings = check_api_keys_are_hashed()
        assert len(warnings) == 1
        assert "2" in warnings[0].msg

    def test_silent_for_64_char_hex_key(self, settings: Any) -> None:
        """A 64-char lowercase hex key (valid HMAC-SHA256 digest) passes silently."""
        settings.FRISIAN_MCP_API_KEYS = {"a" * 64: "read"}
        assert not check_api_keys_are_hashed()

    def test_silent_when_no_api_keys_set(self, settings: Any) -> None:
        """Empty dict → no warning."""
        settings.FRISIAN_MCP_API_KEYS = {}
        assert not check_api_keys_are_hashed()

    def test_silent_when_setting_absent(self, settings: Any) -> None:
        """Absent setting → no warning."""
        if hasattr(settings, "FRISIAN_MCP_API_KEYS"):
            del settings.FRISIAN_MCP_API_KEYS
        assert not check_api_keys_are_hashed()

    def test_uppercase_hex_treated_as_raw(self, settings: Any) -> None:
        """Uppercase hex is treated as raw — valid digests are lowercase only."""
        settings.FRISIAN_MCP_API_KEYS = {"A" * 64: "read"}
        warnings = check_api_keys_are_hashed()
        assert len(warnings) == 1

    def test_hint_mentions_management_command(self, settings: Any) -> None:
        """Hint directs the operator to mcp_hash_api_key."""
        settings.FRISIAN_MCP_API_KEYS = {"bad-key": "read"}
        warnings = check_api_keys_are_hashed()
        assert "mcp_hash_api_key" in warnings[0].hint

    def test_warning_fires_for_invalid_tier_value(self, settings: Any) -> None:
        """A typo'd API-key tier value triggers W002."""
        settings.FRISIAN_MCP_API_KEYS = {"a" * 64: "Admin"}
        warnings = check_api_keys_are_hashed()
        assert len(warnings) == 1
        assert warnings[0].id == W002_PLAINTEXT_API_KEYS
        assert "invalid permission tier" in warnings[0].msg

    def test_warning_hint_lists_valid_tiers_for_invalid_value(self, settings: Any) -> None:
        """The invalid-tier warning tells operators the allowed values."""
        settings.FRISIAN_MCP_API_KEYS = {"a" * 64: "write"}
        warnings = check_api_keys_are_hashed()
        assert "read_write" in warnings[0].hint
        assert "case-sensitive" in warnings[0].hint


# ---------------------------------------------------------------------------
# W003 — FRISIAN_MCP_SERVICE_ACCOUNT_USER in production
# ---------------------------------------------------------------------------


class TestServiceAccountUserCheck:
    """W003 fires when FRISIAN_MCP_SERVICE_ACCOUNT_USER is set in non-DEBUG."""

    @override_settings(DEBUG=False)
    def test_warning_fires_when_setting_present(self, settings: Any) -> None:
        """Setting present in production → W003."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "svc-account"
        warnings = check_service_account_user()
        assert len(warnings) == 1
        assert warnings[0].id == W003_PRIVILEGED_SERVICE_ACCOUNT

    @override_settings(DEBUG=False)
    def test_warning_message_includes_username(self, settings: Any) -> None:
        """Warning message names the configured account."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "svc-account"
        warnings = check_service_account_user()
        assert "svc-account" in warnings[0].msg

    @override_settings(DEBUG=False)
    def test_hint_mentions_mcp_doctor(self, settings: Any) -> None:
        """Hint directs operator to mcp_doctor --security."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "svc-account"
        warnings = check_service_account_user()
        assert "mcp_doctor" in warnings[0].hint

    @override_settings(DEBUG=True)
    def test_silent_in_debug_mode(self, settings: Any) -> None:
        """DEBUG=True suppresses the check."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "svc-account"
        assert not check_service_account_user()

    @override_settings(DEBUG=False)
    def test_silent_when_setting_absent(self, settings: Any) -> None:
        """Absent setting → no warning."""
        if hasattr(settings, "FRISIAN_MCP_SERVICE_ACCOUNT_USER"):
            del settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER
        assert not check_service_account_user()

    @override_settings(DEBUG=False)
    def test_silent_when_setting_is_empty_string(self, settings: Any) -> None:
        """Empty string is treated as not configured."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = ""
        assert not check_service_account_user()


# ---------------------------------------------------------------------------
# W012 — OAuth discovery hidden while contrib.oauth is installed
# ---------------------------------------------------------------------------


class TestOAuthDiscoveryHidden:
    """W012 fires when contrib.oauth is installed but PUBLIC_DISCOVERY=False."""

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
    def test_warning_fires_when_discovery_hidden(self) -> None:
        """contrib.oauth installed (test settings) + False → one LOUD warning."""
        warnings = check_oauth_discovery_not_hidden()
        assert len(warnings) == 1
        assert warnings[0].id == W012_OAUTH_DISCOVERY_HIDDEN

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False, DEBUG=True)
    def test_warning_fires_in_debug_mode_too(self) -> None:
        """No DEBUG gate — the handshake is equally broken in development."""
        assert len(check_oauth_discovery_not_hidden()) == 1

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
    def test_message_names_the_setting_and_the_break(self) -> None:
        """Operators must be able to find the setting and understand the failure."""
        warnings = check_oauth_discovery_not_hidden()
        assert "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY" in warnings[0].msg
        assert "resource_metadata" in warnings[0].msg

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
    def test_hint_points_at_the_real_gates_and_the_silencer(self) -> None:
        """The hint names the actual auth gates and the deliberate opt-out."""
        warnings = check_oauth_discovery_not_hidden()
        assert "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER" in warnings[0].hint
        assert "SILENCED_SYSTEM_CHECKS" in warnings[0].hint

    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=True)
    def test_silent_when_discovery_public(self) -> None:
        """Explicit True is the supported shape."""
        assert not check_oauth_discovery_not_hidden()

    def test_silent_when_setting_absent(self, settings: Any) -> None:
        """The default (absent → True) never warns."""
        if hasattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY"):
            del settings.FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY
        assert not check_oauth_discovery_not_hidden()

    @modify_settings(INSTALLED_APPS={"remove": "frisian_mcp.contrib.oauth"})
    @override_settings(FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False)
    def test_silent_when_contrib_oauth_not_installed(self) -> None:
        """Without the app there are no discovery endpoints to hide."""
        assert not check_oauth_discovery_not_hidden()


# ---------------------------------------------------------------------------
# W013 — malformed token rate-limit string (fails open silently)
# ---------------------------------------------------------------------------


class TestMalformedRateLimit:
    """W013 fires when FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT is set but unparseable."""

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="20/minutes")
    def test_warning_fires_on_bad_period(self) -> None:
        """A plausible typo ('minutes' not 'minute') is the silent-disable case."""
        warnings = check_oauth_token_rate_limit_format()
        assert len(warnings) == 1
        assert warnings[0].id == W013_MALFORMED_RATE_LIMIT

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="not-a-limit")
    def test_warning_message_names_the_setting_and_fail_open(self) -> None:
        """The operator must learn the throttle they configured is inactive."""
        warnings = check_oauth_token_rate_limit_format()
        assert "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT" in warnings[0].msg
        assert "fails open" in warnings[0].msg

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="20/minute")
    def test_silent_on_valid_value(self) -> None:
        """A well-formed value never warns."""
        assert not check_oauth_token_rate_limit_format()

    def test_silent_when_unset(self, settings: Any) -> None:
        """No configured limit → nothing to validate."""
        if hasattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT"):
            del settings.FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT
        assert not check_oauth_token_rate_limit_format()

    @modify_settings(INSTALLED_APPS={"remove": "frisian_mcp.contrib.oauth"})
    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="garbage")
    def test_silent_when_contrib_oauth_not_installed(self) -> None:
        """The limiter lives in contrib.oauth — no app, no check."""
        assert not check_oauth_token_rate_limit_format()

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="20/minute")
    def test_shares_parser_with_runtime(self) -> None:
        """The check and the runtime limiter must agree on what parses (one parser)."""
        from frisian_mcp.contrib.oauth._rate_limiting import parse_rate_limit

        assert parse_rate_limit("20/minute") == (20, 60)
        assert parse_rate_limit("20/minutes") is None
        assert not check_oauth_token_rate_limit_format()

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="0/minute")
    def test_zero_count_warns_not_silent_dos(self) -> None:
        """`0/minute` would block every request (fail-closed) — W013 must fire (V11-28)."""
        warnings = check_oauth_token_rate_limit_format()
        assert len(warnings) == 1
        assert warnings[0].id == W013_MALFORMED_RATE_LIMIT

    @override_settings(FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT="-5/minute")
    def test_negative_count_warns(self) -> None:
        """A negative count is malformed, not a valid throttle."""
        assert len(check_oauth_token_rate_limit_format()) == 1

    def test_parser_rejects_non_positive_counts(self) -> None:
        """parse_rate_limit rejects 0 and negatives so the runtime fails open, not closed."""
        from frisian_mcp.contrib.oauth._rate_limiting import parse_rate_limit

        assert parse_rate_limit("0/minute") is None
        assert parse_rate_limit("-1/hour") is None
        assert parse_rate_limit("1/minute") == (1, 60)


# ---------------------------------------------------------------------------
# H7 — E007: an unrecognised FRISIAN_MCP_UNAUTHENTICATED_TIER must be loud
# ---------------------------------------------------------------------------


class TestUnauthenticatedTierCheck:
    """
    This check is for the operator; the runtime already denies.

    Denying silently is its own trap: a host that meant ``read_write`` and typed
    ``readwrite`` would lose anonymous access with nothing explaining why.  The
    original defect was undetectable by reading the config — the setting was
    present and spelled plausibly — so the fix has to be detectable by reading
    the startup output.
    """

    def test_absent_setting_is_silent(self, settings: Any) -> None:
        """Absence is the documented default, not a misconfiguration."""
        if hasattr(settings, "FRISIAN_MCP_UNAUTHENTICATED_TIER"):
            del settings.FRISIAN_MCP_UNAUTHENTICATED_TIER
        assert check_unauthenticated_tier_value() == []

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER=None)
    def test_explicit_none_is_silent(self) -> None:
        """An explicit ``None`` is a deliberate lockdown, not an error."""
        assert check_unauthenticated_tier_value() == []

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="none")
    def test_canonical_none_string_is_silent(self) -> None:
        """The canonical ``"none"`` is a deliberate lockdown, not an error."""
        assert check_unauthenticated_tier_value() == []

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="read_write")
    def test_valid_tier_is_silent(self) -> None:
        """A recognised tier raises nothing."""
        assert check_unauthenticated_tier_value() == []

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="readwrite")
    def test_typo_raises_error_not_warning(self) -> None:
        """
        A typo is an ``Error``, deliberately, not a ``Warning``.

        Warnings are routinely ignored; this one changes whether anonymous
        callers can reach the server at all, so it fails ``manage.py check``
        and CI rather than scrolling past in a log stream.
        """
        errors = check_unauthenticated_tier_value()
        assert len(errors) == 1
        assert errors[0].id == E007_INVALID_UNAUTHENTICATED_TIER
        assert isinstance(errors[0], Error)

    @override_settings(FRISIAN_MCP_UNAUTHENTICATED_TIER="readwrite")
    def test_error_names_the_value_and_the_accepted_set(self) -> None:
        """
        The message must let the operator fix it without reading the source.

        It names what they typed, states that anonymous access is now DENIED
        (the consequence they will otherwise be debugging), and lists every
        accepted value including how to deny deliberately.
        """
        msg = check_unauthenticated_tier_value()[0]
        assert "readwrite" in msg.msg
        assert "DENIED" in msg.msg
        for accepted in ("read", "read_write", "admin", "none"):
            assert accepted in msg.hint

    def test_check_is_registered_with_django(self) -> None:
        """Registered under the security tag, so ``check --deploy`` runs it."""
        from django.core.checks import registry as checks_registry

        assert any(
            c is check_unauthenticated_tier_value for c in checks_registry.registry.get_checks()
        )


# ---------------------------------------------------------------------------
# H5 — E008: a group dispatcher registered without membership is an Error
# ---------------------------------------------------------------------------


class TestDispatcherMembershipCheck:
    """
    ``group_tool_names`` is a security-mechanism input, not a negotiation hint.

    Both consumers in ``views.py`` fail closed on a falsy membership set, so a
    host in this state is safe — it is simply never told that ``@mcp_heavy``
    negotiation and the dispatcher-routed lean write envelope have gone inert.
    The runtime is deliberately unchanged here; this check exists so the
    operator learns.
    """

    def _group(self, reg: Any, *, members: Any) -> None:
        """Register a group dispatcher the way ``apps.py`` does."""
        reg.register(
            name="catalog",
            fn=lambda a, r: {},
            description="Group dispatcher.",
            input_schema={"type": "object"},
            permission_classes=[],
            permission_tier="read",
            is_dispatcher=True,
            **({} if members is None else {"group_tool_names": members}),
        )

    def test_membership_less_group_raises_error(self) -> None:
        """The reported defect: a hand-registered group with no membership."""
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        self._group(reg, members=None)
        with patch("frisian_mcp.checks.tool_registry", reg):
            errors = check_dispatcher_membership()
        assert len(errors) == 1
        assert errors[0].id == E008_DISPATCHER_WITHOUT_MEMBERSHIP
        assert isinstance(errors[0], Error)

    def test_empty_membership_also_raises(self) -> None:
        """An empty frozenset is as unroutable as an absent one."""
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        self._group(reg, members=frozenset())
        with patch("frisian_mcp.checks.tool_registry", reg):
            assert len(check_dispatcher_membership()) == 1

    def test_populated_group_is_silent(self) -> None:
        """A package-constructed group never fires — apps.py always sets it."""
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("item_list", lambda a, r: {}, "member", {"type": "object"})
        self._group(reg, members=frozenset({"item_list"}))
        with patch("frisian_mcp.checks.tool_registry", reg):
            assert check_dispatcher_membership() == []

    def test_class_dispatcher_never_fires(self) -> None:
        """
        ⚠️ The blast-radius guard: a class dispatcher must never fire.

        A ``@mcp_dispatcher`` class legitimately has no membership set — its
        actions are methods, not registry entries — so firing on it would break
        startup for every correctly-configured host.

        The two kinds are mutually exclusive at registration: ``decorators.py``
        sets ``dispatcher_meta`` and never ``group_tool_names``; ``apps.py``
        and ``route_views.py`` do the reverse.  This asserts the discriminator,
        not merely that the happy path is quiet.
        """
        from frisian_mcp.decorators import mcp_action, mcp_dispatcher
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("tasks", description="Class dispatcher.")
            class _Tasks:
                """Synthetic class dispatcher."""

                @mcp_action("list", description="List.", params={})
                def list(self, request: Any, params: dict[str, Any]) -> Any:
                    """Return nothing."""
                    return {}

            _ = _Tasks

        entry = reg.get_entry("tasks")
        assert entry.is_dispatcher and entry.dispatcher_meta is not None
        assert not entry.group_tool_names, "premise: a class dispatcher has no membership"
        with patch("frisian_mcp.checks.tool_registry", reg):
            assert check_dispatcher_membership() == []

    def test_flat_tool_never_fires(self) -> None:
        """Non-dispatcher entries are out of scope entirely."""
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("item_list", lambda a, r: {}, "flat", {"type": "object"})
        with patch("frisian_mcp.checks.tool_registry", reg):
            assert check_dispatcher_membership() == []

    def test_message_names_what_is_lost_and_how_to_silence(self) -> None:
        """
        The message must say what the host LOSES, not that something is odd.

        An operator hitting a startup failure needs the consequence (negotiation
        and the lean write envelope are inert, the membership gate has nothing
        to check) and both exits: supply the field, or silence the ID.
        """
        from frisian_mcp.registry import ToolRegistry

        reg = ToolRegistry()
        self._group(reg, members=None)
        with patch("frisian_mcp.checks.tool_registry", reg):
            err = check_dispatcher_membership()[0]
        assert "group_tool_names" in err.msg
        assert "@mcp_heavy" in err.msg
        assert "global registry" in err.msg
        assert "SILENCED_SYSTEM_CHECKS" in err.hint
        assert E008_DISPATCHER_WITHOUT_MEMBERSHIP in err.hint

    def test_check_is_registered_with_django(self) -> None:
        """Registered under the security tag, so ``manage.py check`` runs it."""
        from django.core.checks import registry as checks_registry

        assert any(c is check_dispatcher_membership for c in checks_registry.registry.get_checks())


class TestW016HeavyCacheIsolation:
    """
    H6: continuation state must not share an eviction domain with security state.

    The check reports the two unseparated arrangements it can see from settings.
    It deliberately cannot prove the converse — see
    ``test_distinct_location_is_silent_even_though_it_may_still_share_an_instance``.
    """

    _DEFAULT_ONLY = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "redis://cache:6379/1",
        }
    }

    def test_unset_alias_warns_because_nothing_was_separated(self) -> None:
        """The pre-H6 arrangement every host already runs: one pool, everything in it."""
        with override_settings(CACHES=self._DEFAULT_ONLY):
            results = check_heavy_cache_isolation()
        assert len(results) == 1
        assert results[0].id == W016_HEAVY_CACHE_NOT_ISOLATED
        # The operator needs to know the exposure is reachable from outside.
        assert "unauthenticated" in results[0].msg
        assert "fails OPEN" in results[0].msg

    def test_alias_pointing_at_the_same_location_warns(self) -> None:
        """
        The exact mistake the ruling names: renaming the pool instead of dividing it.

        An alias alone is not a boundary, so a config that merely *looks*
        separated must not read as compliant.
        """
        caches_setting = dict(self._DEFAULT_ONLY)
        caches_setting["heavy"] = {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "redis://cache:6379/1",  # identical to default
        }
        with override_settings(CACHES=caches_setting, FRISIAN_MCP_HEAVY_CACHE_ALIAS="heavy"):
            results = check_heavy_cache_isolation()
        assert len(results) == 1
        assert results[0].id == W016_HEAVY_CACHE_NOT_ISOLATED
        assert "renames the pool" in results[0].msg

    def test_distinct_location_is_silent_even_though_it_may_still_share_an_instance(
        self,
    ) -> None:
        """
        Silence is NOT proof of isolation, and this test exists to say so.

        Two logical Redis DBs on one instance have distinct ``LOCATION`` strings
        and still share that instance's memory, so exhausting one takes the
        other down.  The check cannot see that from settings; the independent
        *budget* is an operator obligation.  Pinned so nobody later reads a
        silent check as a verified boundary.
        """
        caches_setting = dict(self._DEFAULT_ONLY)
        caches_setting["heavy"] = {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "redis://cache:6379/2",  # same instance, different DB
        }
        with override_settings(CACHES=caches_setting, FRISIAN_MCP_HEAVY_CACHE_ALIAS="heavy"):
            results = check_heavy_cache_isolation()
        assert results == []

    def test_check_is_registered_with_django(self) -> None:
        """Registered under the security tag, so ``manage.py check`` runs it."""
        from django.core.checks import registry as checks_registry

        assert any(c is check_heavy_cache_isolation for c in checks_registry.registry.get_checks())


class TestE009HeavyCacheAliasMissing:
    """
    H19: an alias naming no configured cache is the case where silence is wrong.

    Every other branch of :func:`check_heavy_cache_isolation` falls through
    quietly for it — ``CACHES.get(alias)`` is ``None``, so the LOCATION
    comparison never fires and the check reports clean while the runtime has
    dropped continuation state back into the cache holding OAuth codes.  The
    operator reads that silence as confirmation.
    """

    def _run(self, settings: Any, alias: str) -> list[Any]:
        settings.FRISIAN_MCP_HEAVY_CACHE_ALIAS = alias
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "default-loc",
            },
            "heavy_continuation": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "heavy-loc",
            },
        }
        return check_heavy_cache_isolation()

    def test_a_typod_alias_is_an_error_not_silence(self, settings: Any) -> None:
        """The named missing case: a configured alias that ``CACHES`` does not define."""
        results = self._run(settings, "heavy_continuations")

        assert len(results) == 1
        assert results[0].id == E009_HEAVY_CACHE_ALIAS_MISSING
        assert isinstance(results[0], Error), (
            "Warning is the wrong level here: the Warning rationale is that the "
            "unseparated arrangement predates the setting, which a typo cannot"
        )

    def test_the_error_names_the_alias_the_operator_wrote(self, settings: Any) -> None:
        """An operator scanning startup output must see their own typo, not a generic line."""
        results = self._run(settings, "hevy")

        assert "'hevy'" in results[0].msg
        assert "hevy" in results[0].hint

    def test_a_correctly_spelled_alias_is_silent(self, settings: Any) -> None:
        """The control — E009 must not fire on the arrangement it is asking for."""
        assert self._run(settings, "heavy_continuation") == []


# ---------------------------------------------------------------------------
# E010: an unrecognised FRISIAN_MCP_MAX_TIER must be loud
# ---------------------------------------------------------------------------


class TestMaxTierValueCheck:
    """
    E007's argument applied to the second tier setting.

    Route ``highest_tier`` is parser-validated; the global cap was read raw.
    The runtime fails closed but *asymmetrically*, which is what makes silence
    dangerous: anonymous read keeps working while every privileged caller is
    denied, so the symptom points away from this setting.
    """

    def test_absent_is_silent(self, settings: Any) -> None:
        """No global cap is the documented default."""
        if hasattr(settings, "FRISIAN_MCP_MAX_TIER"):
            del settings.FRISIAN_MCP_MAX_TIER
        assert check_max_tier_value() == []

    @override_settings(FRISIAN_MCP_MAX_TIER="read_write")
    def test_valid_tier_is_silent(self) -> None:
        """A recognised tier raises nothing."""
        assert check_max_tier_value() == []

    @override_settings(FRISIAN_MCP_MAX_TIER="readwrite")
    def test_typo_raises_error(self) -> None:
        """A typo is an Error, matching E007's treatment of the sibling setting."""
        errors = check_max_tier_value()
        assert len(errors) == 1
        assert errors[0].id == E010_INVALID_MAX_TIER
        assert isinstance(errors[0], Error)

    @override_settings(FRISIAN_MCP_MAX_TIER="readwrite")
    def test_message_names_the_asymmetry(self) -> None:
        """
        The message must describe the *asymmetric* consequence.

        An operator seeing "admins denied, anonymous fine" would not otherwise
        suspect a global cap, so the message says exactly that.
        """
        err = check_max_tier_value()[0]
        assert "readwrite" in err.msg
        assert "DENIED" in err.msg
        assert "'read' callers are unaffected" in err.msg

    def test_runtime_really_is_asymmetric(self) -> None:
        """
        Pins the behaviour the message describes, so the two cannot drift.

        Asserted on the rank rather than end-to-end: ranking is what every gate
        compares, and the clamp returns the invalid string itself.
        """
        from unittest.mock import MagicMock

        from frisian_mcp.registry import _apply_max_tier_cap, _caller_rank

        req = MagicMock(_mcp_max_tier="reed")
        assert _apply_max_tier_cap("read", req) == "read"
        assert _caller_rank(_apply_max_tier_cap("read_write", req)) < _caller_rank("read")
        assert _caller_rank(_apply_max_tier_cap("admin", req)) < _caller_rank("read")

    @override_settings(FRISIAN_MCP_MAX_TIER="  READ_WRITE  ")
    def test_non_canonical_value_is_accepted_by_both_check_and_runtime(self) -> None:
        """
        The check must not bless a value the runtime rejects (CodeRabbit).

        This check normalised with ``strip().lower()`` while the request stamp
        used the raw value, so ``"  READ_WRITE  "`` passed here and then denied
        every privileged caller at runtime.  A control that *certifies* a broken
        config is worse than one that stays silent, and this was that control.

        Both sides now share ``normalize_tier_setting``, so agreement is
        structural rather than a coincidence of two matching expressions.
        """
        from unittest.mock import MagicMock

        from frisian_mcp.registry import _apply_max_tier_cap, _caller_rank
        from frisian_mcp.views import McpView

        assert check_max_tier_value() == []
        stamped = McpView()._effective_max_tier()  # noqa: SLF001
        capped = _apply_max_tier_cap("admin", MagicMock(_mcp_max_tier=stamped))
        assert _caller_rank(capped) == _caller_rank(
            "read_write"
        ), "the check accepted this value; the runtime must too"

    @override_settings(FRISIAN_MCP_MAX_TIER="readwrite")
    def test_a_real_typo_still_denies_and_still_fires(self) -> None:
        """Normalising must not turn a genuine typo into a silent pass."""
        from unittest.mock import MagicMock

        from frisian_mcp.registry import _apply_max_tier_cap, _caller_rank
        from frisian_mcp.views import McpView

        assert len(check_max_tier_value()) == 1
        stamped = McpView()._effective_max_tier()  # noqa: SLF001
        capped = _apply_max_tier_cap("admin", MagicMock(_mcp_max_tier=stamped))
        assert _caller_rank(capped) < _caller_rank("read")
