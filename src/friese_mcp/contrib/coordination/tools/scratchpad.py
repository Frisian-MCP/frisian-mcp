"""Scratchpad @mcp_dispatcher — per-session ephemeral notes for agents."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import Scratchpad
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _scratchpad_dict(s: Scratchpad) -> dict[str, Any]:
    """Serialize a Scratchpad to a plain dict."""
    return {
        "scratchpad_id": str(s.id),
        "title": s.title,
        "content": s.content,
        "agent_role": s.agent_role,
        "session_id": str(s.session_id) if s.session_id else None,
        "project_id": str(s.project_id) if s.project_id else None,
        "tenant_id": str(s.tenant_id) if s.tenant_id else None,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _get_scratchpad_or_raise(scratchpad_id: str, request: HttpRequest) -> Scratchpad:
    """Return the Scratchpad scoped by tenant, or raise LookupError."""
    qs = scope_qs(Scratchpad.objects.all(), request)
    try:
        return qs.get(id=scratchpad_id)
    except Scratchpad.DoesNotExist as exc:
        raise LookupError(f"Scratchpad {scratchpad_id!r} not found.") from exc


@mcp_dispatcher("scratchpad", "Create and manage per-session ephemeral notes.")
class ScratchpadDispatcher:
    """Dispatcher for per-agent, per-session scratchpad notes."""

    @mcp_action(
        "create",
        "Create a new scratchpad note.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Scratchpad title."},
                "content": {"type": "string", "description": "Initial content."},
                "agent_role": {"type": "string", "description": "Owning agent role."},
                "session_id": {
                    "type": "string",
                    "description": "Optional session UUID to group notes.",
                },
                "project_id": {"type": "string", "description": "Optional project UUID."},
            },
            "required": ["title"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new Scratchpad."""
        tenant = get_tenant(request)
        session_id = params.get("session_id")
        project_id = params.get("project_id")
        s = Scratchpad.objects.create(
            title=params["title"],
            content=params.get("content", ""),
            agent_role=params.get("agent_role", ""),
            session_id=uuid.UUID(session_id) if session_id else None,
            project_id=uuid.UUID(project_id) if project_id else None,
            tenant=tenant,
        )
        return _scratchpad_dict(s)

    @mcp_action(
        "get",
        "Get a scratchpad by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "scratchpad_id": {"type": "string", "description": "Scratchpad UUID."},
            },
            "required": ["scratchpad_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single scratchpad's details."""
        return _scratchpad_dict(_get_scratchpad_or_raise(params["scratchpad_id"], request))

    @mcp_action(
        "update",
        "Update scratchpad content (overwrite or append).",
        input_schema={
            "type": "object",
            "properties": {
                "scratchpad_id": {"type": "string", "description": "Scratchpad UUID."},
                "content": {"type": "string", "description": "New or appended content."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "How to apply content: overwrite (default) or append.",
                },
            },
            "required": ["scratchpad_id", "content"],
        },
    )
    def update(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Overwrite or append content on an existing scratchpad."""
        s = _get_scratchpad_or_raise(params["scratchpad_id"], request)
        mode: str = params.get("mode", "overwrite")
        if mode == "append":
            s.content = s.content + params["content"]
        else:
            s.content = params["content"]
        s.save(update_fields=["content", "updated_at"])
        return _scratchpad_dict(s)

    @mcp_action(
        "list",
        "List scratchpads with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Filter by agent role."},
                "project_id": {"type": "string", "description": "Filter by project UUID."},
                "session_id": {"type": "string", "description": "Filter by session UUID."},
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return scratchpads scoped by tenant, optionally filtered."""
        qs = scope_qs(Scratchpad.objects.all(), request)
        if role := params.get("agent_role"):
            qs = qs.filter(agent_role=role)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        if session_id := params.get("session_id"):
            qs = qs.filter(session_id=session_id)
        return {"scratchpads": [_scratchpad_dict(s) for s in qs]}
