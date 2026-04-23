"""Django admin registration for contrib.coordination models."""

from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import (
    ApprovalRequest,
    Artifact,
    CoordinationTenant,
    Project,
    ProjectTask,
    Room,
    RoomNote,
    Scratchpad,
    Worker,
)


@admin.register(CoordinationTenant)
class CoordinationTenantAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for CoordinationTenant."""

    list_display = ("name", "created_at")
    search_fields = ("name",)
    readonly_fields = ("id", "created_at")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Project."""

    list_display = ("name", "status", "created_by", "tenant", "created_at")
    list_filter = ("status", "tenant", "created_at")
    search_fields = ("name", "description", "created_by")
    readonly_fields = ("id", "created_at", "updated_at")


class RoomNoteInline(admin.TabularInline):  # type: ignore[type-arg]
    """Inline display of RoomNotes inside the Room admin."""

    model = RoomNote
    extra = 0
    readonly_fields = ("id", "agent_role", "worker_id", "content", "is_human_message", "created_at")
    can_delete = False


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Room, with RoomNote inline."""

    list_display = ("name", "project", "status", "tenant", "created_at")
    list_filter = ("status", "tenant", "created_at")
    search_fields = ("name", "purpose")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [RoomNoteInline]


@admin.register(RoomNote)
class RoomNoteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for RoomNote."""

    list_display = ("room", "agent_role", "is_human_message", "created_at")
    list_filter = ("is_human_message", "created_at")
    search_fields = ("content", "agent_role")
    readonly_fields = ("id", "created_at")


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Worker."""

    list_display = ("name", "role", "status", "last_heartbeat", "tenant", "registered_at")
    list_filter = ("status", "tenant", "registered_at")
    search_fields = ("name", "role")
    readonly_fields = ("id", "registered_at", "updated_at")
    actions = ["deregister_workers"]

    @admin.action(description="Mark selected workers as disconnected")
    def deregister_workers(
        self,
        request: HttpRequest,
        queryset: QuerySet[Worker],
    ) -> None:
        """Set status=disconnected on all selected workers."""
        updated: int = queryset.update(status="disconnected")
        self.message_user(request, f"{updated} worker(s) marked as disconnected.")


@admin.register(ProjectTask)
class ProjectTaskAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for ProjectTask."""

    list_display = ("title", "assigned_role", "status", "priority", "project", "created_at")
    list_filter = ("status", "assigned_role", "tenant", "created_at")
    search_fields = ("title", "description", "assigned_role")
    readonly_fields = (
        "id",
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "created_at",
        "updated_at",
    )


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Artifact."""

    list_display = ("name", "artifact_type", "version", "project", "created_by", "created_at")
    list_filter = ("artifact_type", "tenant", "created_at")
    search_fields = ("name", "content", "created_by")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Scratchpad)
class ScratchpadAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Scratchpad."""

    list_display = ("title", "agent_role", "project", "tenant", "updated_at")
    list_filter = ("tenant", "updated_at")
    search_fields = ("title", "content", "agent_role")
    readonly_fields = ("id", "session_id", "created_at", "updated_at")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for ApprovalRequest."""

    list_display = ("title", "requesting_agent", "status", "project", "created_at")
    list_filter = ("status", "tenant", "created_at")
    search_fields = ("title", "description", "requesting_agent", "resolved_by")
    readonly_fields = ("id", "created_at", "updated_at")
    actions = ["approve_requests", "reject_requests"]

    @admin.action(description="Approve selected approval requests")
    def approve_requests(
        self,
        request: HttpRequest,
        queryset: QuerySet[ApprovalRequest],
    ) -> None:
        """Set status=approved on all selected pending approval requests."""
        updated: int = queryset.filter(status="pending").update(status="approved")
        self.message_user(request, f"{updated} approval request(s) approved.")

    @admin.action(description="Reject selected approval requests")
    def reject_requests(
        self,
        request: HttpRequest,
        queryset: QuerySet[ApprovalRequest],
    ) -> None:
        """Set status=rejected on all selected pending approval requests."""
        updated: int = queryset.filter(status="pending").update(status="rejected")
        self.message_user(request, f"{updated} approval request(s) rejected.")
