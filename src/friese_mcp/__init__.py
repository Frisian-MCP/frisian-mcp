"""friese-mcp: Django MCP gateway with runtime introspection and permission-aware tool scoping."""

from friese_mcp.decorators import (
    mcp_action,
    mcp_dispatcher,
    mcp_heavy,
    mcp_ignore,
    mcp_resource,
    mcp_tool,
)
from friese_mcp.registry import (
    ToolInputError,
    ToolNotFoundError,
    ToolRegistry,
    register,
    tool_registry,
)
from friese_mcp.resources import ResourceNotFoundError, ResourceRegistry, resource_registry
from friese_mcp.views import invalidate_tools_list_cache

__version__ = "0.2.0"

__all__ = [
    "ResourceNotFoundError",
    "ResourceRegistry",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolRegistry",
    "invalidate_tools_list_cache",
    "mcp_action",
    "mcp_dispatcher",
    "mcp_heavy",
    "mcp_ignore",
    "mcp_resource",
    "mcp_tool",
    "register",
    "resource_registry",
    "tool_registry",
]
