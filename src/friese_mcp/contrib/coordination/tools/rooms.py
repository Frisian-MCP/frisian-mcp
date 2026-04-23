"""Rooms @mcp_dispatcher — room creation, messaging, and search."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import Room, RoomNote
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _room_dict(r: Room) -> dict[str, Any]:
    """Serialize a Room to a plain dict."""
    return {
        "room_id": str(r.id),
        "name": r.name,
        "purpose": r.purpose,
        "status": r.status,
        "outcome_type": r.outcome_type,
        "project_id": str(r.project_id) if r.project_id else None,
        "tenant_id": str(r.tenant_id) if r.tenant_id else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _note_dict(n: RoomNote) -> dict[str, Any]:
    """Serialize a RoomNote to a plain dict."""
    return {
        "note_id": str(n.id),
        "room_id": str(n.room_id),
        "agent_role": n.agent_role,
        "worker_id": str(n.worker_id) if n.worker_id else None,
        "content": n.content,
        "is_human_message": n.is_human_message,
        "created_at": n.created_at.isoformat(),
    }


def _get_room_or_raise(room_id: str, request: HttpRequest) -> Room:
    """Return the Room scoped by tenant, or raise LookupError."""
    qs = scope_qs(Room.objects.all(), request)
    try:
        return qs.get(id=room_id)
    except Room.DoesNotExist as exc:
        raise LookupError(f"Room {room_id!r} not found.") from exc


@mcp_dispatcher("rooms", "Manage discussion rooms and post/read messages.")
class RoomsDispatcher:
    """Dispatcher for room lifecycle and messaging."""

    @mcp_action(
        "create",
        "Create a new discussion room.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique room name."},
                "purpose": {"type": "string", "description": "Room purpose or topic."},
                "project_id": {"type": "string", "description": "Optional project UUID."},
                "outcome_type": {
                    "type": "string",
                    "description": "Expected outcome type (e.g. 'decision').",
                },
            },
            "required": ["name"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new Room and return its details."""
        tenant = get_tenant(request)
        project_id = params.get("project_id")
        r = Room.objects.create(
            name=params["name"],
            purpose=params.get("purpose", ""),
            outcome_type=params.get("outcome_type", ""),
            tenant=tenant,
            project_id=uuid.UUID(project_id) if project_id else None,
        )
        return _room_dict(r)

    @mcp_action(
        "list",
        "List rooms with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (active/closed/archived).",
                },
                "project_id": {"type": "string", "description": "Filter by project UUID."},
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return rooms scoped by tenant, optionally filtered."""
        qs = scope_qs(Room.objects.all(), request)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        return {"rooms": [_room_dict(r) for r in qs]}

    @mcp_action(
        "read",
        "Read recent messages from a room.",
        input_schema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "description": "Room UUID."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Max messages to return (default 50).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of messages to skip (default 0).",
                },
            },
            "required": ["room_id"],
        },
    )
    def read(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return paginated messages from a room."""
        _get_room_or_raise(params["room_id"], request)  # access check
        limit: int = params.get("limit", 50)
        offset: int = params.get("offset", 0)
        base_qs = RoomNote.objects.filter(room_id=params["room_id"]).order_by("created_at")
        total = base_qs.count()
        notes = base_qs[offset : offset + limit]
        messages = [_note_dict(n) for n in notes]
        has_more = (offset + len(messages)) < total
        return {
            "messages": messages,
            "total": total,
            "has_more": has_more,
            "next_offset": offset + len(messages) if has_more else None,
        }

    @mcp_action(
        "post",
        "Post a message to a room.",
        input_schema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "description": "Room UUID."},
                "content": {"type": "string", "description": "Message content."},
                "role": {"type": "string", "description": "Posting agent's role label."},
                "worker_id": {"type": "string", "description": "Optional posting worker UUID."},
                "is_human_message": {
                    "type": "boolean",
                    "description": "Mark as a human message (default false).",
                },
            },
            "required": ["room_id", "content"],
        },
    )
    def post(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Post a new RoomNote and return its details."""
        _get_room_or_raise(params["room_id"], request)  # access check
        worker_id = params.get("worker_id")
        note = RoomNote.objects.create(
            room_id=uuid.UUID(params["room_id"]),
            content=params["content"],
            agent_role=params.get("role", ""),
            worker_id=uuid.UUID(worker_id) if worker_id else None,
            is_human_message=params.get("is_human_message", False),
        )
        return _note_dict(note)

    @mcp_action(
        "close",
        "Close a room.",
        input_schema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "description": "Room UUID."},
                "outcome_type": {
                    "type": "string",
                    "description": "Outcome type label (e.g. 'decision', 'cancelled').",
                },
            },
            "required": ["room_id"],
        },
    )
    def close(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Set room status to closed and record outcome_type."""
        r = _get_room_or_raise(params["room_id"], request)
        r.status = "closed"
        if outcome_type := params.get("outcome_type"):
            r.outcome_type = outcome_type
        r.save(update_fields=["status", "outcome_type", "updated_at"])
        return _room_dict(r)

    @mcp_action(
        "search",
        "Search room messages by content.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for (case-insensitive).",
                },
                "room_id": {"type": "string", "description": "Limit search to a specific room."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max results to return (default 20).",
                },
            },
            "required": ["query"],
        },
    )
    def search(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Case-insensitive content search across RoomNotes."""
        qs = RoomNote.objects.filter(content__icontains=params["query"]).order_by("-created_at")
        if room_id := params.get("room_id"):
            _get_room_or_raise(room_id, request)  # access check
            qs = qs.filter(room_id=room_id)
        else:
            # Scope by tenant via room's tenant FK
            tenant = get_tenant(request)
            if tenant is not None:
                qs = qs.filter(room__tenant=tenant)
        limit: int = params.get("limit", 20)
        return {"results": [_note_dict(n) for n in qs[:limit]], "query": params["query"]}
