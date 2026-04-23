"""AppConfig for friese_mcp.contrib.coordination."""

from django.apps import AppConfig


class CoordinationConfig(AppConfig):
    """Django app configuration for the contrib.coordination module."""

    name = "friese_mcp.contrib.coordination"
    label = "friese_mcp_coordination"
    verbose_name = "Friese MCP Coordination"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Import tools to trigger @mcp_dispatcher registration."""
        # pylint: disable=import-outside-toplevel
        from friese_mcp.contrib.coordination.tools import approvals as _approvals  # noqa: F401
        from friese_mcp.contrib.coordination.tools import artifacts as _artifacts  # noqa: F401
        from friese_mcp.contrib.coordination.tools import escalate as _escalate  # noqa: F401
        from friese_mcp.contrib.coordination.tools import projects as _projects  # noqa: F401
        from friese_mcp.contrib.coordination.tools import rooms as _rooms  # noqa: F401
        from friese_mcp.contrib.coordination.tools import scratchpad as _scratchpad  # noqa: F401
        from friese_mcp.contrib.coordination.tools import system as _system  # noqa: F401
        from friese_mcp.contrib.coordination.tools import tasks as _tasks  # noqa: F401
        from friese_mcp.contrib.coordination.tools import workers as _workers  # noqa: F401
