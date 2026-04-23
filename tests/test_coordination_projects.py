"""Tests for the projects @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import Project, ProjectTask, Room
from friese_mcp.contrib.coordination.tools.projects import ProjectsDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the ProjectsDispatcher directly."""
    dispatcher = ProjectsDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsCreate:
    """Tests for the create action."""

    def test_create_returns_project_id(self) -> None:
        """Create returns a valid UUID project_id."""
        result = _dispatch("create", {"name": "Alpha"})
        uuid.UUID(result["project_id"])

    def test_create_persists_project(self) -> None:
        """Create persists the Project to the database."""
        result = _dispatch("create", {"name": "Beta"})
        assert Project.objects.filter(id=result["project_id"]).exists()

    def test_create_default_status_active(self) -> None:
        """Default status is active."""
        result = _dispatch("create", {"name": "Gamma"})
        assert result["status"] == "active"

    def test_create_with_description(self) -> None:
        """Description is stored correctly."""
        result = _dispatch("create", {"name": "Delta", "description": "A plan"})
        assert result["description"] == "A plan"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsGet:
    """Tests for the get action."""

    def test_get_returns_project(self) -> None:
        """Get returns the project dict for a valid project_id."""
        p = Project.objects.create(name="Epsilon")
        result = _dispatch("get", {"project_id": str(p.id)})
        assert result["project_id"] == str(p.id)
        assert result["name"] == "Epsilon"

    def test_get_includes_task_counts(self) -> None:
        """Get includes task_counts keyed by status."""
        p = Project.objects.create(name="WithTasks")
        ProjectTask.objects.create(
            title="t1", description="d", project=p, status="ready", assigned_role="pm"
        )
        ProjectTask.objects.create(
            title="t2", description="d", project=p, status="done", assigned_role="pm"
        )
        result = _dispatch("get", {"project_id": str(p.id)})
        assert result["task_counts"]["ready"] == 1
        assert result["task_counts"]["done"] == 1

    def test_get_includes_linked_rooms(self) -> None:
        """Get includes linked_rooms."""
        p = Project.objects.create(name="WithRooms")
        Room.objects.create(name="room-x", project=p)
        result = _dispatch("get", {"project_id": str(p.id)})
        assert len(result["linked_rooms"]) == 1
        assert result["linked_rooms"][0]["name"] == "room-x"

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent project_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"project_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all projects."""
        Project.objects.create(name="P1")
        Project.objects.create(name="P2")
        result = _dispatch("list", {})
        assert len(result["projects"]) >= 2

    def test_list_filters_by_status(self) -> None:
        """List with status filter returns only matching projects."""
        Project.objects.create(name="active-p", status="active")
        Project.objects.create(name="completed-p", status="completed")
        result = _dispatch("list", {"status": "completed"})
        assert all(p["status"] == "completed" for p in result["projects"])


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsUpdate:
    """Tests for the update action."""

    def test_update_name(self) -> None:
        """Update changes the name field."""
        p = Project.objects.create(name="Old name")
        _dispatch("update", {"project_id": str(p.id), "name": "New name"})
        p.refresh_from_db()
        assert p.name == "New name"

    def test_update_status(self) -> None:
        """Update changes the status field."""
        p = Project.objects.create(name="Status-p")
        _dispatch("update", {"project_id": str(p.id), "status": "completed"})
        p.refresh_from_db()
        assert p.status == "completed"

    def test_update_raises_for_unknown_id(self) -> None:
        """Update raises LookupError for a non-existent project_id."""
        with pytest.raises(LookupError):
            _dispatch("update", {"project_id": str(uuid.uuid4()), "name": "x"})


# ---------------------------------------------------------------------------
# get_plan
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsGetPlan:
    """Tests for the get_plan action."""

    def test_get_plan_returns_plan(self) -> None:
        """get_plan returns the plan JSON."""
        p = Project.objects.create(name="Planned", plan={"version": 1, "content": "v1"})
        result = _dispatch("get_plan", {"project_id": str(p.id)})
        assert result["plan"]["content"] == "v1"

    def test_get_plan_returns_none_when_no_plan(self) -> None:
        """get_plan returns plan=None when no plan is set."""
        p = Project.objects.create(name="No-plan")
        result = _dispatch("get_plan", {"project_id": str(p.id)})
        assert result["plan"] is None

    def test_get_plan_raises_for_unknown_id(self) -> None:
        """get_plan raises LookupError for a non-existent project_id."""
        with pytest.raises(LookupError):
            _dispatch("get_plan", {"project_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# update_plan
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsUpdatePlan:
    """Tests for the update_plan action."""

    def test_update_plan_stores_content(self) -> None:
        """update_plan stores the new content."""
        p = Project.objects.create(name="Plan-store")
        _dispatch("update_plan", {"project_id": str(p.id), "content": {"tasks": []}})
        p.refresh_from_db()
        assert p.plan["content"] == {"tasks": []}

    def test_update_plan_increments_version(self) -> None:
        """update_plan increments the version on each call."""
        p = Project.objects.create(name="Versioned")
        _dispatch("update_plan", {"project_id": str(p.id), "content": "v1"})
        result = _dispatch("update_plan", {"project_id": str(p.id), "content": "v2"})
        assert result["version"] == 2

    def test_update_plan_starts_at_version_1(self) -> None:
        """First update_plan sets version to 1."""
        p = Project.objects.create(name="Fresh-plan")
        result = _dispatch("update_plan", {"project_id": str(p.id), "content": "start"})
        assert result["version"] == 1

    def test_update_plan_raises_for_unknown_id(self) -> None:
        """update_plan raises LookupError for a non-existent project_id."""
        with pytest.raises(LookupError):
            _dispatch("update_plan", {"project_id": str(uuid.uuid4()), "content": "x"})


# ---------------------------------------------------------------------------
# list_rooms
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProjectsListRooms:
    """Tests for the list_rooms action."""

    def test_list_rooms_returns_linked_rooms(self) -> None:
        """list_rooms returns rooms linked to the project."""
        p = Project.objects.create(name="Room-proj")
        Room.objects.create(name="r-a", project=p)
        Room.objects.create(name="r-b", project=p)
        result = _dispatch("list_rooms", {"project_id": str(p.id)})
        assert len(result["rooms"]) == 2

    def test_list_rooms_filters_by_status(self) -> None:
        """list_rooms with status filter returns only matching rooms."""
        p = Project.objects.create(name="Filter-proj")
        Room.objects.create(name="open-r", project=p, status="active")
        Room.objects.create(name="closed-r", project=p, status="closed")
        result = _dispatch("list_rooms", {"project_id": str(p.id), "status": "closed"})
        assert all(r["status"] == "closed" for r in result["rooms"])

    def test_list_rooms_raises_for_unknown_project(self) -> None:
        """list_rooms raises LookupError for a non-existent project_id."""
        with pytest.raises(LookupError):
            _dispatch("list_rooms", {"project_id": str(uuid.uuid4())})
