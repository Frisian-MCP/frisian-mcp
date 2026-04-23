"""Workers @mcp_dispatcher — agent worker registration, heartbeats, and status."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest
from django.utils import timezone

from friese_mcp.contrib.coordination.models import RoomNote, Worker
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _worker_dict(w: Worker) -> dict[str, Any]:
    """Serialize a Worker to a plain dict."""
    return {
        "worker_id": str(w.id),
        "name": w.name,
        "role": w.role,
        "status": w.status,
        "capabilities": w.capabilities,
        "project_id": str(w.project_id) if w.project_id else None,
        "tenant_id": str(w.tenant_id) if w.tenant_id else None,
        "last_heartbeat": w.last_heartbeat.isoformat() if w.last_heartbeat else None,
        "registered_at": w.registered_at.isoformat(),
    }


def _get_worker_or_raise(worker_id: str, request: HttpRequest) -> Worker:
    """Return the Worker scoped by tenant, or raise LookupError."""
    qs = scope_qs(Worker.objects.all(), request)
    try:
        return qs.get(id=worker_id)
    except Worker.DoesNotExist as exc:
        raise LookupError(f"Worker {worker_id!r} not found.") from exc


@mcp_dispatcher("workers", "Manage agent worker registration, heartbeats, and status.")
class WorkersDispatcher:
    """Dispatcher for worker lifecycle management."""

    @mcp_action(
        "register",
        "Register a new agent worker.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique worker name."},
                "role": {
                    "type": "string",
                    "description": "Agent role (e.g. 'python-development').",
                },
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of capability tags.",
                },
                "project_id": {"type": "string", "description": "Optional project UUID to join."},
            },
            "required": ["name", "role"],
        },
    )
    def register(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new Worker row and return its details."""
        tenant = get_tenant(request)
        project_id = params.get("project_id")
        w = Worker.objects.create(
            name=params["name"],
            role=params["role"],
            capabilities=params.get("capabilities", []),
            tenant=tenant,
            project_id=uuid.UUID(project_id) if project_id else None,
            status="active",
            last_heartbeat=timezone.now(),
        )
        return _worker_dict(w)

    @mcp_action(
        "get",
        "Get details for a specific worker.",
        input_schema={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker UUID."},
            },
            "required": ["worker_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single worker's details."""
        return _worker_dict(_get_worker_or_raise(params["worker_id"], request))

    @mcp_action(
        "list",
        "List workers with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (active/stale/disconnected).",
                },
                "role": {"type": "string", "description": "Filter by role."},
                "project_id": {"type": "string", "description": "Filter by project UUID."},
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a list of workers, optionally filtered."""
        qs = scope_qs(Worker.objects.all(), request)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if role := params.get("role"):
            qs = qs.filter(role=role)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        return {"workers": [_worker_dict(w) for w in qs]}

    @mcp_action(
        "heartbeat",
        "Send a heartbeat to keep a worker active.",
        input_schema={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker UUID."},
            },
            "required": ["worker_id"],
        },
    )
    def heartbeat(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Stamp last_heartbeat=now and status=active on the worker."""
        w = _get_worker_or_raise(params["worker_id"], request)
        w.last_heartbeat = timezone.now()
        w.status = "active"
        w.save(update_fields=["last_heartbeat", "status", "updated_at"])
        return {
            "worker_id": str(w.id),
            "status": w.status,
            "last_heartbeat": w.last_heartbeat.isoformat(),
        }

    @mcp_action(
        "deregister",
        "Deregister a worker (sets status=disconnected).",
        input_schema={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker UUID."},
            },
            "required": ["worker_id"],
        },
    )
    def deregister(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Mark a worker as disconnected."""
        w = _get_worker_or_raise(params["worker_id"], request)
        w.status = "disconnected"
        w.save(update_fields=["status", "updated_at"])
        return {"worker_id": str(w.id), "status": w.status}

    @mcp_action(
        "activity",
        "Get recent room notes posted by a worker.",
        input_schema={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker UUID."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max notes to return (default 20).",
                },
            },
            "required": ["worker_id"],
        },
    )
    def activity(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return recent RoomNotes posted by the worker."""
        worker_id = params["worker_id"]
        _get_worker_or_raise(worker_id, request)  # access check
        limit: int = params.get("limit", 20)
        notes = (
            RoomNote.objects.filter(worker_id=worker_id)
            .select_related("room")
            .order_by("-created_at")[:limit]
        )
        return {
            "worker_id": worker_id,
            "notes": [
                {
                    "note_id": str(n.id),
                    "room_id": str(n.room_id),
                    "room_name": n.room.name,
                    "content": n.content,
                    "created_at": n.created_at.isoformat(),
                }
                for n in notes
            ],
        }
