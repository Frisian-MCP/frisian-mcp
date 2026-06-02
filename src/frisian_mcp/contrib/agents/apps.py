"""AppConfig for frisian_mcp.contrib.agents."""

from django.apps import AppConfig


class AgentsConfig(AppConfig):
    """Django app configuration for the contrib.agents module."""

    name = "frisian_mcp.contrib.agents"
    label = "frisian_mcp_agents"
    verbose_name = "Frisian MCP Agent Connections"
    default_auto_field = "django.db.models.BigAutoField"
