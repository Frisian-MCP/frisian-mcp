"""Tests for the workers @mcp_dispatcher and WorkerHeartbeatMiddleware."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from friese_mcp.contrib.coordination.middleware import WorkerHeartbeatMiddleware
from friese_mcp.contrib.coordination.models import Room, RoomNote, Worker
from friese_mcp.contrib.coordination.tools.workers import WorkersDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None, worker_id: Any = None) -> MagicMock:
    """Return a mock request with optional tenant and worker_id."""
    req = MagicMock()
    req.user.tenant = tenant
    req.user.worker_id = worker_id
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the WorkersDispatcher directly."""
    dispatcher = WorkersDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersRegister:
    """Tests for the register action."""

    def test_register_creates_worker(self) -> None:
        """Register creates a Worker row and returns expected keys."""
        result = _dispatch("register", {"name": "agent-1", "role": "python-development"})
        assert Worker.objects.filter(id=result["worker_id"]).exists()

    def test_register_returns_worker_id(self) -> None:
        """Register response includes worker_id as UUID string."""
        result = _dispatch("register", {"name": "agent-2", "role": "pm"})
        uuid.UUID(result["worker_id"])  # raises if not valid UUID

    def test_register_sets_status_active(self) -> None:
        """Registered worker status is active."""
        result = _dispatch("register", {"name": "agent-3", "role": "eng"})
        assert result["status"] == "active"

    def test_register_with_capabilities(self) -> None:
        """Capabilities list is persisted correctly."""
        result = _dispatch(
            "register",
            {"name": "agent-4", "role": "eng", "capabilities": ["django", "postgres"]},
        )
        w = Worker.objects.get(id=result["worker_id"])
        assert w.capabilities == ["django", "postgres"]

    def test_register_stamps_last_heartbeat(self) -> None:
        """Register sets last_heartbeat to now."""
        result = _dispatch("register", {"name": "agent-5", "role": "pm"})
        w = Worker.objects.get(id=result["worker_id"])
        assert w.last_heartbeat is not None


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersGet:
    """Tests for the get action."""

    def test_get_returns_worker(self) -> None:
        """Get returns the worker dict for a valid worker_id."""
        w = Worker.objects.create(name="bot", role="pm")
        result = _dispatch("get", {"worker_id": str(w.id)})
        assert result["worker_id"] == str(w.id)
        assert result["role"] == "pm"

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent worker_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"worker_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all workers."""
        Worker.objects.create(name="w1", role="pm")
        Worker.objects.create(name="w2", role="eng")
        result = _dispatch("list", {})
        assert len(result["workers"]) >= 2

    def test_list_filters_by_status(self) -> None:
        """List with status filter returns only matching workers."""
        Worker.objects.create(name="active-w", role="pm", status="active")
        Worker.objects.create(name="stale-w", role="pm", status="stale")
        result = _dispatch("list", {"status": "stale"})
        assert all(w["status"] == "stale" for w in result["workers"])

    def test_list_filters_by_role(self) -> None:
        """List with role filter returns only workers with that role."""
        Worker.objects.create(name="pm-w", role="pm")
        Worker.objects.create(name="eng-w", role="eng")
        result = _dispatch("list", {"role": "pm"})
        assert all(w["role"] == "pm" for w in result["workers"])


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersHeartbeat:
    """Tests for the heartbeat action."""

    def test_heartbeat_updates_last_heartbeat(self) -> None:
        """Heartbeat stamps last_heartbeat to now."""
        w = Worker.objects.create(name="hb-w", role="pm")
        before = timezone.now()
        _dispatch("heartbeat", {"worker_id": str(w.id)})
        w.refresh_from_db()
        assert w.last_heartbeat >= before

    def test_heartbeat_sets_status_active(self) -> None:
        """Heartbeat sets status to active even if previously stale."""
        w = Worker.objects.create(name="stale-hb", role="pm", status="stale")
        _dispatch("heartbeat", {"worker_id": str(w.id)})
        w.refresh_from_db()
        assert w.status == "active"

    def test_heartbeat_returns_expected_keys(self) -> None:
        """Heartbeat response has worker_id, status, last_heartbeat."""
        w = Worker.objects.create(name="keys-hb", role="pm")
        result = _dispatch("heartbeat", {"worker_id": str(w.id)})
        assert {"worker_id", "status", "last_heartbeat"} <= result.keys()

    def test_heartbeat_raises_for_unknown_id(self) -> None:
        """Heartbeat raises LookupError for a non-existent worker_id."""
        with pytest.raises(LookupError):
            _dispatch("heartbeat", {"worker_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# deregister
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersDeregister:
    """Tests for the deregister action."""

    def test_deregister_sets_disconnected(self) -> None:
        """Deregister sets status to disconnected."""
        w = Worker.objects.create(name="bye-w", role="pm")
        _dispatch("deregister", {"worker_id": str(w.id)})
        w.refresh_from_db()
        assert w.status == "disconnected"

    def test_deregister_returns_status(self) -> None:
        """Deregister response includes worker_id and status=disconnected."""
        w = Worker.objects.create(name="bye2-w", role="pm")
        result = _dispatch("deregister", {"worker_id": str(w.id)})
        assert result["status"] == "disconnected"
        assert result["worker_id"] == str(w.id)

    def test_deregister_raises_for_unknown_id(self) -> None:
        """Deregister raises LookupError for a non-existent worker_id."""
        with pytest.raises(LookupError):
            _dispatch("deregister", {"worker_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkersActivity:
    """Tests for the activity action."""

    def test_activity_returns_notes(self) -> None:
        """Activity returns notes posted by the worker."""
        w = Worker.objects.create(name="act-w", role="pm")
        room = Room.objects.create(name="test-room")
        RoomNote.objects.create(room=room, worker_id=w.id, content="hello", agent_role="pm")
        result = _dispatch("activity", {"worker_id": str(w.id)})
        assert len(result["notes"]) == 1
        assert result["notes"][0]["content"] == "hello"

    def test_activity_respects_limit(self) -> None:
        """Activity limits results to the limit param."""
        w = Worker.objects.create(name="limit-w", role="pm")
        room = Room.objects.create(name="big-room")
        for i in range(5):
            RoomNote.objects.create(
                room=room, worker_id=w.id, content=f"msg {i}", agent_role="pm"
            )
        result = _dispatch("activity", {"worker_id": str(w.id), "limit": 3})
        assert len(result["notes"]) == 3

    def test_activity_raises_for_unknown_id(self) -> None:
        """Activity raises LookupError for a non-existent worker_id."""
        with pytest.raises(LookupError):
            _dispatch("activity", {"worker_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# WorkerHeartbeatMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkerHeartbeatMiddleware:
    """Tests for WorkerHeartbeatMiddleware."""

    def test_heartbeat_stamp_on_call(self) -> None:
        """Middleware stamps last_heartbeat and status=active on the worker."""
        w = Worker.objects.create(name="mw-w", role="pm", status="stale")
        request = _request(worker_id=w.id)
        before = timezone.now()

        call_next = MagicMock(return_value={"ok": True})
        mw = WorkerHeartbeatMiddleware()
        mw(request, "some_tool", {}, call_next)

        w.refresh_from_db()
        assert w.last_heartbeat >= before
        assert w.status == "active"

    def test_middleware_calls_next(self) -> None:
        """Middleware always delegates to call_next."""
        w = Worker.objects.create(name="mw-next", role="pm")
        request = _request(worker_id=w.id)
        call_next = MagicMock(return_value={"result": 42})
        mw = WorkerHeartbeatMiddleware()
        result = mw(request, "any_tool", {}, call_next)
        call_next.assert_called_once_with(request, "any_tool", {})
        assert result == {"result": 42}

    def test_middleware_noop_when_no_worker_id(self) -> None:
        """Middleware silently no-ops when request.user.worker_id is None."""
        request = _request(worker_id=None)
        call_next = MagicMock(return_value={})
        mw = WorkerHeartbeatMiddleware()
        mw(request, "tool", {}, call_next)
        call_next.assert_called_once()

    def test_middleware_noop_when_worker_id_not_found(self) -> None:
        """Middleware silently no-ops when the worker_id matches no row."""
        request = _request(worker_id=uuid.uuid4())
        call_next = MagicMock(return_value={})
        mw = WorkerHeartbeatMiddleware()
        mw(request, "tool", {}, call_next)
        call_next.assert_called_once()
