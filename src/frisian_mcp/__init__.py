"""frisian-mcp: Django MCP gateway with runtime introspection and permission-aware tool scoping."""

from typing import Any

from frisian_mcp.decorators import (
    mcp_action,
    mcp_dispatcher,
    mcp_heavy,
    mcp_ignore,
    mcp_resource,
    mcp_tool,
)
from frisian_mcp.registry import (
    ToolInputError,
    ToolNotFoundError,
    ToolRegistry,
    register,
    tool_registry,
)
from frisian_mcp.resources import ResourceNotFoundError, ResourceRegistry, resource_registry

__version__ = "1.0.11rc1"

# ``invalidate_tools_list_cache`` is exposed via ``__getattr__`` (PEP 562),
# not as a top-level binding — pylint cannot statically resolve such entries.
__all__ = [
    "ResourceNotFoundError",
    "ResourceRegistry",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolRegistry",
    "invalidate_tools_list_cache",  # pylint: disable=undefined-all-variable
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


def __getattr__(name: str) -> Any:
    """
    Lazy attribute resolution for view-layer re-exports.

    ``invalidate_tools_list_cache`` lives in :mod:`frisian_mcp.views`, which
    imports DRF view classes that touch Django's app registry on import.  An
    eager top-level import here would trigger ``AppRegistryNotReady`` during
    management commands (``migrate``, ``post_upgrade``) that run before
    ``apps.populate()`` finishes.  Resolving on first attribute access defers
    that import until Django is ready.
    """
    if name == "invalidate_tools_list_cache":
        from frisian_mcp.views import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            invalidate_tools_list_cache as _impl,
        )

        return _impl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
