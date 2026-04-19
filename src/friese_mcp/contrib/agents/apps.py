"""AppConfig for friese_mcp.contrib.agents."""

from django.apps import AppConfig


class AgentsConfig(AppConfig):
    """Django app configuration for the contrib.agents module."""

    name = "friese_mcp.contrib.agents"
    label = "friese_mcp_agents"
    verbose_name = "Friese MCP Agent Connections"
    default_auto_field = "django.db.models.BigAutoField"
