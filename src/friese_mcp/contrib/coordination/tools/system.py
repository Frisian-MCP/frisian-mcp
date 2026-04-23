"""System @mcp_dispatcher — connectivity, role listing, and surface help."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest

from friese_mcp.decorators import mcp_action, mcp_dispatcher

_DEFAULT_ROLES: list[str] = [
    "project-manager",
    "python-development",
    "frontend-engineer",
    "platform-architecture",
    "data-scientist",
    "documentation-generation",
    "testing",
    "security",
    "orchestrator",
]

_TOOL_SURFACE: list[dict[str, str]] = [
    {"tool": "workers", "description": "Register, heartbeat, and manage agent workers."},
    {"tool": "rooms", "description": "Create rooms, post messages, read, search."},
    {"tool": "tasks", "description": "Create, lease, complete, and track project tasks."},
    {"tool": "projects", "description": "Manage projects, plans, and linked rooms."},
    {"tool": "artifacts", "description": "Store and retrieve versioned documents."},
    {"tool": "scratchpad", "description": "Per-session ephemeral notes for agents."},
    {"tool": "approvals", "description": "Human-approval gates for agent workflows."},
    {"tool": "escalate_to_human", "description": "Escalate a blocking issue to a human."},
    {"tool": "system", "description": "Connectivity checks, role lists, and help."},
]


@mcp_dispatcher("system", "Connectivity checks, role listing, and coordination surface help.")
class SystemDispatcher:
    """Dispatcher for system-level queries."""

    @mcp_action(
        "echo",
        "Echo params back — use as a connectivity test.",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Any message to echo back."},
            },
        },
    )
    def echo(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return params unchanged — connectivity check."""
        return {"echo": params.get("message", ""), "params": params}

    @mcp_action(
        "role_list",
        "Return the list of known agent roles.",
        input_schema={"type": "object", "properties": {}},
    )
    def role_list(  # pylint: disable=unused-argument
        self, request: HttpRequest, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Return agent roles from FRIESE_MCP_COORDINATION_ROLES or defaults."""
        roles: list[str] = getattr(settings, "FRIESE_MCP_COORDINATION_ROLES", _DEFAULT_ROLES)
        return {"roles": list(roles)}

    @mcp_action(
        "help",
        "Return a summary of the coordination tool surface.",
        input_schema={"type": "object", "properties": {}},
    )
    def help(  # pylint: disable=unused-argument
        self, request: HttpRequest, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Return the available coordination tools and brief descriptions."""
        return {"tools": _TOOL_SURFACE}
