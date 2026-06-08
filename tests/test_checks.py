"""
Django system check tests for frisian-mcp configuration safety.

W001 — FRISIAN_MCP_PERMISSION_CLASSES empty in production.
W002 — FRISIAN_MCP_API_KEYS contains unhashed (plaintext) keys.
W003 — FRISIAN_MCP_SERVICE_ACCOUNT_USER set in production.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings

from frisian_mcp.checks import (
    W001_NO_PERMISSION_CLASSES,
    W002_PLAINTEXT_API_KEYS,
    W003_PRIVILEGED_SERVICE_ACCOUNT,
    check_api_keys_are_hashed,
    check_permission_classes_in_production,
    check_service_account_user,
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
    def test_warning_message_mentions_frisian_mcp_setting(
        self, settings: Any
    ) -> None:
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
        """64-char string with uppercase hex is treated as not a valid digest (digest is lowercase)."""
        settings.FRISIAN_MCP_API_KEYS = {"A" * 64: "read"}
        warnings = check_api_keys_are_hashed()
        assert len(warnings) == 1

    def test_hint_mentions_management_command(self, settings: Any) -> None:
        """Hint directs the operator to mcp_hash_api_key."""
        settings.FRISIAN_MCP_API_KEYS = {"bad-key": "read"}
        warnings = check_api_keys_are_hashed()
        assert "mcp_hash_api_key" in warnings[0].hint


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
