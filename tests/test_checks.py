"""
SEC-4 — Django system check for empty FRIESE_MCP_PERMISSION_CLASSES in production.

The check (``friese_mcp.W001``) runs when ``DEBUG=False`` and refuses to be
silent about an unauthenticated MCP gateway.  Operators who want an open
gateway must set ``FRIESE_MCP_ALLOW_UNAUTHENTICATED=True`` to acknowledge
the trade-off explicitly.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings

from friese_mcp.checks import (
    W001_NO_PERMISSION_CLASSES,
    check_permission_classes_in_production,
)

# ---------------------------------------------------------------------------
# DEBUG=False, no permission classes → warning
# ---------------------------------------------------------------------------


class TestProductionMisconfiguration:
    """The W001 warning fires when DEBUG=False AND PERMISSION_CLASSES is empty."""

    @override_settings(DEBUG=False, FRIESE_MCP_PERMISSION_CLASSES=[])
    def test_warning_fires_when_setting_is_empty_list(self, settings: Any) -> None:
        """Explicit ``[]`` in production is the misconfigured case."""
        if hasattr(settings, "FRIESE_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRIESE_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert len(warnings) == 1
        assert warnings[0].id == W001_NO_PERMISSION_CLASSES

    @override_settings(DEBUG=False)
    def test_warning_fires_when_setting_is_absent(self, settings: Any) -> None:
        """An absent setting is treated identically to ``[]``."""
        for attr in ("FRIESE_MCP_PERMISSION_CLASSES", "FRIESE_MCP_ALLOW_UNAUTHENTICATED"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        warnings = check_permission_classes_in_production()
        assert len(warnings) == 1
        assert warnings[0].id == W001_NO_PERMISSION_CLASSES

    @override_settings(DEBUG=False, FRIESE_MCP_PERMISSION_CLASSES=[])
    def test_warning_message_mentions_friese_mcp_setting(
        self, settings: Any
    ) -> None:
        """The warning message names the setting so operators can find it."""
        if hasattr(settings, "FRIESE_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRIESE_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert "FRIESE_MCP_PERMISSION_CLASSES" in warnings[0].msg

    @override_settings(DEBUG=False, FRIESE_MCP_PERMISSION_CLASSES=[])
    def test_warning_hint_mentions_opt_in(self, settings: Any) -> None:
        """The hint shows the opt-out / opt-in path explicitly."""
        if hasattr(settings, "FRIESE_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRIESE_MCP_ALLOW_UNAUTHENTICATED
        warnings = check_permission_classes_in_production()
        assert "FRIESE_MCP_ALLOW_UNAUTHENTICATED" in warnings[0].hint


# ---------------------------------------------------------------------------
# Silenced cases
# ---------------------------------------------------------------------------


class TestSilencedScenarios:
    """The check stays quiet for legitimate configurations."""

    @override_settings(DEBUG=True, FRIESE_MCP_PERMISSION_CLASSES=[])
    def test_silent_in_debug_mode(self) -> None:
        """Developers running runserver should not get nagged."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRIESE_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"],
    )
    def test_silent_when_classes_configured(self) -> None:
        """A non-empty list is the supported production shape."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_ALLOW_UNAUTHENTICATED=True,
    )
    def test_silent_when_explicit_opt_in_set(self) -> None:
        """Operators who deliberately want an open gateway opt in to silence."""
        assert not check_permission_classes_in_production()

    @override_settings(
        DEBUG=False,
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_ALLOW_UNAUTHENTICATED=False,
    )
    def test_explicit_false_does_not_silence(self) -> None:
        """``FRIESE_MCP_ALLOW_UNAUTHENTICATED=False`` is the same as not set."""
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

    @override_settings(DEBUG=False, FRIESE_MCP_PERMISSION_CLASSES=[])
    def test_run_checks_surfaces_w001(self, settings: Any) -> None:
        """W001 appears in run_checks() output when configured insecurely."""
        from django.core.checks import (  # pylint: disable=import-outside-toplevel
            run_checks,
        )

        if hasattr(settings, "FRIESE_MCP_ALLOW_UNAUTHENTICATED"):
            del settings.FRIESE_MCP_ALLOW_UNAUTHENTICATED
        results = run_checks()
        ids = {w.id for w in results if hasattr(w, "id")}
        assert W001_NO_PERMISSION_CLASSES in ids
