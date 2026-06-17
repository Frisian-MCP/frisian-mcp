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
        import os
        import re as re_module
        from django.conf import settings
        from django.urls import clear_url_caches, get_resolver, include, path, re_path

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
        mcp_path = re_module.escape(
            getattr(settings, "FRISIAN_MCP_PATH", "mcp").strip("/")
        )
        auto_resolver = re_path(rf"^{mcp_path}/?", include("frisian_mcp.urls"))
        setattr(auto_resolver, _MCP_AUTO_ATTR, True)
        resolver.url_patterns.insert(0, auto_resolver)

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
