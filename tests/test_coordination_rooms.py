"""Tests for the rooms @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import (
    CoordinationTenant,
    Project,
    Room,
    RoomNote,
)
from friese_mcp.contrib.coordination.tools.rooms import RoomsDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the RoomsDispatcher directly."""
    dispatcher = RoomsDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsCreate:
    """Tests for the create action."""

    def test_create_returns_room_id(self) -> None:
        """Create returns a valid UUID room_id."""
        result = _dispatch("create", {"name": "general"})
        uuid.UUID(result["room_id"])

    def test_create_persists_room(self) -> None:
        """Create persists the Room to the database."""
        result = _dispatch("create", {"name": "dev"})
        assert Room.objects.filter(id=result["room_id"]).exists()

    def test_create_with_purpose(self) -> None:
        """Create stores the purpose field."""
        result = _dispatch("create", {"name": "discuss", "purpose": "Planning"})
        assert result["purpose"] == "Planning"

    def test_create_default_status_active(self) -> None:
        """Newly created rooms have status=active."""
        result = _dispatch("create", {"name": "active-room"})
        assert result["status"] == "active"

    def test_create_with_project_id(self) -> None:
        """Create links room to a project_id when supplied."""
        p = Project.objects.create(name="proj")
        result = _dispatch("create", {"name": "proj-room", "project_id": str(p.id)})
        assert result["project_id"] == str(p.id)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all rooms."""
        Room.objects.create(name="r1")
        Room.objects.create(name="r2")
        result = _dispatch("list", {})
        assert len(result["rooms"]) >= 2

    def test_list_filters_by_status(self) -> None:
        """List with status filter returns only matching rooms."""
        Room.objects.create(name="open-r", status="active")
        Room.objects.create(name="closed-r", status="closed")
        result = _dispatch("list", {"status": "closed"})
        assert all(r["status"] == "closed" for r in result["rooms"])

    def test_list_tenant_scoping(self) -> None:
        """List returns only the tenant's rooms when a tenant is set."""
        t_a = CoordinationTenant.objects.create(name="A")
        t_b = CoordinationTenant.objects.create(name="B")
        Room.objects.create(name="room-a", tenant=t_a)
        Room.objects.create(name="room-b", tenant=t_b)
        result = _dispatch("list", {}, request=_request(tenant=t_a))
        names = [r["name"] for r in result["rooms"]]
        assert "room-a" in names
        assert "room-b" not in names

    def test_list_no_tenant_returns_all(self) -> None:
        """List without a tenant returns all rooms (single-tenant mode)."""
        Room.objects.create(name="st-r1")
        Room.objects.create(name="st-r2")
        result = _dispatch("list", {}, request=_request(tenant=None))
        assert len(result["rooms"]) >= 2


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsRead:
    """Tests for the read action."""

    def test_read_returns_messages(self) -> None:
        """Read returns notes posted in the room."""
        room = Room.objects.create(name="chat")
        RoomNote.objects.create(room=room, content="hello", agent_role="pm")
        result = _dispatch("read", {"room_id": str(room.id)})
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "hello"

    def test_read_pagination(self) -> None:
        """Read with limit and offset returns correct slice."""
        room = Room.objects.create(name="paginated")
        for i in range(5):
            RoomNote.objects.create(room=room, content=f"msg {i}", agent_role="bot")
        result = _dispatch("read", {"room_id": str(room.id), "limit": 2, "offset": 1})
        assert len(result["messages"]) == 2
        assert result["has_more"] is True
        assert result["next_offset"] == 3

    def test_read_has_more_false_at_end(self) -> None:
        """Read has_more is False when all messages are returned."""
        room = Room.objects.create(name="small-room")
        RoomNote.objects.create(room=room, content="only", agent_role="pm")
        result = _dispatch("read", {"room_id": str(room.id)})
        assert result["has_more"] is False
        assert result["next_offset"] is None

    def test_read_raises_for_unknown_room(self) -> None:
        """Read raises LookupError for a non-existent room_id."""
        with pytest.raises(LookupError):
            _dispatch("read", {"room_id": str(uuid.uuid4())})

    def test_read_total_count(self) -> None:
        """Read returns the total note count for the room."""
        room = Room.objects.create(name="count-room")
        for i in range(3):
            RoomNote.objects.create(room=room, content=f"n{i}", agent_role="pm")
        result = _dispatch("read", {"room_id": str(room.id)})
        assert result["total"] == 3


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsPost:
    """Tests for the post action."""

    def test_post_creates_note(self) -> None:
        """Post creates a RoomNote and returns its note_id."""
        room = Room.objects.create(name="post-room")
        result = _dispatch("post", {"room_id": str(room.id), "content": "Hi there"})
        assert RoomNote.objects.filter(id=result["note_id"]).exists()

    def test_post_sets_agent_role(self) -> None:
        """Post stores the role param as agent_role."""
        room = Room.objects.create(name="role-room")
        result = _dispatch(
            "post", {"room_id": str(room.id), "content": "msg", "role": "pm"}
        )
        assert result["agent_role"] == "pm"

    def test_post_sets_worker_id(self) -> None:
        """Post stores worker_id when provided."""
        room = Room.objects.create(name="wid-room")
        wid = str(uuid.uuid4())
        result = _dispatch(
            "post", {"room_id": str(room.id), "content": "msg", "worker_id": wid}
        )
        assert result["worker_id"] == wid

    def test_post_raises_for_unknown_room(self) -> None:
        """Post raises LookupError for a non-existent room_id."""
        with pytest.raises(LookupError):
            _dispatch("post", {"room_id": str(uuid.uuid4()), "content": "x"})


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsClose:
    """Tests for the close action."""

    def test_close_sets_status(self) -> None:
        """Close sets room status to closed."""
        room = Room.objects.create(name="cl-room")
        _dispatch("close", {"room_id": str(room.id)})
        room.refresh_from_db()
        assert room.status == "closed"

    def test_close_sets_outcome_type(self) -> None:
        """Close stores outcome_type when provided."""
        room = Room.objects.create(name="ot-room")
        _dispatch("close", {"room_id": str(room.id), "outcome_type": "decision"})
        room.refresh_from_db()
        assert room.outcome_type == "decision"

    def test_close_raises_for_unknown_room(self) -> None:
        """Close raises LookupError for a non-existent room_id."""
        with pytest.raises(LookupError):
            _dispatch("close", {"room_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRoomsSearch:
    """Tests for the search action."""

    def test_search_finds_matching_notes(self) -> None:
        """Search returns notes containing the query string."""
        room = Room.objects.create(name="search-room")
        RoomNote.objects.create(room=room, content="deploy the widget", agent_role="pm")
        RoomNote.objects.create(room=room, content="unrelated note", agent_role="pm")
        result = _dispatch("search", {"query": "widget"})
        assert len(result["results"]) == 1
        assert "widget" in result["results"][0]["content"]

    def test_search_case_insensitive(self) -> None:
        """Search is case-insensitive."""
        room = Room.objects.create(name="ci-room")
        RoomNote.objects.create(room=room, content="Hello World", agent_role="pm")
        result = _dispatch("search", {"query": "hello"})
        assert len(result["results"]) >= 1

    def test_search_with_room_filter(self) -> None:
        """Search limited to a specific room only returns notes from that room."""
        room_a = Room.objects.create(name="sa-room")
        room_b = Room.objects.create(name="sb-room")
        RoomNote.objects.create(room=room_a, content="needle in A", agent_role="pm")
        RoomNote.objects.create(room=room_b, content="needle in B", agent_role="pm")
        result = _dispatch("search", {"query": "needle", "room_id": str(room_a.id)})
        assert all(n["room_id"] == str(room_a.id) for n in result["results"])

    def test_search_respects_limit(self) -> None:
        """Search limits results to the limit param."""
        room = Room.objects.create(name="lim-room")
        for i in range(5):
            RoomNote.objects.create(room=room, content=f"target {i}", agent_role="pm")
        result = _dispatch("search", {"query": "target", "limit": 3})
        assert len(result["results"]) == 3

    def test_search_returns_query_echo(self) -> None:
        """Search response echoes the query string."""
        room = Room.objects.create(name="echo-room")
        RoomNote.objects.create(room=room, content="anything", agent_role="pm")
        result = _dispatch("search", {"query": "any"})
        assert result["query"] == "any"
