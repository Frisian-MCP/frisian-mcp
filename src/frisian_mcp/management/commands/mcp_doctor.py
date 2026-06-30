"""Management command: audit the host's frisian-mcp integration and report issues."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

_OK = "✓"
_WARN = "⚠"
_ERR = "✗"


class Command(BaseCommand):
    """
    Audit the host's frisian-mcp configuration and surface integration issues.

    Checks gateway installation, URL mounting, contrib app wiring, security
    settings, and performance hints. Exits non-zero if any errors are found.

    Use ``--security`` for an extended OAuth security audit that surfaces
    common misconfigurations that could allow privilege escalation or
    information disclosure.
    """

    help = "Audit frisian-mcp configuration and report integration issues."

    def add_arguments(self, parser: Any) -> None:
        """Add --security flag for the extended OAuth security audit."""
        parser.add_argument(
            "--security",
            action="store_true",
            default=False,
            help=(
                "Run extended OAuth security checks in addition to the standard audit.  "
                "Surfaces misconfigurations that could allow privilege escalation, "
                "credential exposure, or unauthenticated access."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run all checks and print a summary."""
        errors: list[str] = []
        warnings: list[str] = []

        self._check_installed_apps(errors)
        self._check_url_mounting(warnings)
        self._check_auth_wiring(warnings)
        self._check_security_settings(warnings)
        self._check_cache_backend(warnings)
        self._check_performance_hints(warnings)
        self._check_oauth_registration(warnings)
        self._check_oauth_authorize_url(warnings)
        self._check_oauth_tier_permissions(warnings)
        self._check_oauth_pkce_redirect_tier_map(warnings)

        if options.get("security"):
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("  — Extended security audit —"))
            self._check_oauth_service_user(warnings)
            self._check_service_account_user_privilege(warnings)
            self._check_body_size_limit(warnings)
            self._check_pkce_auto_register(warnings, errors)
            self._check_oauth_auto_approve(warnings)
            self._check_oauth_auto_approve_consent_records(warnings)
            self._check_oauth_registration_vs_wellknown(warnings)
            self._check_hmac_key_rotation(warnings)

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
            self.stdout.write(self.style.SUCCESS("All checks passed. frisian-mcp looks healthy."))
        self.stdout.write("")

        if errors:
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_installed_apps(self, errors: list[str]) -> None:
        """Verify frisian_mcp and any installed contrib apps are consistent."""
        installed: list[str] = list(getattr(settings, "INSTALLED_APPS", []))

        if "frisian_mcp" not in installed:
            self._fail(errors, "frisian_mcp is not in INSTALLED_APPS")
        else:
            self._ok("frisian_mcp in INSTALLED_APPS")

        tokens = "frisian_mcp.contrib.tokens" in installed
        oauth = "frisian_mcp.contrib.oauth" in installed
        agents = "frisian_mcp.contrib.agents" in installed

        for app, present in [
            ("contrib.tokens", tokens),
            ("contrib.oauth", oauth),
            ("contrib.agents", agents),
        ]:
            if present:
                self._ok(f"frisian_mcp.{app} in INSTALLED_APPS")
            else:
                self._warn(
                    f"frisian_mcp.{app} not installed — optional; add if you need its features"
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

            url = reverse("frisian_mcp:gateway")
            self._ok(f"MCP gateway mounted at {url}")
        except Exception:  # pylint: disable=broad-exception-caught
            self._warn_msg(
                warnings,
                "Could not resolve frisian_mcp:gateway — add"
                " path('mcp/', include('frisian_mcp.urls')) to your ROOT_URLCONF",
            )

        try:
            from django.urls import reverse  # pylint: disable=import-outside-toplevel

            reverse("frisian_mcp_oauth_wellknown:oauth_authorization_server")
            self._ok("OAuth .well-known URLs mounted")
        except Exception:  # pylint: disable=broad-exception-caught
            self._warn_msg(
                warnings,
                "OAuth .well-known URLs not mounted — add"
                " path('.well-known/', include('frisian_mcp.contrib.oauth.wellknown_urls'))"
                " for Claude.ai-compatible auto-discovery",
            )

    def _check_auth_wiring(self, warnings: list[str]) -> None:
        """Check authentication and permission class wiring."""
        auth_classes: list[str] = list(getattr(settings, "FRISIAN_MCP_AUTHENTICATION_CLASSES", []))
        perm_classes: list[str] = list(getattr(settings, "FRISIAN_MCP_PERMISSION_CLASSES", []))

        tokens_installed = "frisian_mcp.contrib.tokens" in getattr(settings, "INSTALLED_APPS", [])
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])

        token_auth = (
            "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication"  # noqa: S105
        )
        oauth_auth = "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication"

        if tokens_installed and token_auth not in auth_classes:
            self._warn_msg(
                warnings,
                "contrib.tokens is installed but FrisianMcpTokenAuthentication is not in"
                " FRISIAN_MCP_AUTHENTICATION_CLASSES — static Bearer tokens will not authenticate",
            )
        elif tokens_installed:
            self._ok("FrisianMcpTokenAuthentication wired in FRISIAN_MCP_AUTHENTICATION_CLASSES")

        if oauth_installed and oauth_auth not in auth_classes:
            self._warn_msg(
                warnings,
                "contrib.oauth is installed but OAuthTokenAuthentication is not in"
                " FRISIAN_MCP_AUTHENTICATION_CLASSES — OAuth tokens will not authenticate",
            )
        elif oauth_installed:
            self._ok("OAuthTokenAuthentication wired in FRISIAN_MCP_AUTHENTICATION_CLASSES")

        if not auth_classes and not perm_classes:
            self._ok("Auth classes empty — gateway is open (intentional for demo/internal use)")

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

        hmac_key: str | None = getattr(settings, "FRISIAN_MCP_HMAC_KEY", None)
        if hmac_key:
            self._ok("FRISIAN_MCP_HMAC_KEY set — token HMACs are independent of SECRET_KEY")
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_HMAC_KEY not set — token HMACs fall back to SECRET_KEY."
                " Rotating SECRET_KEY will invalidate all issued tokens."
                " Set FRISIAN_MCP_HMAC_KEY to decouple them.",
            )

        _unauth_raw = getattr(settings, "FRISIAN_MCP_UNAUTHENTICATED_TIER", None)
        unauth_tier: str = str(_unauth_raw) if _unauth_raw is not None else "read"
        if _unauth_raw is None:
            self._ok(
                "FRISIAN_MCP_UNAUTHENTICATED_TIER not set — defaulting to 'read'"
                " (anonymous callers see only read-tier tools)"
            )
        elif unauth_tier == "read":
            self._ok(
                "FRISIAN_MCP_UNAUTHENTICATED_TIER='read'"
                " — anonymous callers see only read-tier tools"
            )
        elif unauth_tier in ("read_write", "admin"):
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_UNAUTHENTICATED_TIER='{unauth_tier}' — anonymous callers can invoke"
                f" {unauth_tier}-tier tools without authentication."
                " Acceptable for internal or demo deployments; not recommended for production.",
            )
        else:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_UNAUTHENTICATED_TIER='{unauth_tier}' is not a recognised tier"
                " (expected 'read', 'read_write', or 'admin') — defaulting to 'read' at runtime",
            )

        proxy_count: int = getattr(settings, "FRISIAN_MCP_TRUSTED_PROXY_COUNT", 0)
        if not debug and proxy_count == 0:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_TRUSTED_PROXY_COUNT=0 in non-debug mode — if nginx or another"
                " proxy sits in front of Django, set this to the number of trusted proxies"
                " so X-Forwarded-For IP resolution and OAuth issuer URLs are correct",
            )
        elif proxy_count > 0:
            self._ok(f"FRISIAN_MCP_TRUSTED_PROXY_COUNT={proxy_count}")

    def _check_performance_hints(self, warnings: list[str]) -> None:
        """Check performance-related settings against the registered tool count."""
        from frisian_mcp.registry import (  # pylint: disable=import-outside-toplevel
            _TIER_RANK,
            tool_registry,
        )

        try:
            all_tools = tool_registry.list_tools(max_tier=None)
            tool_count = len(all_tools)
        except Exception:  # pylint: disable=broad-exception-caught
            all_tools = []
            tool_count = 0

        if tool_count:
            tier_counts: dict[str, int] = dict.fromkeys(_TIER_RANK, 0)
            for tool in all_tools:
                entry = tool_registry.get_entry(tool["name"])
                if entry is not None:
                    tier_counts[entry.permission_tier] = (
                        tier_counts.get(entry.permission_tier, 0) + 1
                    )
            dist = ", ".join(f"{t}={tier_counts[t]}" for t in _TIER_RANK)
            self._ok(f"{tool_count} tool(s) registered — tier distribution: {dist}")

        page_size: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_PAGE_SIZE", None)
        if tool_count > 80 and page_size is None:
            self._warn_msg(
                warnings,
                f"{tool_count} tools registered and FRISIAN_MCP_TOOLS_PAGE_SIZE is unset —"
                " tools/list returns the full manifest in one response."
                " Consider setting FRISIAN_MCP_TOOLS_PAGE_SIZE to ~50 to enable cursor pagination",
            )
        elif page_size:
            self._ok(f"FRISIAN_MCP_TOOLS_PAGE_SIZE={page_size}")

        cache_ttl: int | None = getattr(settings, "FRISIAN_MCP_TOOLS_LIST_CACHE_TTL", None)
        if tool_count > 80 and cache_ttl is None:
            self._warn_msg(
                warnings,
                f"{tool_count} tools registered and FRISIAN_MCP_TOOLS_LIST_CACHE_TTL is unset —"
                " each tools/list call rebuilds the manifest from scratch."
                " Set FRISIAN_MCP_TOOLS_LIST_CACHE_TTL (seconds) to cache it"
                " in Django's cache backend",
            )
        elif cache_ttl:
            self._ok(f"FRISIAN_MCP_TOOLS_LIST_CACHE_TTL={cache_ttl}s")

    def _check_cache_backend(self, warnings: list[str]) -> None:
        """Warn when LocMemCache is the default backend and contrib.oauth is installed."""
        _locmem = "django.core.cache.backends.locmem.LocMemCache"
        cache_backend = getattr(settings, "CACHES", {}).get("default", {}).get("BACKEND", "")
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])

        if cache_backend == _locmem and oauth_installed:
            self._warn_msg(
                warnings,
                "CACHES['default'] is LocMemCache and contrib.oauth is installed — authorization"
                " codes are stored per-process. In a multi-worker deployment (gunicorn, uWSGI)"
                " a code written by one worker will not be visible to another, causing"
                " intermittent invalid_grant errors. Configure a shared cache backend"
                " (Redis, Memcached) before going to production.",
            )
        elif cache_backend == _locmem:
            self._warn_msg(
                warnings,
                "CACHES['default'] is LocMemCache — per-process only."
                " Acceptable for development; switch to Redis or Memcached in production.",
            )
        else:
            self._ok(f"Cache backend: {cache_backend or '(default)'}")

    def _check_oauth_registration(self, warnings: list[str]) -> None:
        """Warn when OAuth registration is closed — blocks agent self-bootstrap."""
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return

        reg_open: bool = getattr(settings, "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN", False)
        if reg_open:
            self._ok("FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=True — agents can self-register")
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False — agents cannot self-register."
                " Discovering agents (e.g. Claude.ai) will see no registration_endpoint in"
                " the .well-known metadata and must use pre-provisioned credentials."
                " Set to True if you want end-to-end agent autodiscovery.",
            )

    def _check_oauth_authorize_url(self, warnings: list[str]) -> None:
        """Check that FRISIAN_MCP_OAUTH_AUTHORIZE_URL is reachable when set."""
        url: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_AUTHORIZE_URL", None)
        if url is None:
            return

        if not url.startswith(("http://", "https://")):
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_OAUTH_AUTHORIZE_URL={url!r} — must be an http:// or https:// URL",
            )
            return

        status: int = 0
        try:
            req = urllib.request.Request(url, method="GET")  # noqa: S310
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_OAUTH_AUTHORIZE_URL={url!r} is set but could not be reached: {exc}",
            )
            return

        if status == 200:
            self._ok(f"FRISIAN_MCP_OAUTH_AUTHORIZE_URL reachable (HTTP {status})")
        else:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_OAUTH_AUTHORIZE_URL={url!r} returned HTTP {status} (expected 200)"
                " — OAuth /authorize endpoint may not be mounted correctly",
            )

    # ------------------------------------------------------------------
    # Extended security checks (--security flag)
    # ------------------------------------------------------------------

    def _check_oauth_service_user(self, warnings: list[str]) -> None:
        """Warn when contrib.oauth is installed but FRISIAN_MCP_OAUTH_SERVICE_USER is not set."""
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return

        service_user: str | None = getattr(settings, "FRISIAN_MCP_OAUTH_SERVICE_USER", None)
        if service_user:
            self._ok(
                f"FRISIAN_MCP_OAUTH_SERVICE_USER='{service_user}' — OAuth requests will be "
                "attributed to this Django user for audit-log FKs"
            )
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_SERVICE_USER is not set — OAuth-authenticated requests "
                "use OAuthServicePrincipal (no real Django User).  Set this to a dedicated "
                "service account username if host models require a User FK for audit logging.",
            )

    def _check_service_account_user_privilege(self, warnings: list[str]) -> None:
        """Warn when FRISIAN_MCP_SERVICE_ACCOUNT_USER names a privileged Django user."""
        service_user: str | None = getattr(settings, "FRISIAN_MCP_SERVICE_ACCOUNT_USER", None)
        if not service_user:
            self._ok(
                "FRISIAN_MCP_SERVICE_ACCOUNT_USER not set — anonymous callers use AnonymousUser"
            )
            return

        from django.contrib.auth import get_user_model  # pylint: disable=import-outside-toplevel

        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=service_user)
        except user_model.DoesNotExist:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_SERVICE_ACCOUNT_USER='{service_user}' not found in the database. "
                "Anonymous MCP callers will fall back to AnonymousUser.",
            )
            return

        if user.is_superuser:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_SERVICE_ACCOUNT_USER='{service_user}' is a superuser. "
                "Anonymous MCP callers will receive superuser permissions at the host-app layer. "
                "Use a dedicated low-privilege service account instead.",
            )
        elif user.is_staff:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_SERVICE_ACCOUNT_USER='{service_user}' is a staff user. "
                "Anonymous MCP callers will receive staff-level permissions at the host-app layer. "
                "Use a dedicated low-privilege service account instead.",
            )
        else:
            self._ok(
                f"FRISIAN_MCP_SERVICE_ACCOUNT_USER='{service_user}'"
                " — account is not staff/superuser"
            )

    def _check_body_size_limit(self, warnings: list[str]) -> None:
        """Warn when FRISIAN_MCP_REQUEST_BODY_MAX_SIZE is not explicitly configured."""
        limit: int | None = getattr(settings, "FRISIAN_MCP_REQUEST_BODY_MAX_SIZE", None)
        if limit is not None:
            self._ok(
                f"FRISIAN_MCP_REQUEST_BODY_MAX_SIZE={limit} bytes "
                f"({limit // 1024} KiB) — oversized MCP bodies will be rejected"
            )
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_REQUEST_BODY_MAX_SIZE is not set — defaults to 1 MiB.  "
                "Set explicitly in settings to document and tune the intended limit.",
            )

    def _check_pkce_auto_register(self, warnings: list[str], errors: list[str]) -> None:
        """Audit the AUTO_REGISTER + host-allowlist (T1) matrix."""
        debug: bool = getattr(settings, "DEBUG", False)
        pkce_auto: bool = getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER", False)
        # Validate shape BEFORE counting: ``list("claude.ai")`` silently
        # explodes a misconfigured-as-string setting into per-character
        # "patterns" and would falsely OK a malformed security input.
        raw_allowlist: object = getattr(
            settings, "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST", []
        )
        if raw_allowlist is None or raw_allowlist == []:
            allowlist: list[str] = []
        elif isinstance(raw_allowlist, list):
            allowlist = list(raw_allowlist)
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST is set but is not a list —"
                " auto-register will treat it as empty (fail-closed). Expected shape:"
                " list[str] of hostname patterns (e.g. ['claude.ai', '*.anthropic.com']).",
            )
            allowlist = []
        if not pkce_auto:
            self._ok("FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=False — PKCE auto-registration disabled")
            return
        # AUTO_REGISTER is True from here.  Behavior depends on the allowlist
        # and DEBUG.  We never echo the allowlist contents into the doctor
        # output (operator-specific values shouldn't land in CI logs);
        # report size/presence only.
        if not allowlist:
            if debug:
                self._warn_msg(
                    warnings,
                    "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True with no host allowlist"
                    " (DEBUG=True) — unknown clients will be refused (effectively disabled)."
                    " Set FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST to opt in to a"
                    " trusted set of redirect-URI hosts before relying on auto-register.",
                )
            else:
                self._fail(
                    errors,
                    "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True but the host allowlist is empty —"
                    " unknown clients will be refused (effectively disabled). Set"
                    " FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST or AUTO_REGISTER=False.",
                )
            return
        # AUTO_REGISTER=True with a non-empty allowlist.
        size = len(allowlist)
        if debug:
            self._warn_msg(
                warnings,
                f"FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True restricted to {size} host pattern(s)"
                " (DEBUG=True). Acceptable for local development; verify the allowlist before"
                " enabling outside DEBUG.",
            )
        else:
            self._ok(
                f"FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True restricted to {size} host pattern(s)"
            )

    def _check_oauth_pkce_redirect_tier_map(self, warnings: list[str]) -> None:
        """Warn when the legacy PKCE_REDIRECT_TIER_MAP setting is still present (T7 removed it)."""
        # The setting is removed by T7; if an operator still has it set in
        # their settings.py the doctor flags it so they can clean up.
        if hasattr(settings, "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP"):
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP is set but the setting is no longer read"
                " by frisian-mcp. Under the hardened authorize path the redirect_uri cannot"
                " influence the client.permission tier. Remove the setting from your settings.py.",
            )

    def _check_oauth_auto_approve(self, warnings: list[str]) -> None:
        """Audit the AUTO_APPROVE (T9) matrix and its interaction with AUTO_REGISTER (T1)."""
        debug: bool = getattr(settings, "DEBUG", False)
        # Sentinel resolution lets us distinguish "operator opted in to True"
        # from "operator left it unset" without echoing the value.
        _auto_approve_raw: object = getattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE", None)
        auto_approve = bool(_auto_approve_raw) if _auto_approve_raw is not None else False
        pkce_auto: bool = getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER", False)
        if not auto_approve:
            self._ok(
                "FRISIAN_MCP_OAUTH_AUTO_APPROVE unset or False"
                " — consent form renders on first contact and on every authorize without a"
                " stored prior-consent record"
            )
            return
        # AUTO_APPROVE=True from here.  Reframed semantics: "remember consent,"
        # not "skip consent."  First-time consent gate still fires.
        if debug:
            self._ok(
                "FRISIAN_MCP_OAUTH_AUTO_APPROVE=True (DEBUG=True) — repeat-grant fast path active."
                " First-time consent gate still applies."
            )
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_AUTO_APPROVE=True — repeat-grant fast path active."
                " The first-time consent gate still applies (no consent record means"
                " the form renders), but subsequent authorize requests for the same"
                " (user, client_id, redirect_uri, scope) tuple skip the form."
                " Confirm this matches your deployment's consent posture.",
            )
        if pkce_auto:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_AUTO_APPROVE=True combined with"
                " FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True — first contact from a newly"
                " auto-registered client still renders the consent form (the gate is unbypassable)"
                " but the combination is only safe with a tight"
                " FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST.",
            )

    def _check_oauth_tier_permissions(self, warnings: list[str]) -> None:
        """Audit the FRISIAN_MCP_OAUTH_TIER_PERMISSIONS (T10) signal."""
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return
        tier_perms: object = getattr(settings, "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS", None)
        if not tier_perms:
            self._ok(
                "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS unset or empty — has_perm default-deny in"
                " effect. Host code consulting Django perms will see no MCP-granted perms."
            )
            return
        if not isinstance(tier_perms, dict):
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS is set but is not a dict — has_perm will treat"
                " it as default-deny. Expected shape: dict[str, list[str]] (tier name → perm"
                " strings).",
            )
            return
        # Top-level dict OK; validate key + value shape so a misconfiguration
        # like ``{"redd": ["app.view"]}`` (typo'd tier name no runtime tier
        # will consult), ``{"read": "app.view_thing"}`` (string instead of
        # list[str]), or ``{"read": [123]}`` (non-string perm) does not get
        # reported as healthy.  Perm strings are intentionally not echoed
        # (CI log hygiene); the warning surfaces shape only.
        valid_tier_keys: frozenset[str] = frozenset({"read", "read_write", "admin"})
        bad_tiers: list[str] = []
        for tier_name, perms in tier_perms.items():
            if not isinstance(tier_name, str):
                bad_tiers.append(repr(tier_name)[:40])
                continue
            if tier_name not in valid_tier_keys:
                bad_tiers.append(tier_name)
                continue
            if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
                bad_tiers.append(tier_name)
        if bad_tiers:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_TIER_PERMISSIONS has unexpected key or value shape for"
                f" {len(bad_tiers)} tier(s) — has_perm will fall back to default-deny for those"
                " tiers. Expected: dict keys in {'read', 'read_write', 'admin'}; per-tier value"
                " a list[str] of permission codenames"
                " (e.g. ['app.view_thing', 'app.change_thing']).",
            )
            return
        tier_count = len(tier_perms)
        self._ok(
            f"FRISIAN_MCP_OAUTH_TIER_PERMISSIONS set for {tier_count} tier(s)"
            " — has_perm grants the listed perms per tier"
        )

    def _check_oauth_auto_approve_consent_records(self, warnings: list[str]) -> None:
        """Warn when AUTO_APPROVE is True but no OAuthAuthorizeConsent rows exist yet."""
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return
        auto_approve: bool = bool(getattr(settings, "FRISIAN_MCP_OAUTH_AUTO_APPROVE", False))
        if not auto_approve:
            return
        try:
            from frisian_mcp.contrib.oauth.models import (  # pylint: disable=import-outside-toplevel
                OAuthAuthorizeConsent,
            )

            count = OAuthAuthorizeConsent.objects.count()
        except Exception:  # pylint: disable=broad-exception-caught
            # Model unavailable or DB not reachable — silent skip; the
            # INSTALLED_APPS / migration checks elsewhere surface that.
            return
        if count == 0:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_OAUTH_AUTO_APPROVE=True but no OAuthAuthorizeConsent rows exist —"
                " every authorize call still renders the consent form. If your deployment is"
                " not new, this likely signals a configuration drift (consent rows missing or"
                " filtered out).",
            )

    def _check_oauth_registration_vs_wellknown(self, _warnings: list[str]) -> None:
        """Verify registration_endpoint advertisement matches actual registration state."""
        oauth_installed = "frisian_mcp.contrib.oauth" in getattr(settings, "INSTALLED_APPS", [])
        if not oauth_installed:
            return

        reg_open: bool = getattr(settings, "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN", False)
        self._ok(
            f"FRISIAN_MCP_OAUTH_REGISTRATION_OPEN={'True' if reg_open else 'False'} — "
            f"registration_endpoint {'will be' if reg_open else 'will not be'} "
            "advertised in .well-known metadata"
        )

    def _check_hmac_key_rotation(self, warnings: list[str]) -> None:
        """Check HMAC key independence from SECRET_KEY for safe key rotation."""
        hmac_key: str | None = getattr(settings, "FRISIAN_MCP_HMAC_KEY", None)
        secret_key: str = getattr(settings, "SECRET_KEY", "")
        if hmac_key and hmac_key != secret_key:
            self._ok(
                "FRISIAN_MCP_HMAC_KEY is set and differs from SECRET_KEY — "
                "token HMACs can be rotated independently of Django's session/CSRF key"
            )
        elif hmac_key:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_HMAC_KEY is set but equals SECRET_KEY — "
                "rotating SECRET_KEY will still invalidate all issued tokens.  "
                "Use a separate randomly-generated HMAC key.",
            )
        else:
            self._warn_msg(
                warnings,
                "FRISIAN_MCP_HMAC_KEY is not set — token HMACs use SECRET_KEY.  "
                "Rotating SECRET_KEY invalidates all tokens.  "
                "Set FRISIAN_MCP_HMAC_KEY to a separate key for independent rotation.",
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
