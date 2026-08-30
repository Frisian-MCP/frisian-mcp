from netbox.plugins import PluginConfig


class FrisianMcpNetBoxConfig(PluginConfig):
    name = "frisian_mcp_netbox"
    verbose_name = "frisian-mcp MCP Gateway"
    version = "0.1.0"
    base_url = "frisian-mcp"
    min_version = None
    max_version = None
    django_apps = [
        "django.contrib.admin",
        "frisian_mcp",
        "frisian_mcp.contrib.oauth",
        "frisian_mcp.contrib.tokens",
    ]

    def ready(self):
        import importlib
        import logging
        import os
        import re as re_module
        from django.conf import settings
        from django.urls import clear_url_caches, get_resolver, include, path, re_path

        logger = logging.getLogger(__name__)

        # Propagate FRISIAN_MCP_* settings from three sources (priority order):
        # 1. os.environ (docker-compose env vars), 2. netbox-docker loaded_configurations,
        # 3. raw config module. NetBox settings.py ignores unknown attrs.

        for key, value in os.environ.items():
            if key.startswith("FRISIAN_MCP_"):
                if not hasattr(settings, key):
                    setattr(settings, key, value)

        config_path = os.getenv("NETBOX_CONFIGURATION", "netbox.configuration")
        try:
            config_module = importlib.import_module(config_path)
            for mod in getattr(config_module, "loaded_configurations", []):
                for attr in dir(mod):
                    if attr.startswith("FRISIAN_MCP_") and not hasattr(settings, attr):
                        setattr(settings, attr, getattr(mod, attr))
            for attr in dir(config_module):
                if attr.startswith("FRISIAN_MCP_") and not hasattr(settings, attr):
                    setattr(settings, attr, getattr(config_module, attr))
        except ImportError:
            pass

        resolver = get_resolver()

        _MCP_AUTO_ATTR = "_frisian_mcp_auto_url"
        resolver.url_patterns[:] = [
            p for p in resolver.url_patterns
            if not getattr(p, _MCP_AUTO_ATTR, False)
        ]

        # ─────────────────────────────────────────────────────────────────
        # Mount EVERY configured route, not just one path.
        #
        # NetBox is the only supported host where frisian-mcp does not mount
        # its own URLs: everywhere else `AppConfig.ready()` does it, and
        # FRISIAN_MCP_ROUTES is honoured there for free. NetBox routes
        # third-party URLs through PluginConfig instead, so THIS is the only
        # thing that mounts anything — and it used to read FRISIAN_MCP_PATH
        # and mount exactly one door.
        #
        # The failure that caused was silent and bad. With three routes
        # configured, all three 404 while the default `/mcp/` answered 401:
        # the settings were accepted, no warning was emitted, and a
        # deployment believing it had a read-only door had an undifferentiated
        # one at a different URL. Configuration that is plausible, accepted
        # and inert is worse than configuration that fails.
        #
        # Each route gets the same `frisian_mcp.urls` include. The package
        # resolves which route a request belongs to from the matched path, so
        # mounting the paths is what makes the ceilings and carve-outs apply.
        # ─────────────────────────────────────────────────────────────────
        routes = getattr(settings, "FRISIAN_MCP_ROUTES", None) or {}
        if routes:
            mounted = []
            # Reversed so that, after successive insert(0) calls, the patterns
            # end up in declaration order — the order a reader of the settings
            # file expects to see them resolved in.
            for route_name, spec in reversed(list(routes.items())):
                raw = (spec or {}).get("path")
                if not raw:
                    logger.warning(
                        "frisian-mcp: route %r has no 'path'; not mounted", route_name
                    )
                    continue
                escaped = re_module.escape(str(raw).strip("/"))
                route_resolver = re_path(
                    rf"^{escaped}/?", include("frisian_mcp.urls")
                )
                setattr(route_resolver, _MCP_AUTO_ATTR, True)
                resolver.url_patterns.insert(0, route_resolver)
                mounted.append(str(raw).strip("/"))
            logger.info(
                "frisian-mcp: mounted %d route(s): %s",
                len(mounted),
                ", ".join(reversed(mounted)),
            )
        else:
            # No routes configured — the original single-door behaviour.
            mcp_path = re_module.escape(
                getattr(settings, "FRISIAN_MCP_PATH", "mcp").strip("/")
            )
            auto_resolver = re_path(rf"^{mcp_path}/?", include("frisian_mcp.urls"))
            setattr(auto_resolver, _MCP_AUTO_ATTR, True)
            resolver.url_patterns.insert(0, auto_resolver)
            logger.info("frisian-mcp: mounted single path %r", mcp_path)

        from django.contrib.auth import get_user_model
        NetBoxUser = get_user_model()
        if not hasattr(NetBoxUser, "is_staff"):
            NetBoxUser.is_staff = property(lambda self: self.is_superuser)

        from django.contrib import admin
        _ADMIN_AUTO_ATTR = "_frisian_mcp_admin_url"
        if not any(getattr(p, _ADMIN_AUTO_ATTR, False) for p in resolver.url_patterns):
            admin_resolver = path("admin/", admin.site.urls)
            setattr(admin_resolver, _ADMIN_AUTO_ATTR, True)
            resolver.url_patterns.insert(1, admin_resolver)

        # Patch _get_action_url onto frisian-mcp models so NetBox's get_action_url()
        # resolves to plugin URLs instead of failing with NoReverseMatch.
        from django.urls import NoReverseMatch as _NRM
        from django.urls import reverse as _reverse
        from frisian_mcp.contrib.oauth.models import OAuthAccessToken, OAuthClient
        from frisian_mcp.contrib.tokens.models import FrisianMcpToken

        def _make_action_url(action_map):
            def _get(cls, action, rest_api=False, kwargs=None):
                url_name = action_map.get(action)
                if url_name:
                    return _reverse(f"plugins:frisian_mcp_netbox:{url_name}", kwargs=kwargs or {})
                raise _NRM(f"No plugin URL for action '{action}'")
            return classmethod(_get)

        OAuthClient._get_action_url = _make_action_url({
            "list": "oauthclient_list",
            "add": "oauthclient_add",
            "edit": "oauthclient_edit",
            "delete": "oauthclient_delete",
        })
        OAuthAccessToken._get_action_url = _make_action_url({
            "list": "oauthaccesstoken_list",
            "delete": "oauthaccesstoken_delete",
        })
        FrisianMcpToken._get_action_url = _make_action_url({
            "list": "frisianmcptoken_list",
            "add": "frisianmcptoken_add",
            "edit": "frisianmcptoken_edit",
            "delete": "frisianmcptoken_delete",
        })

        clear_url_caches()
        super().ready()


config = FrisianMcpNetBoxConfig
