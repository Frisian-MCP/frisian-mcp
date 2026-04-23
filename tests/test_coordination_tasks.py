"""Tests for the tasks @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import ProjectTask
from friese_mcp.contrib.coordination.tools.tasks import TasksDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the TasksDispatcher directly."""
    dispatcher = TasksDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


def _make_task(**kwargs: Any) -> ProjectTask:
    """Create a ProjectTask with sensible defaults."""
    defaults = {"title": "Default task", "description": "desc", "assigned_role": "pm"}
    defaults.update(kwargs)
    return ProjectTask.objects.create(**defaults)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksCreate:
    """Tests for the create action."""

    def test_create_returns_task_id(self) -> None:
        """Create returns a valid UUID task_id."""
        result = _dispatch("create", {"title": "Build feature", "description": "Details"})
        uuid.UUID(result["task_id"])

    def test_create_default_status_ready(self) -> None:
        """Newly created tasks have status=ready."""
        result = _dispatch("create", {"title": "T1", "description": "d"})
        assert result["status"] == "ready"

    def test_create_default_priority(self) -> None:
        """Default priority is 50."""
        result = _dispatch("create", {"title": "T2", "description": "d"})
        assert result["priority"] == 50

    def test_create_with_role(self) -> None:
        """assigned_role is stored correctly."""
        result = _dispatch(
            "create", {"title": "T3", "description": "d", "assigned_role": "eng"}
        )
        assert result["assigned_role"] == "eng"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksGet:
    """Tests for the get action."""

    def test_get_returns_task(self) -> None:
        """Get returns the task dict for a valid task_id."""
        t = _make_task(title="Find me")
        result = _dispatch("get", {"task_id": str(t.id)})
        assert result["task_id"] == str(t.id)
        assert result["title"] == "Find me"

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent task_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"task_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksList:
    """Tests for the list action."""

    def test_list_returns_tasks(self) -> None:
        """List returns all tasks when no filters are set."""
        _make_task(title="L1")
        _make_task(title="L2")
        result = _dispatch("list", {})
        assert len(result["tasks"]) >= 2

    def test_list_filters_by_status(self) -> None:
        """List with status filter returns only matching tasks."""
        _make_task(title="ready-t", status="ready")
        _make_task(title="done-t", status="done")
        result = _dispatch("list", {"status": "done"})
        assert all(t["status"] == "done" for t in result["tasks"])

    def test_list_filters_by_role(self) -> None:
        """List with assigned_role filter returns only matching tasks."""
        _make_task(title="pm-t", assigned_role="pm")
        _make_task(title="eng-t", assigned_role="eng")
        result = _dispatch("list", {"assigned_role": "pm"})
        assert all(t["assigned_role"] == "pm" for t in result["tasks"])

    def test_list_respects_limit(self) -> None:
        """List respects the limit param."""
        for i in range(5):
            _make_task(title=f"limit-t{i}")
        result = _dispatch("list", {"limit": 2})
        assert len(result["tasks"]) <= 2


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksUpdate:
    """Tests for the update action."""

    def test_update_title(self) -> None:
        """Update changes the title field."""
        t = _make_task()
        _dispatch("update", {"task_id": str(t.id), "title": "New title"})
        t.refresh_from_db()
        assert t.title == "New title"

    def test_update_priority(self) -> None:
        """Update changes the priority field."""
        t = _make_task()
        _dispatch("update", {"task_id": str(t.id), "priority": 10})
        t.refresh_from_db()
        assert t.priority == 10

    def test_update_raises_for_unknown_id(self) -> None:
        """Update raises LookupError for a non-existent task_id."""
        with pytest.raises(LookupError):
            _dispatch("update", {"task_id": str(uuid.uuid4()), "title": "x"})


# ---------------------------------------------------------------------------
# lease_next
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksLeaseNext:
    """Tests for the lease_next action."""

    def test_lease_next_claims_ready_task(self) -> None:
        """lease_next returns the highest-priority ready task for the role."""
        _make_task(title="Claimable", assigned_role="pm", status="ready", priority=1)
        result = _dispatch("lease_next", {"role": "pm"})
        assert result["task"] is not None
        assert result["task"]["status"] == "in_progress"

    def test_lease_next_sets_lease_expires_at(self) -> None:
        """lease_next sets lease_expires_at on the task."""
        _make_task(assigned_role="eng", status="ready")
        result = _dispatch("lease_next", {"role": "eng"})
        assert result["task"]["lease_expires_at"] is not None

    def test_lease_next_returns_none_when_no_tasks(self) -> None:
        """lease_next returns task=None when no tasks are available."""
        result = _dispatch("lease_next", {"role": "nonexistent-role"})
        assert result["task"] is None

    def test_lease_next_skips_in_progress_tasks(self) -> None:
        """lease_next does not return tasks already in_progress."""
        _make_task(title="In flight", assigned_role="pm", status="in_progress")
        result = _dispatch("lease_next", {"role": "pm"})
        assert result["task"] is None

    def test_lease_next_with_worker_id(self) -> None:
        """lease_next records worker_id as lease_owner."""
        _make_task(assigned_role="pm", status="ready")
        wid = str(uuid.uuid4())
        result = _dispatch("lease_next", {"role": "pm", "worker_id": wid})
        assert result["task"]["lease_owner"] == wid


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksHeartbeat:
    """Tests for the heartbeat action."""

    def test_heartbeat_updates_lease(self) -> None:
        """Heartbeat extends lease_expires_at."""
        t = _make_task(status="in_progress")
        result = _dispatch("heartbeat", {"task_id": str(t.id)})
        assert "lease_expires_at" in result

    def test_heartbeat_requires_in_progress(self) -> None:
        """Heartbeat raises ValueError if task is not in_progress."""
        t = _make_task(status="ready")
        with pytest.raises(ValueError):
            _dispatch("heartbeat", {"task_id": str(t.id)})


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksComplete:
    """Tests for the complete action."""

    def test_complete_sets_done(self) -> None:
        """Complete transitions task to done."""
        t = _make_task(status="in_progress")
        _dispatch("complete", {"task_id": str(t.id), "result_summary": "Done!"})
        t.refresh_from_db()
        assert t.status == "done"

    def test_complete_stores_result_summary(self) -> None:
        """Complete stores result_summary."""
        t = _make_task(status="in_progress")
        _dispatch("complete", {"task_id": str(t.id), "result_summary": "All green"})
        t.refresh_from_db()
        assert t.result_summary == "All green"

    def test_complete_clears_lease(self) -> None:
        """Complete clears lease_owner and lease_expires_at."""
        t = _make_task(status="in_progress")
        t.lease_owner = uuid.uuid4()
        t.save()
        _dispatch("complete", {"task_id": str(t.id)})
        t.refresh_from_db()
        assert t.lease_owner is None

    def test_complete_requires_in_progress(self) -> None:
        """Complete raises ValueError if task is not in_progress."""
        t = _make_task(status="ready")
        with pytest.raises(ValueError):
            _dispatch("complete", {"task_id": str(t.id)})


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksFail:
    """Tests for the fail action."""

    def test_fail_sets_failed(self) -> None:
        """Fail transitions task to failed."""
        t = _make_task(status="in_progress")
        _dispatch("fail", {"task_id": str(t.id), "failure_reason": "Timeout"})
        t.refresh_from_db()
        assert t.status == "failed"

    def test_fail_requires_in_progress(self) -> None:
        """Fail raises ValueError if task is not in_progress."""
        t = _make_task(status="ready")
        with pytest.raises(ValueError):
            _dispatch("fail", {"task_id": str(t.id), "failure_reason": "oops"})


# ---------------------------------------------------------------------------
# block / unblock
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksBlockUnblock:
    """Tests for the block and unblock actions."""

    def test_block_from_ready(self) -> None:
        """Block transitions a ready task to blocked."""
        t = _make_task(status="ready")
        _dispatch("block", {"task_id": str(t.id), "reason": "waiting on infra"})
        t.refresh_from_db()
        assert t.status == "blocked"
        assert t.blocked_reason == "waiting on infra"

    def test_block_from_in_progress(self) -> None:
        """Block transitions an in_progress task to blocked."""
        t = _make_task(status="in_progress")
        _dispatch("block", {"task_id": str(t.id), "reason": "external dep"})
        t.refresh_from_db()
        assert t.status == "blocked"

    def test_block_rejects_done(self) -> None:
        """Block raises ValueError for a done task."""
        t = _make_task(status="done")
        with pytest.raises(ValueError):
            _dispatch("block", {"task_id": str(t.id), "reason": "late block"})

    def test_unblock_returns_to_ready(self) -> None:
        """Unblock transitions a blocked task back to ready."""
        t = _make_task(status="blocked")
        t.blocked_reason = "held"
        t.save()
        _dispatch("unblock", {"task_id": str(t.id)})
        t.refresh_from_db()
        assert t.status == "ready"
        assert t.blocked_reason == ""

    def test_unblock_requires_blocked(self) -> None:
        """Unblock raises ValueError if task is not blocked."""
        t = _make_task(status="ready")
        with pytest.raises(ValueError):
            _dispatch("unblock", {"task_id": str(t.id)})


# ---------------------------------------------------------------------------
# Stub actions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasksStubs:
    """Tests for the v1 stub actions."""

    def test_get_comments_returns_empty(self) -> None:
        """get_comments returns an empty list."""
        t = _make_task()
        result = _dispatch("get_comments", {"task_id": str(t.id)})
        assert not result["comments"]

    def test_get_artifacts_returns_empty(self) -> None:
        """get_artifacts returns an empty list."""
        t = _make_task()
        result = _dispatch("get_artifacts", {"task_id": str(t.id)})
        assert not result["artifacts"]

    def test_get_relationships_returns_empty(self) -> None:
        """get_relationships returns an empty list."""
        t = _make_task()
        result = _dispatch("get_relationships", {"task_id": str(t.id)})
        assert not result["relationships"]

    def test_stubs_raise_for_unknown_task(self) -> None:
        """Stub actions raise LookupError for a non-existent task_id."""
        bad_id = str(uuid.uuid4())
        for action in ("get_comments", "get_artifacts", "get_relationships"):
            with pytest.raises(LookupError):
                _dispatch(action, {"task_id": bad_id})
