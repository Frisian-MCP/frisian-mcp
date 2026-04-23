"""Data models for contrib.coordination — the multi-agent coordination layer."""

from __future__ import annotations

import uuid

from django.db import models

# ---------------------------------------------------------------------------
# Choice tuples
# ---------------------------------------------------------------------------

PROJECT_STATUS_CHOICES: list[tuple[str, str]] = [
    ("draft", "Draft"),
    ("active", "Active"),
    ("on_hold", "On Hold"),
    ("completed", "Completed"),
]

ROOM_STATUS_CHOICES: list[tuple[str, str]] = [
    ("active", "Active"),
    ("closed", "Closed"),
    ("archived", "Archived"),
]

WORKER_STATUS_CHOICES: list[tuple[str, str]] = [
    ("active", "Active"),
    ("stale", "Stale"),
    ("disconnected", "Disconnected"),
]

TASK_STATUS_CHOICES: list[tuple[str, str]] = [
    ("ready", "Ready"),
    ("in_progress", "In Progress"),
    ("blocked", "Blocked"),
    ("done", "Done"),
    ("failed", "Failed"),
]

ARTIFACT_TYPE_CHOICES: list[tuple[str, str]] = [
    ("note", "Note"),
    ("plan", "Plan"),
    ("spec", "Spec"),
    ("custom", "Custom"),
]

APPROVAL_STATUS_CHOICES: list[tuple[str, str]] = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
    ("cancelled", "Cancelled"),
]

# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


class CoordinationTenant(models.Model):
    """
    Optional tenant stub for multi-tenant installs.

    Single-tenant deployments leave the ``tenant`` FK on all other models
    as ``None``.  Multi-tenant deployments create one ``CoordinationTenant``
    per org/workspace and set the FK on each row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Coordination Tenant"
        verbose_name_plural = "Coordination Tenants"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return tenant name."""
        return str(self.name)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class Project(models.Model):
    """A coordination project grouping rooms, tasks, workers, and artifacts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=PROJECT_STATUS_CHOICES,
        default="active",
    )
    plan = models.JSONField(null=True, blank=True)
    created_by = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Project"
        verbose_name_plural = "Projects"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return project name and status."""
        return f"{self.name} ({self.status})"


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------


class Room(models.Model):
    """A discussion room attached to a project, scoped by tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rooms",
    )
    name = models.CharField(max_length=255)
    purpose = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=ROOM_STATUS_CHOICES,
        default="active",
    )
    outcome_type = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Room"
        verbose_name_plural = "Rooms"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="unique_room_name_per_tenant",
                condition=models.Q(tenant__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        """Return room name and status."""
        return f"{self.name} ({self.status})"


# ---------------------------------------------------------------------------
# RoomNote
# ---------------------------------------------------------------------------


class RoomNote(models.Model):
    """A single message posted in a room."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        "friese_mcp_coordination.Room",
        on_delete=models.CASCADE,
        related_name="notes",
    )
    agent_role = models.CharField(max_length=100, blank=True)
    worker_id = models.UUIDField(null=True, blank=True)
    content = models.TextField()
    is_human_message = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Room Note"
        verbose_name_plural = "Room Notes"
        ordering = ["created_at"]

    def __str__(self) -> str:
        """Return truncated content with agent role."""
        role = self.agent_role or "anon"
        return f"[{role}] {self.content[:50]}"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class Worker(models.Model):
    """A registered agent worker participating in coordination."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workers",
    )
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=WORKER_STATUS_CHOICES,
        default="active",
    )
    capabilities = models.JSONField(default=list)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Worker"
        verbose_name_plural = "Workers"
        ordering = ["-registered_at"]

    def __str__(self) -> str:
        """Return worker name, role, and status."""
        return f"{self.name} ({self.role}, {self.status})"


# ---------------------------------------------------------------------------
# ProjectTask
# ---------------------------------------------------------------------------


class ProjectTask(models.Model):
    """A discrete unit of work within a project, with lease support."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    title = models.CharField(max_length=500)
    description = models.TextField()
    assigned_role = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=20,
        choices=TASK_STATUS_CHOICES,
        default="ready",
    )
    priority = models.IntegerField(default=50)
    deadline = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.TextField(blank=True)
    result_summary = models.TextField(blank=True)
    lease_owner = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Project Task"
        verbose_name_plural = "Project Tasks"
        ordering = ["priority", "-created_at"]

    def __str__(self) -> str:
        """Return task title and status."""
        return f"{self.title} ({self.status})"


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


class Artifact(models.Model):
    """A versioned content artifact attached to a project."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="artifacts",
    )
    name = models.CharField(max_length=255)
    artifact_type = models.CharField(
        max_length=20,
        choices=ARTIFACT_TYPE_CHOICES,
        default="note",
    )
    content = models.TextField()
    version = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Artifact"
        verbose_name_plural = "Artifacts"
        ordering = ["name", "-version"]

    def __str__(self) -> str:
        """Return artifact name, version, and type."""
        return f"{self.name} v{self.version} ({self.artifact_type})"


# ---------------------------------------------------------------------------
# Scratchpad
# ---------------------------------------------------------------------------


class Scratchpad(models.Model):
    """An agent-scoped scratchpad for ephemeral notes within a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scratchpads",
    )
    title = models.CharField(max_length=255)
    content = models.TextField()
    agent_role = models.CharField(max_length=100, blank=True)
    session_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Scratchpad"
        verbose_name_plural = "Scratchpads"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        """Return scratchpad title and agent role."""
        role = self.agent_role or "anon"
        return f"{self.title} ({role})"


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------


class ApprovalRequest(models.Model):
    """A human-approval gate raised by an agent during a workflow."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "friese_mcp_coordination.CoordinationTenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )
    project = models.ForeignKey(
        "friese_mcp_coordination.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_requests",
    )
    title = models.CharField(max_length=500)
    description = models.TextField()
    requesting_agent = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=APPROVAL_STATUS_CHOICES,
        default="pending",
    )
    resolution_note = models.TextField(blank=True)
    resolved_by = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Approval Request"
        verbose_name_plural = "Approval Requests"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return approval request title and status."""
        return f"{self.title} ({self.status})"
