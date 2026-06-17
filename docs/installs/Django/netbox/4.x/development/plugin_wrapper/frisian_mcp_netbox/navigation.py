from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label="MCP Gateway",
    icon_class="mdi mdi-api",
    groups=(
        (
            "OAuth",
            (
                PluginMenuItem(
                    link="plugins:frisian_mcp_netbox:oauthclient_list",
                    link_text="OAuth Clients",
                ),
                PluginMenuItem(
                    link="plugins:frisian_mcp_netbox:oauthaccesstoken_list",
                    link_text="Access Tokens",
                ),
            ),
        ),
        (
            "Tokens",
            (
                PluginMenuItem(
                    link="plugins:frisian_mcp_netbox:frisianmcptoken_list",
                    link_text="MCP Tokens",
                ),
            ),
        ),
    ),
)
