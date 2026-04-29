"""Management command: audit the host's friese-mcp integration and report issues."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

_OK = "✓"
_WARN = "⚠"
_ERR = "✗"


class Command(BaseCommand):
    """
    Audit the host's friese-mcp configuration and surface integration issues.

    Checks gateway installation, URL mounting, contrib app wiring, security
    settings, and performance hints. Exits non-zero if any errors are found.
    """

    help = "Audit friese-mcp configuration and report integration issues."

    def handle(self, *args: Any, **options: Any) -> None:
        """Run all checks and print a summary."""
        errors: list[str] = []
        warnings: list[str] = []

        self._check_installed_apps(errors)
        self._check_url_mounting(warnings)
        self._check_auth_wiring(warnings)
        self._check_security_settings(warnings)
        self._check_performance_hints(warnings)
        self._check_oauth_registration(warnings)

        self.stdout.write("")
        if errors:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(errors)} error(s) found — integration will not work correctly."
                )
            )
        elif warnings:
            self.stdout.write(
                self.style.WARNING(f"No errors. {len(warnings)} warning(s) to review.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("All checks passed. friese-mcp looks healthy."))
        self.stdout.write("")

        if errors:
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_installed_apps(self, errors: list[str]) -> None:
        """Verify friese_mcp and any installed contrib apps are consistent."""
        installed: list[str] = list(getattr(settings, "INSTALLED_APPS", []))

        if "friese_mcp" not in installed:
            self._fail(errors, "friese_mcp is not in INSTALLED_APPS")
        else:
            self._ok("friese_mcp in INSTALLED_APPS")

        tokens = "friese_mcp.contrib.tokens" in installed
        oauth = "friese_mcp.contrib.oauth" in installed
        agents = "friese_mcp.contrib.agents" in installed

        for app, present in [
            ("contrib.tokens", tokens),
            ("contrib.oauth", oauth),
            ("contrib.agents", agents),
        ]:
            if present:
                self._ok(f"friese_mcp.{app} in INSTALLED_APPS")
            else:
                self._warn(
                    f"friese_mcp.{app} not installed — optional; add if you need its features"
                )

        if agents and not tokens:
            self._fail(
                errors,
                "contrib.agents requires contrib.tokens in INSTALLED_APPS (AgentConnection FK)",
            )

    def _check_url_mounting(self, warnings: list[str]) -> None:
        """Check that the MCP gateway is reachable in the URL configuration."""
        try:
            from django.urls import reverse  # pylint: disable=import-outside-toplevel

            url = reverse("friese_mcp:gateway")
            self._ok(f"MCP gateway mounted at {url}")
        except Exception:  # pylint: disable=broad-exception-caught
            self._warn_msg(
                warnings,
                "Could not resolve friese_mcp:gateway — add"
                " path('mcp/', include('friese_mcp.urls')) to your ROOT_URLCONF",
            )

        try:
            from django.urls import reverse  # pylint: disable=import-outside-toplevel

            reverse("friese_mcp_oauth_wellknown:oauth_authorization_server")
            self._ok("OAuth .well-known URLs mounted")
        except Exception:  # pylint: disable=broad-exception-caught
            self._warn_msg(
                warnings,
                "OAuth .well-known URLs not mounted — add"
                " path('.well-known/', include('friese_mcp.contrib.oauth.wellknown_urls'))"
                " for Claude.ai-compatible auto-discovery",
            )

    def _check_auth_wiring(self, warnings: list[str]) -> None:
        """Check authentication and permission class wiring."""
        auth_classes: list[str] = list(
            getattr(settings, "FRIESE_MCP_AUTHENTICATION_CLASSES", [])
        )
        perm_classes: list[str] = list(
            getattr(settings, "FRIESE_MCP_PERMISSION_CLASSES", [])
        )

        tokens_installed = "friese_mcp.contrib.tokens" in getattr(settings, "INSTALLED_APPS", [])
        oauth_installed = "friese_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])

        token_auth = (
            "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication"  # noqa: S105
        )
        oauth_auth = "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication"

        if tokens_installed and token_auth not in auth_classes:
            self._warn_msg(
                warnings,
                "contrib.tokens is installed but FrieseMcpTokenAuthentication is not in"
                " FRIESE_MCP_AUTHENTICATION_CLASSES — static Bearer tokens will not authenticate",
            )
        elif tokens_installed:
            self._ok("FrieseMcpTokenAuthentication wired in FRIESE_MCP_AUTHENTICATION_CLASSES")

        if oauth_installed and oauth_auth not in auth_classes:
            self._warn_msg(
                warnings,
                "contrib.oauth is installed but OAuthTokenAuthentication is not in"
                " FRIESE_MCP_AUTHENTICATION_CLASSES — OAuth tokens will not authenticate",
            )
        elif oauth_installed:
            self._ok("OAuthTokenAuthentication wired in FRIESE_MCP_AUTHENTICATION_CLASSES")

        if not auth_classes and not perm_classes:
            self._ok(
                "Auth classes empty — gateway is open (intentional for demo/internal use)"
            )

    def _check_security_settings(self, warnings: list[str]) -> None:
        """Check security-relevant settings."""
        debug: bool = getattr(settings, "DEBUG", False)
        if debug:
            self._warn_msg(
                warnings,
                "DEBUG=True — ensure this is not a production deployment",
            )
        else:
            self._ok("DEBUG=False")

        hmac_key: str | None = getattr(settings, "FRIESE_MCP_HMAC_KEY", None)
        if hmac_key:
            self._ok("FRIESE_MCP_HMAC_KEY set — token HMACs are independent of SECRET_KEY")
        else:
            self._warn_msg(
                warnings,
                "FRIESE_MCP_HMAC_KEY not set — token HMACs fall back to SECRET_KEY."
                " Rotating SECRET_KEY will invalidate all issued tokens."
                " Set FRIESE_MCP_HMAC_KEY to decouple them.",
            )

        proxy_count: int = getattr(settings, "FRIESE_MCP_TRUSTED_PROXY_COUNT", 0)
        if not debug and proxy_count == 0:
            self._warn_msg(
                warnings,
                "FRIESE_MCP_TRUSTED_PROXY_COUNT=0 in non-debug mode — if nginx or another"
                " proxy sits in front of Django, set this to the number of trusted proxies"
                " so X-Forwarded-For IP resolution and OAuth issuer URLs are correct",
            )
        elif proxy_count > 0:
            self._ok(f"FRIESE_MCP_TRUSTED_PROXY_COUNT={proxy_count}")

    def _check_performance_hints(self, warnings: list[str]) -> None:
        """Check performance-related settings against the registered tool count."""
        from friese_mcp.registry import tool_registry  # pylint: disable=import-outside-toplevel

        try:
            tool_count = len(tool_registry.list_tools())
        except Exception:  # pylint: disable=broad-exception-caught
            tool_count = 0

        if tool_count:
            self._ok(f"{tool_count} tool(s) registered")

        page_size: int | None = getattr(settings, "FRIESE_MCP_TOOLS_PAGE_SIZE", None)
        if tool_count > 80 and page_size is None:
            self._warn_msg(
                warnings,
                f"{tool_count} tools registered and FRIESE_MCP_TOOLS_PAGE_SIZE is unset —"
                " tools/list returns the full manifest in one response."
                " Consider setting FRIESE_MCP_TOOLS_PAGE_SIZE to ~50 to enable cursor pagination",
            )
        elif page_size:
            self._ok(f"FRIESE_MCP_TOOLS_PAGE_SIZE={page_size}")

        cache_ttl: int | None = getattr(settings, "FRIESE_MCP_TOOLS_LIST_CACHE_TTL", None)
        if tool_count > 80 and cache_ttl is None:
            self._warn_msg(
                warnings,
                f"{tool_count} tools registered and FRIESE_MCP_TOOLS_LIST_CACHE_TTL is unset —"
                " each tools/list call rebuilds the manifest from scratch."
                " Set FRIESE_MCP_TOOLS_LIST_CACHE_TTL (seconds) to cache it"
                " in Django's cache backend",
            )
        elif cache_ttl:
            self._ok(f"FRIESE_MCP_TOOLS_LIST_CACHE_TTL={cache_ttl}s")

    def _check_oauth_registration(self, warnings: list[str]) -> None:
        """Warn when OAuth registration is closed — blocks agent self-bootstrap."""
        oauth_installed = "friese_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return

        reg_open: bool = getattr(settings, "FRIESE_MCP_OAUTH_REGISTRATION_OPEN", False)
        if reg_open:
            self._ok("FRIESE_MCP_OAUTH_REGISTRATION_OPEN=True — agents can self-register")
        else:
            self._warn_msg(
                warnings,
                "FRIESE_MCP_OAUTH_REGISTRATION_OPEN=False — agents cannot self-register."
                " Discovering agents (e.g. Claude.ai) will see no registration_endpoint in"
                " the .well-known metadata and must use pre-provisioned credentials."
                " Set to True if you want end-to-end agent autodiscovery.",
            )

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _ok(self, message: str) -> None:
        self.stdout.write(f"  {self.style.SUCCESS(_OK)} {message}")

    def _warn(self, message: str) -> None:
        self.stdout.write(f"  {self.style.WARNING(_WARN)} {message}")

    def _warn_msg(self, warnings: list[str], message: str) -> None:
        warnings.append(message)
        self._warn(message)

    def _fail(self, errors: list[str], message: str) -> None:
        errors.append(message)
        self.stdout.write(f"  {self.style.ERROR(_ERR)} {message}")
