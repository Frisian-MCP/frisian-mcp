"""Approvals @mcp_dispatcher — human-approval gates for agent workflows."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import ApprovalRequest
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _approval_dict(a: ApprovalRequest) -> dict[str, Any]:
    """Serialize an ApprovalRequest to a plain dict."""
    return {
        "approval_id": str(a.id),
        "title": a.title,
        "description": a.description,
        "requesting_agent": a.requesting_agent,
        "status": a.status,
        "resolution_note": a.resolution_note,
        "resolved_by": a.resolved_by,
        "project_id": str(a.project_id) if a.project_id else None,
        "tenant_id": str(a.tenant_id) if a.tenant_id else None,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _get_approval_or_raise(approval_id: str, request: HttpRequest) -> ApprovalRequest:
    """Return the ApprovalRequest scoped by tenant, or raise LookupError."""
    qs = scope_qs(ApprovalRequest.objects.all(), request)
    try:
        return qs.get(id=approval_id)
    except ApprovalRequest.DoesNotExist as exc:
        raise LookupError(f"Approval request {approval_id!r} not found.") from exc


def _require_pending(a: ApprovalRequest) -> None:
    """Raise ValueError if the approval request is not pending."""
    if a.status != "pending":
        raise ValueError(
            f"Cannot act on approval request with status {a.status!r}. Must be 'pending'."
        )


@mcp_dispatcher("approvals", "Create and manage human-approval gates for agent workflows.")
class ApprovalsDispatcher:
    """Dispatcher for ApprovalRequest lifecycle management."""

    @mcp_action(
        "create",
        "Create a new approval request.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title for the request."},
                "description": {
                    "type": "string",
                    "description": "Full details of what needs approval.",
                },
                "requesting_agent": {
                    "type": "string",
                    "description": "Role of the requesting agent.",
                },
                "project_id": {"type": "string", "description": "Optional project UUID."},
            },
            "required": ["title", "description", "requesting_agent"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a pending ApprovalRequest."""
        tenant = get_tenant(request)
        project_id = params.get("project_id")
        a = ApprovalRequest.objects.create(
            title=params["title"],
            description=params["description"],
            requesting_agent=params["requesting_agent"],
            project_id=project_id if project_id else None,
            tenant=tenant,
        )
        return _approval_dict(a)

    @mcp_action(
        "get",
        "Get an approval request by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "ApprovalRequest UUID."},
            },
            "required": ["approval_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single ApprovalRequest's details."""
        return _approval_dict(_get_approval_or_raise(params["approval_id"], request))

    @mcp_action(
        "list",
        "List approval requests with optional filters.",
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
        """Return approval requests scoped by tenant, optionally filtered."""
        qs = scope_qs(ApprovalRequest.objects.all(), request)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        return {"approvals": [_approval_dict(a) for a in qs]}

    @mcp_action(
        "approve",
        "Approve a pending approval request.",
        input_schema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "ApprovalRequest UUID."},
                "resolution_note": {"type": "string", "description": "Optional approval note."},
                "resolved_by": {"type": "string", "description": "Resolver identity (agent/user)."},
            },
            "required": ["approval_id"],
        },
    )
    def approve(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition a pending ApprovalRequest to approved."""
        a = _get_approval_or_raise(params["approval_id"], request)
        _require_pending(a)
        a.status = "approved"
        a.resolution_note = params.get("resolution_note", "")
        a.resolved_by = params.get("resolved_by", "")
        a.save(update_fields=["status", "resolution_note", "resolved_by", "updated_at"])
        return _approval_dict(a)

    @mcp_action(
        "reject",
        "Reject a pending approval request.",
        input_schema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "ApprovalRequest UUID."},
                "resolution_note": {"type": "string", "description": "Reason for rejection."},
                "resolved_by": {"type": "string", "description": "Resolver identity (agent/user)."},
            },
            "required": ["approval_id"],
        },
    )
    def reject(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition a pending ApprovalRequest to rejected."""
        a = _get_approval_or_raise(params["approval_id"], request)
        _require_pending(a)
        a.status = "rejected"
        a.resolution_note = params.get("resolution_note", "")
        a.resolved_by = params.get("resolved_by", "")
        a.save(update_fields=["status", "resolution_note", "resolved_by", "updated_at"])
        return _approval_dict(a)

    @mcp_action(
        "cancel",
        "Cancel a pending approval request.",
        input_schema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "ApprovalRequest UUID."},
                "reason": {"type": "string", "description": "Reason for cancellation."},
            },
            "required": ["approval_id"],
        },
    )
    def cancel(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition a pending ApprovalRequest to cancelled."""
        a = _get_approval_or_raise(params["approval_id"], request)
        _require_pending(a)
        a.status = "cancelled"
        a.resolution_note = params.get("reason", "")
        a.save(update_fields=["status", "resolution_note", "updated_at"])
        return _approval_dict(a)
