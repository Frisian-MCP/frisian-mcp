"""Tasks @mcp_dispatcher — task creation, leasing, and lifecycle management."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from friese_mcp.contrib.coordination.models import ProjectTask
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher

_DEFAULT_LEASE_SECONDS = 300


def _task_dict(t: ProjectTask) -> dict[str, Any]:
    """Serialize a ProjectTask to a plain dict."""
    return {
        "task_id": str(t.id),
        "title": t.title,
        "description": t.description,
        "assigned_role": t.assigned_role,
        "status": t.status,
        "priority": t.priority,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "blocked_reason": t.blocked_reason,
        "result_summary": t.result_summary,
        "lease_owner": str(t.lease_owner) if t.lease_owner else None,
        "lease_expires_at": t.lease_expires_at.isoformat() if t.lease_expires_at else None,
        "heartbeat_at": t.heartbeat_at.isoformat() if t.heartbeat_at else None,
        "project_id": str(t.project_id) if t.project_id else None,
        "tenant_id": str(t.tenant_id) if t.tenant_id else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def _get_task_or_raise(task_id: str, request: HttpRequest) -> ProjectTask:
    """Return the ProjectTask scoped by tenant, or raise LookupError."""
    qs = scope_qs(ProjectTask.objects.all(), request)
    try:
        return qs.get(id=task_id)
    except ProjectTask.DoesNotExist as exc:
        raise LookupError(f"Task {task_id!r} not found.") from exc


def _require_status(task: ProjectTask, *allowed: str) -> None:
    """Raise ValueError if task.status is not in *allowed*."""
    if task.status not in allowed:
        raise ValueError(
            f"Cannot perform operation on task with status {task.status!r}. "
            f"Expected one of: {', '.join(repr(s) for s in allowed)}."
        )


@mcp_dispatcher("tasks", "Create, assign, lease, complete, and track project tasks.")
class TasksDispatcher:
    """Dispatcher for task lifecycle management."""

    @mcp_action(
        "create",
        "Create a new project task.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title."},
                "description": {"type": "string", "description": "Task details."},
                "assigned_role": {
                    "type": "string",
                    "description": "Role responsible for this task.",
                },
                "project_id": {"type": "string", "description": "Optional project UUID."},
                "priority": {
                    "type": "integer",
                    "description": "Priority (lower = higher priority, default 50).",
                },
                "deadline": {
                    "type": "string",
                    "description": "ISO-8601 deadline datetime (optional).",
                },
            },
            "required": ["title", "description"],
        },
    )
    def create(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new ProjectTask."""
        tenant = get_tenant(request)
        project_id = params.get("project_id")
        deadline_raw = params.get("deadline")
        t = ProjectTask.objects.create(
            title=params["title"],
            description=params["description"],
            assigned_role=params.get("assigned_role", ""),
            priority=params.get("priority", 50),
            deadline=deadline_raw if deadline_raw else None,
            tenant=tenant,
            project_id=uuid.UUID(project_id) if project_id else None,
        )
        return _task_dict(t)

    @mcp_action(
        "get",
        "Get full details for a specific task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single task's details."""
        return _task_dict(_get_task_or_raise(params["task_id"], request))

    @mcp_action(
        "list",
        "List tasks with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project UUID."},
                "status": {"type": "string", "description": "Filter by status."},
                "assigned_role": {"type": "string", "description": "Filter by assigned role."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Max tasks to return (default 50).",
                },
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return tasks scoped by tenant, optionally filtered."""
        qs = scope_qs(ProjectTask.objects.all(), request)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if role := params.get("assigned_role"):
            qs = qs.filter(assigned_role=role)
        limit: int = params.get("limit", 50)
        return {"tasks": [_task_dict(t) for t in qs[:limit]]}

    @mcp_action(
        "update",
        "Update task fields.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "integer"},
                "deadline": {"type": "string"},
                "assigned_role": {"type": "string"},
                "result_summary": {"type": "string"},
            },
            "required": ["task_id"],
        },
    )
    def update(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Update allowed fields on an existing task."""
        t = _get_task_or_raise(params["task_id"], request)
        updatable = (
            "title", "description", "status", "priority",
            "deadline", "assigned_role", "result_summary",
        )
        for field in updatable:
            if field in params:
                setattr(t, field, params[field])
        t.save()
        return _task_dict(t)

    @mcp_action(
        "lease_next",
        "Atomically lease the next available task for a role.",
        input_schema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Role to claim a task for."},
                "worker_id": {
                    "type": "string",
                    "description": "Optional worker UUID to record as lease owner.",
                },
                "lease_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 3600,
                    "description": f"Lease duration in seconds (default {_DEFAULT_LEASE_SECONDS}).",
                },
            },
            "required": ["role"],
        },
    )
    def lease_next(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Atomically claim the highest-priority ready task for the given role."""
        role: str = params["role"]
        worker_id = params.get("worker_id")
        lease_seconds: int = params.get("lease_seconds", _DEFAULT_LEASE_SECONDS)

        with transaction.atomic():
            task = (
                scope_qs(
                    ProjectTask.objects.select_for_update(skip_locked=True), request
                )
                .filter(status="ready", assigned_role=role)
                .order_by("priority", "created_at")
                .first()
            )
            if task is None:
                return {"task": None, "message": "No available tasks for this role."}
            task.status = "in_progress"
            task.lease_owner = uuid.UUID(worker_id) if worker_id else None
            task.lease_expires_at = timezone.now() + timedelta(seconds=lease_seconds)
            task.heartbeat_at = timezone.now()
            task.save(update_fields=[
                "status", "lease_owner", "lease_expires_at", "heartbeat_at", "updated_at",
            ])

        return {"task": _task_dict(task), "message": f"Task {task.title!r} leased successfully."}

    @mcp_action(
        "heartbeat",
        "Extend the lease on an in-progress task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "lease_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 3600,
                    "description": (
                        f"New lease duration from now (default {_DEFAULT_LEASE_SECONDS})."
                    ),
                },
            },
            "required": ["task_id"],
        },
    )
    def heartbeat(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Extend the lease expiry and update heartbeat_at."""
        t = _get_task_or_raise(params["task_id"], request)
        _require_status(t, "in_progress")
        lease_seconds: int = params.get("lease_seconds", _DEFAULT_LEASE_SECONDS)
        t.heartbeat_at = timezone.now()
        t.lease_expires_at = timezone.now() + timedelta(seconds=lease_seconds)
        t.save(update_fields=["heartbeat_at", "lease_expires_at", "updated_at"])
        return {
            "task_id": str(t.id),
            "heartbeat_at": t.heartbeat_at.isoformat(),
            "lease_expires_at": t.lease_expires_at.isoformat(),
        }

    @mcp_action(
        "complete",
        "Mark a task as done.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "result_summary": {
                    "type": "string",
                    "description": "Summary of the work done.",
                },
            },
            "required": ["task_id"],
        },
    )
    def complete(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition an in-progress task to done."""
        t = _get_task_or_raise(params["task_id"], request)
        _require_status(t, "in_progress")
        t.status = "done"
        t.result_summary = params.get("result_summary", "")
        t.lease_owner = None
        t.lease_expires_at = None
        t.save(update_fields=[
            "status", "result_summary", "lease_owner", "lease_expires_at", "updated_at",
        ])
        return _task_dict(t)

    @mcp_action(
        "fail",
        "Mark a task as failed.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "failure_reason": {"type": "string", "description": "Reason for failure."},
            },
            "required": ["task_id"],
        },
    )
    def fail(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition an in-progress task to failed."""
        t = _get_task_or_raise(params["task_id"], request)
        _require_status(t, "in_progress")
        t.status = "failed"
        t.blocked_reason = params.get("failure_reason", "")
        t.lease_owner = None
        t.lease_expires_at = None
        t.save(update_fields=[
            "status", "blocked_reason", "lease_owner", "lease_expires_at", "updated_at",
        ])
        return _task_dict(t)

    @mcp_action(
        "block",
        "Mark a task as blocked.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "reason": {"type": "string", "description": "Why the task is blocked."},
            },
            "required": ["task_id", "reason"],
        },
    )
    def block(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition a ready or in-progress task to blocked."""
        t = _get_task_or_raise(params["task_id"], request)
        _require_status(t, "ready", "in_progress")
        t.status = "blocked"
        t.blocked_reason = params["reason"]
        t.save(update_fields=["status", "blocked_reason", "updated_at"])
        return _task_dict(t)

    @mcp_action(
        "unblock",
        "Return a blocked task to ready.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
    )
    def unblock(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Transition a blocked task back to ready."""
        t = _get_task_or_raise(params["task_id"], request)
        _require_status(t, "blocked")
        t.status = "ready"
        t.blocked_reason = ""
        t.save(update_fields=["status", "blocked_reason", "updated_at"])
        return _task_dict(t)

    @mcp_action(
        "get_comments",
        "Get comments for a task (stub — returns empty list in v1).",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
    )
    def get_comments(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return task comments (stub for v1)."""
        _get_task_or_raise(params["task_id"], request)
        return {"task_id": params["task_id"], "comments": []}

    @mcp_action(
        "get_artifacts",
        "Get artifacts linked to a task (stub — returns empty list in v1).",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
    )
    def get_artifacts(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return task artifacts (stub for v1)."""
        _get_task_or_raise(params["task_id"], request)
        return {"task_id": params["task_id"], "artifacts": []}

    @mcp_action(
        "get_relationships",
        "Get task relationships (stub — returns empty list in v1).",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
    )
    def get_relationships(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return task relationships (stub for v1)."""
        _get_task_or_raise(params["task_id"], request)
        return {"task_id": params["task_id"], "relationships": []}
