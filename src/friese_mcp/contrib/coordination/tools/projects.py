"""Projects @mcp_dispatcher — project creation, status, plan management."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import Project, Room
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _project_dict(p: Project) -> dict[str, Any]:
    """Serialize a Project to a plain dict."""
    return {
        "project_id": str(p.id),
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "created_by": p.created_by,
        "plan": p.plan,
        "tenant_id": str(p.tenant_id) if p.tenant_id else None,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def _room_summary(r: Room) -> dict[str, Any]:
    """Serialize a Room to a brief summary dict."""
    return {
        "room_id": str(r.id),
        "name": r.name,
        "status": r.status,
        "purpose": r.purpose,
    }


def _get_project_or_raise(project_id: str, request: HttpRequest) -> Project:
    """Return the Project scoped by tenant, or raise LookupError."""
    qs = scope_qs(Project.objects.all(), request)
    try:
        return qs.get(id=project_id)
    except Project.DoesNotExist as exc:
        raise LookupError(f"Project {project_id!r} not found.") from exc


@mcp_dispatcher("projects", "Create, list, and manage coordination projects.")
class ProjectsDispatcher:
    """Dispatcher for project lifecycle management."""

    @mcp_action(
        "create",
        "Create a new project.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name."},
                "description": {"type": "string", "description": "Project description."},
                "created_by": {"type": "string", "description": "Creator agent role."},
                "status": {
                    "type": "string",
                    "description": "Initial status (default: active).",
                },
            },
            "required": ["name"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new Project."""
        tenant = get_tenant(request)
        p = Project.objects.create(
            name=params["name"],
            description=params.get("description", ""),
            created_by=params.get("created_by", ""),
            status=params.get("status", "active"),
            tenant=tenant,
        )
        return _project_dict(p)

    @mcp_action(
        "get",
        "Get project details including task counts and linked rooms.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
            },
            "required": ["project_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return project details with task counts and linked rooms."""
        p = _get_project_or_raise(params["project_id"], request)
        task_qs = p.tasks.values("status").annotate(count=Count("id"))
        task_counts: dict[str, int] = {row["status"]: row["count"] for row in task_qs}
        rooms = [_room_summary(r) for r in p.rooms.all()]
        result = _project_dict(p)
        result["task_counts"] = {
            "ready": task_counts.get("ready", 0),
            "in_progress": task_counts.get("in_progress", 0),
            "done": task_counts.get("done", 0),
            "blocked": task_counts.get("blocked", 0),
            "failed": task_counts.get("failed", 0),
        }
        result["linked_rooms"] = rooms
        return result

    @mcp_action(
        "list",
        "List projects with optional status filter.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (active/draft/on_hold/completed).",
                },
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return projects scoped by tenant, optionally filtered."""
        qs = scope_qs(Project.objects.all(), request)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        return {"projects": [_project_dict(p) for p in qs]}

    @mcp_action(
        "update",
        "Update project fields.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["project_id"],
        },
    )
    def update(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Update name, description, or status on an existing project."""
        p = _get_project_or_raise(params["project_id"], request)
        for field in ("name", "description", "status"):
            if field in params:
                setattr(p, field, params[field])
        p.save()
        return _project_dict(p)

    @mcp_action(
        "get_plan",
        "Return the project plan JSON.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
            },
            "required": ["project_id"],
        },
    )
    def get_plan(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return the project's plan field."""
        p = _get_project_or_raise(params["project_id"], request)
        return {"project_id": str(p.id), "plan": p.plan}

    @mcp_action(
        "update_plan",
        "Update the project plan (version auto-increments).",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
                "content": {
                    "description": "New plan content (any JSON value).",
                },
            },
            "required": ["project_id", "content"],
        },
    )
    def update_plan(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Atomically update project.plan and increment its version."""
        project_id = params["project_id"]
        with transaction.atomic():
            p = (
                scope_qs(Project.objects.select_for_update(), request)
                .filter(id=uuid.UUID(project_id))
                .first()
            )
            if p is None:
                raise LookupError(f"Project {project_id!r} not found.")
            plan_dict = p.plan if isinstance(p.plan, dict) else {}
            current_version: int = plan_dict.get("version", 0)
            p.plan = {"version": current_version + 1, "content": params["content"]}
            p.save(update_fields=["plan", "updated_at"])
        return {"project_id": str(p.id), "plan": p.plan, "version": p.plan["version"]}

    @mcp_action(
        "list_rooms",
        "List rooms linked to a project.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
                "status": {"type": "string", "description": "Filter by room status."},
            },
            "required": ["project_id"],
        },
    )
    def list_rooms(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return rooms linked to the project."""
        p = _get_project_or_raise(params["project_id"], request)
        qs = p.rooms.all()
        if status := params.get("status"):
            qs = qs.filter(status=status)
        return {
            "project_id": str(p.id),
            "rooms": [_room_summary(r) for r in qs],
        }
