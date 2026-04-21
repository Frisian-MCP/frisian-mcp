"""friese-mcp: Django MCP gateway with runtime introspection and permission-aware tool scoping."""

from friese_mcp.decorators import mcp_action, mcp_dispatcher, mcp_ignore, mcp_tool
from friese_mcp.registry import ToolInputError, ToolNotFoundError, ToolRegistry, tool_registry

__version__ = "0.1.0"

__all__ = [
    "ToolInputError",
    "ToolNotFoundError",
    "ToolRegistry",
    "mcp_action",
    "mcp_dispatcher",
    "mcp_ignore",
    "mcp_tool",
    "tool_registry",
]
