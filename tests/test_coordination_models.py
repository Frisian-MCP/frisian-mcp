"""Tests for contrib.coordination models and scope_qs helper."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

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
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_request(tenant: Any = None) -> MagicMock:
    """Return a mock request whose user.tenant equals *tenant*."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


# ---------------------------------------------------------------------------
# TestCoordinationTenant
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCoordinationTenant:
    """Tests for CoordinationTenant model."""

    def test_create(self) -> None:
        """Can create a tenant and retrieve it."""
        t = CoordinationTenant.objects.create(name="Acme Corp")
        assert CoordinationTenant.objects.filter(pk=t.pk).exists()

    def test_str(self) -> None:
        """__str__ returns the tenant name."""
        t = CoordinationTenant(name="Acme Corp")
        assert str(t) == "Acme Corp"

    def test_uuid_pk(self) -> None:
        """Primary key is a UUID."""
        t = CoordinationTenant.objects.create(name="UUID test")
        assert isinstance(t.pk, uuid.UUID)


# ---------------------------------------------------------------------------
# TestProject
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProject:
    """Tests for Project model."""

    def test_create_without_tenant(self) -> None:
        """Can create a project without a tenant (single-tenant)."""
        p = Project.objects.create(name="Alpha", status="active")
        assert p.tenant is None

    def test_create_with_tenant(self) -> None:
        """Can create a project scoped to a tenant."""
        tenant = CoordinationTenant.objects.create(name="T1")
        p = Project.objects.create(name="Beta", tenant=tenant)
        assert p.tenant == tenant

    def test_str(self) -> None:
        """__str__ includes name and status."""
        p = Project(name="Gamma", status="draft")
        assert str(p) == "Gamma (draft)"

    def test_default_status(self) -> None:
        """Default status is 'active'."""
        p = Project.objects.create(name="Default")
        assert p.status == "active"


# ---------------------------------------------------------------------------
# TestRoom
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoom:
    """Tests for Room model."""

    def test_create(self) -> None:
        """Can create a room linked to a project."""
        project = Project.objects.create(name="P1")
        r = Room.objects.create(name="general", project=project)
        assert r.project == project

    def test_str(self) -> None:
        """__str__ includes name and status."""
        r = Room(name="dev", status="active")
        assert str(r) == "dev (active)"

    def test_default_status(self) -> None:
        """Default room status is 'active'."""
        r = Room.objects.create(name="lobby")
        assert r.status == "active"


# ---------------------------------------------------------------------------
# TestRoomNote
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomNote:
    """Tests for RoomNote model."""

    def test_create(self) -> None:
        """Can create a note inside a room."""
        room = Room.objects.create(name="chat")
        note = RoomNote.objects.create(room=room, content="Hello", agent_role="pm")
        assert note.room == room

    def test_str_with_role(self) -> None:
        """__str__ includes role and truncated content."""
        note = RoomNote(agent_role="worker", content="Short message")
        assert str(note) == "[worker] Short message"

    def test_str_without_role(self) -> None:
        """__str__ uses 'anon' when agent_role is blank."""
        note = RoomNote(agent_role="", content="Anonymous note")
        assert str(note) == "[anon] Anonymous note"

    def test_str_truncates_long_content(self) -> None:
        """__str__ truncates content to 50 characters."""
        note = RoomNote(agent_role="bot", content="x" * 100)
        assert len(str(note)) <= 60  # "[bot] " + 50 chars


# ---------------------------------------------------------------------------
# TestWorker
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorker:
    """Tests for Worker model."""

    def test_create(self) -> None:
        """Can create a worker."""
        w = Worker.objects.create(name="agent-1", role="python-development")
        assert w.status == "active"

    def test_str(self) -> None:
        """__str__ includes name, role, and status."""
        w = Worker(name="bot", role="pm", status="stale")
        assert str(w) == "bot (pm, stale)"

    def test_capabilities_default(self) -> None:
        """Capabilities defaults to empty list."""
        w = Worker.objects.create(name="blank", role="test")
        assert w.capabilities == []


# ---------------------------------------------------------------------------
# TestProjectTask
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectTask:
    """Tests for ProjectTask model."""

    def test_create(self) -> None:
        """Can create a task with default status 'ready'."""
        t = ProjectTask.objects.create(title="Do something", description="Details")
        assert t.status == "ready"

    def test_str(self) -> None:
        """__str__ includes title and status."""
        t = ProjectTask(title="Fix bug", status="in_progress")
        assert str(t) == "Fix bug (in_progress)"

    def test_lease_fields_nullable(self) -> None:
        """Lease fields are nullable by default."""
        t = ProjectTask.objects.create(title="Leased", description="")
        assert t.lease_owner is None
        assert t.lease_expires_at is None


# ---------------------------------------------------------------------------
# TestArtifact
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifact:
    """Tests for Artifact model."""

    def test_create(self) -> None:
        """Can create an artifact with default version 1."""
        a = Artifact.objects.create(name="plan.md", content="# Plan", artifact_type="plan")
        assert a.version == 1

    def test_str(self) -> None:
        """__str__ includes name, version, and type."""
        a = Artifact(name="spec", version=3, artifact_type="spec")
        assert str(a) == "spec v3 (spec)"


# ---------------------------------------------------------------------------
# TestScratchpad
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScratchpad:
    """Tests for Scratchpad model."""

    def test_create(self) -> None:
        """Can create a scratchpad."""
        s = Scratchpad.objects.create(title="Notes", content="Draft")
        assert s.agent_role == ""

    def test_str_with_role(self) -> None:
        """__str__ includes title and agent_role."""
        s = Scratchpad(title="Scratch", agent_role="researcher")
        assert str(s) == "Scratch (researcher)"

    def test_str_without_role(self) -> None:
        """__str__ uses 'anon' when agent_role is blank."""
        s = Scratchpad(title="Anon note", agent_role="")
        assert str(s) == "Anon note (anon)"


# ---------------------------------------------------------------------------
# TestApprovalRequest
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalRequest:
    """Tests for ApprovalRequest model."""

    def test_create(self) -> None:
        """Can create an approval request with default status 'pending'."""
        a = ApprovalRequest.objects.create(
            title="Deploy to prod",
            description="Ready to ship",
            requesting_agent="pm",
        )
        assert a.status == "pending"

    def test_str(self) -> None:
        """__str__ includes title and status."""
        a = ApprovalRequest(title="Merge PR", status="approved")
        assert str(a) == "Merge PR (approved)"


# ---------------------------------------------------------------------------
# TestScopeQs
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScopeQs:
    """Tests for scope_qs + get_tenant helpers."""

    def test_scope_qs_without_tenant_returns_all(self) -> None:
        """scope_qs with no tenant returns the full queryset."""
        Project.objects.create(name="P1")
        Project.objects.create(name="P2")
        request = _mock_request(tenant=None)
        qs = scope_qs(Project.objects.all(), request)
        assert qs.count() == 2

    def test_scope_qs_with_tenant_filters(self) -> None:
        """scope_qs with a tenant returns only that tenant's rows."""
        tenant_a = CoordinationTenant.objects.create(name="A")
        tenant_b = CoordinationTenant.objects.create(name="B")
        Project.objects.create(name="A-project", tenant=tenant_a)
        Project.objects.create(name="B-project", tenant=tenant_b)
        Project.objects.create(name="No-tenant")

        request = _mock_request(tenant=tenant_a)
        qs = scope_qs(Project.objects.all(), request)
        assert qs.count() == 1
        assert qs.first().name == "A-project"

    def test_get_tenant_from_user(self) -> None:
        """get_tenant reads tenant from request.user.tenant."""
        tenant = CoordinationTenant.objects.create(name="X")
        request = _mock_request(tenant=tenant)
        assert get_tenant(request) == tenant

    def test_get_tenant_returns_none_when_no_tenant(self) -> None:
        """get_tenant returns None when user has no tenant attribute."""
        request = _mock_request(tenant=None)
        assert get_tenant(request) is None
