"""Tests for contrib.coordination admin registration and list_display."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.contrib.admin.sites import AdminSite

from friese_mcp.contrib.coordination.admin import (
    ApprovalRequestAdmin,
    ArtifactAdmin,
    CoordinationTenantAdmin,
    ProjectAdmin,
    ProjectTaskAdmin,
    RoomAdmin,
    RoomNoteAdmin,
    RoomNoteInline,
    ScratchpadAdmin,
    WorkerAdmin,
)
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


@pytest.fixture()
def admin_site() -> AdminSite:
    """Return a fresh AdminSite instance."""
    return AdminSite()


class TestAdminRegistration:
    """All coordination models are registered with the admin."""

    def test_coordination_tenant_registered(self, admin_site: AdminSite) -> None:
        """CoordinationTenantAdmin is bound to CoordinationTenant."""
        admin = CoordinationTenantAdmin(CoordinationTenant, admin_site)
        assert admin.model is CoordinationTenant

    def test_project_registered(self, admin_site: AdminSite) -> None:
        """ProjectAdmin is bound to Project."""
        admin = ProjectAdmin(Project, admin_site)
        assert admin.model is Project

    def test_room_registered(self, admin_site: AdminSite) -> None:
        """RoomAdmin is bound to Room."""
        admin = RoomAdmin(Room, admin_site)
        assert admin.model is Room

    def test_room_note_registered(self, admin_site: AdminSite) -> None:
        """RoomNoteAdmin is bound to RoomNote."""
        admin = RoomNoteAdmin(RoomNote, admin_site)
        assert admin.model is RoomNote

    def test_worker_registered(self, admin_site: AdminSite) -> None:
        """WorkerAdmin is bound to Worker."""
        admin = WorkerAdmin(Worker, admin_site)
        assert admin.model is Worker

    def test_project_task_registered(self, admin_site: AdminSite) -> None:
        """ProjectTaskAdmin is bound to ProjectTask."""
        admin = ProjectTaskAdmin(ProjectTask, admin_site)
        assert admin.model is ProjectTask

    def test_artifact_registered(self, admin_site: AdminSite) -> None:
        """ArtifactAdmin is bound to Artifact."""
        admin = ArtifactAdmin(Artifact, admin_site)
        assert admin.model is Artifact

    def test_scratchpad_registered(self, admin_site: AdminSite) -> None:
        """ScratchpadAdmin is bound to Scratchpad."""
        admin = ScratchpadAdmin(Scratchpad, admin_site)
        assert admin.model is Scratchpad

    def test_approval_request_registered(self, admin_site: AdminSite) -> None:
        """ApprovalRequestAdmin is bound to ApprovalRequest."""
        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        assert admin.model is ApprovalRequest


class TestListDisplay:
    """list_display tuples contain expected fields."""

    def test_coordination_tenant_list_display(self, admin_site: AdminSite) -> None:
        """CoordinationTenantAdmin shows name and created_at."""
        admin = CoordinationTenantAdmin(CoordinationTenant, admin_site)
        assert "name" in admin.list_display
        assert "created_at" in admin.list_display

    def test_project_list_display(self, admin_site: AdminSite) -> None:
        """ProjectAdmin shows name, status, and tenant."""
        admin = ProjectAdmin(Project, admin_site)
        assert "name" in admin.list_display
        assert "status" in admin.list_display
        assert "tenant" in admin.list_display

    def test_room_list_display(self, admin_site: AdminSite) -> None:
        """RoomAdmin shows name, status, and project."""
        admin = RoomAdmin(Room, admin_site)
        assert "name" in admin.list_display
        assert "status" in admin.list_display
        assert "project" in admin.list_display

    def test_room_note_list_display(self, admin_site: AdminSite) -> None:
        """RoomNoteAdmin shows room, agent_role, and is_human_message."""
        admin = RoomNoteAdmin(RoomNote, admin_site)
        assert "room" in admin.list_display
        assert "agent_role" in admin.list_display
        assert "is_human_message" in admin.list_display

    def test_worker_list_display(self, admin_site: AdminSite) -> None:
        """WorkerAdmin shows name, role, and status."""
        admin = WorkerAdmin(Worker, admin_site)
        assert "name" in admin.list_display
        assert "role" in admin.list_display
        assert "status" in admin.list_display

    def test_project_task_list_display(self, admin_site: AdminSite) -> None:
        """ProjectTaskAdmin shows title, status, and priority."""
        admin = ProjectTaskAdmin(ProjectTask, admin_site)
        assert "title" in admin.list_display
        assert "status" in admin.list_display
        assert "priority" in admin.list_display

    def test_artifact_list_display(self, admin_site: AdminSite) -> None:
        """ArtifactAdmin shows name, artifact_type, and version."""
        admin = ArtifactAdmin(Artifact, admin_site)
        assert "name" in admin.list_display
        assert "artifact_type" in admin.list_display
        assert "version" in admin.list_display

    def test_scratchpad_list_display(self, admin_site: AdminSite) -> None:
        """ScratchpadAdmin shows title and agent_role."""
        admin = ScratchpadAdmin(Scratchpad, admin_site)
        assert "title" in admin.list_display
        assert "agent_role" in admin.list_display

    def test_approval_request_list_display(self, admin_site: AdminSite) -> None:
        """ApprovalRequestAdmin shows title, status, and requesting_agent."""
        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        assert "title" in admin.list_display
        assert "status" in admin.list_display
        assert "requesting_agent" in admin.list_display


class TestRoomNoteInline:
    """RoomNoteInline is configured correctly."""

    def test_inline_model(self, admin_site: AdminSite) -> None:
        """Inline model is RoomNote."""
        inline = RoomNoteInline(Room, admin_site)
        assert inline.model is RoomNote

    def test_inline_extra_zero(self, admin_site: AdminSite) -> None:
        """extra=0 suppresses blank inline forms."""
        inline = RoomNoteInline(Room, admin_site)
        assert inline.extra == 0

    def test_inline_cannot_delete(self, admin_site: AdminSite) -> None:
        """can_delete=False prevents note deletion from the Room admin."""
        inline = RoomNoteInline(Room, admin_site)
        assert inline.can_delete is False

    def test_room_admin_has_inline(self, admin_site: AdminSite) -> None:
        """RoomAdmin includes RoomNoteInline."""
        admin = RoomAdmin(Room, admin_site)
        assert RoomNoteInline in admin.inlines


class TestWorkerAdminActions:
    """WorkerAdmin actions are configured correctly."""

    def test_deregister_workers_action_present(self, admin_site: AdminSite) -> None:
        """deregister_workers action is listed on WorkerAdmin."""
        admin = WorkerAdmin(Worker, admin_site)
        assert "deregister_workers" in admin.actions

    @pytest.mark.django_db
    def test_deregister_workers_updates_status(self, admin_site: AdminSite) -> None:
        """deregister_workers sets all selected workers to disconnected."""
        Worker.objects.create(name="w1", role="pm", status="active")
        Worker.objects.create(name="w2", role="eng", status="stale")

        admin = WorkerAdmin(Worker, admin_site)
        admin.deregister_workers(MagicMock(), Worker.objects.all())

        assert Worker.objects.filter(status="disconnected").count() == 2


class TestApprovalRequestAdminActions:
    """ApprovalRequestAdmin actions approve and reject only pending rows."""

    def test_approve_action_present(self, admin_site: AdminSite) -> None:
        """approve_requests action is listed on ApprovalRequestAdmin."""
        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        assert "approve_requests" in admin.actions

    def test_reject_action_present(self, admin_site: AdminSite) -> None:
        """reject_requests action is listed on ApprovalRequestAdmin."""
        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        assert "reject_requests" in admin.actions

    @pytest.mark.django_db
    def test_approve_only_pending(self, admin_site: AdminSite) -> None:
        """approve_requests changes pending→approved but leaves non-pending unchanged."""
        ApprovalRequest.objects.create(
            title="A", description="", requesting_agent="pm", status="pending"
        )
        ApprovalRequest.objects.create(
            title="B", description="", requesting_agent="pm", status="rejected"
        )

        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        admin.approve_requests(MagicMock(), ApprovalRequest.objects.all())

        assert ApprovalRequest.objects.get(title="A").status == "approved"
        assert ApprovalRequest.objects.get(title="B").status == "rejected"

    @pytest.mark.django_db
    def test_reject_only_pending(self, admin_site: AdminSite) -> None:
        """reject_requests changes pending→rejected but leaves non-pending unchanged."""
        ApprovalRequest.objects.create(
            title="C", description="", requesting_agent="pm", status="pending"
        )
        ApprovalRequest.objects.create(
            title="D", description="", requesting_agent="pm", status="approved"
        )

        admin = ApprovalRequestAdmin(ApprovalRequest, admin_site)
        admin.reject_requests(MagicMock(), ApprovalRequest.objects.all())

        assert ApprovalRequest.objects.get(title="C").status == "rejected"
        assert ApprovalRequest.objects.get(title="D").status == "approved"
