"""Escalate-to-human @mcp_dispatcher — urgent human escalation via approval requests."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import ApprovalRequest, Room, RoomNote
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher

# Sentinel stored in requesting_agent to identify escalations.
_ESCALATION_PREFIX = "escalation:"


def _escalation_dict(a: ApprovalRequest) -> dict[str, Any]:
    """Serialize an escalation ApprovalRequest to a plain dict."""
    return {
        "escalation_id": str(a.id),
        "title": a.title,
        "description": a.description,
        "requesting_agent": a.requesting_agent.removeprefix(_ESCALATION_PREFIX),
        "status": a.status,
        "resolution_note": a.resolution_note,
        "resolved_by": a.resolved_by,
        "project_id": str(a.project_id) if a.project_id else None,
        "tenant_id": str(a.tenant_id) if a.tenant_id else None,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _get_escalation_or_raise(escalation_id: str, request: HttpRequest) -> ApprovalRequest:
    """Return the escalation ApprovalRequest scoped by tenant, or raise LookupError."""
    qs = scope_qs(
        ApprovalRequest.objects.filter(requesting_agent__startswith=_ESCALATION_PREFIX),
        request,
    )
    try:
        return qs.get(id=escalation_id)
    except ApprovalRequest.DoesNotExist as exc:
        raise LookupError(f"Escalation {escalation_id!r} not found.") from exc


@mcp_dispatcher("escalate_to_human", "Escalate a blocking issue to a human reviewer.")
class EscalateToHumanDispatcher:
    """Dispatcher for human escalation via ApprovalRequest."""

    @mcp_action(
        "create",
        "Create a human escalation request, optionally posting to a room.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short escalation title."},
                "description": {"type": "string", "description": "Details of the blocking issue."},
                "requesting_agent": {
                    "type": "string",
                    "description": "Role of the escalating agent.",
                },
                "room_id": {
                    "type": "string",
                    "description": "Optional room UUID to post a notification.",
                },
                "project_id": {"type": "string", "description": "Optional project UUID."},
            },
            "required": ["title", "description", "requesting_agent"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create an escalation ApprovalRequest and optionally post to a room."""
        tenant = get_tenant(request)
        project_id = params.get("project_id")
        requesting_agent = f"{_ESCALATION_PREFIX}{params['requesting_agent']}"
        a = ApprovalRequest.objects.create(
            title=params["title"],
            description=params["description"],
            requesting_agent=requesting_agent,
            project_id=project_id if project_id else None,
            tenant=tenant,
        )
        if room_id := params.get("room_id"):
            try:
                room = scope_qs(Room.objects.all(), request).get(id=room_id)
                RoomNote.objects.create(
                    room=room,
                    content=f"[ESCALATION] {params['title']}\n\n{params['description']}",
                    agent_role=params["requesting_agent"],
                    is_human_message=False,
                )
            except Room.DoesNotExist:
                pass  # Non-fatal: room not found, skip notification
        return _escalation_dict(a)

    @mcp_action(
        "get",
        "Get an escalation request by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "escalation_id": {"type": "string", "description": "Escalation UUID."},
            },
            "required": ["escalation_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single escalation's details."""
        return _escalation_dict(_get_escalation_or_raise(params["escalation_id"], request))

    @mcp_action(
        "list",
        "List escalation requests with optional status filter.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (pending/approved/rejected/cancelled).",
                },
                "project_id": {"type": "string", "description": "Filter by project UUID."},
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return escalations scoped by tenant, optionally filtered."""
        qs = scope_qs(
            ApprovalRequest.objects.filter(requesting_agent__startswith=_ESCALATION_PREFIX),
            request,
        )
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        return {"escalations": [_escalation_dict(a) for a in qs]}


    @mcp_action(
        "resolve",
        "Resolve an escalation (approve or reject).",
        input_schema={
            "type": "object",
            "properties": {
                "escalation_id": {"type": "string", "description": "Escalation UUID."},
                "resolution": {
                    "type": "string",
                    "enum": ["approved", "rejected"],
                    "description": "Resolution outcome.",
                },
                "resolution_note": {"type": "string", "description": "Resolution details."},
                "resolved_by": {"type": "string", "description": "Human resolver identity."},
            },
            "required": ["escalation_id", "resolution"],
        },
    )
    def resolve(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a pending escalation."""
        a = _get_escalation_or_raise(params["escalation_id"], request)
        if a.status != "pending":
            raise ValueError(
                f"Cannot resolve escalation with status {a.status!r}. Must be 'pending'."
            )
        a.status = params["resolution"]
        a.resolution_note = params.get("resolution_note", "")
        a.resolved_by = params.get("resolved_by", "")
        a.save(update_fields=["status", "resolution_note", "resolved_by", "updated_at"])
        return _escalation_dict(a)
