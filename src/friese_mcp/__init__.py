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

__version__ = "0.1.0"

__all__ = [
    "ResourceNotFoundError",
    "ResourceRegistry",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolRegistry",
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
