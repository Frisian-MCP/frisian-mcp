"""Tests for the escalate_to_human @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import (
    ApprovalRequest,
    CoordinationTenant,
    Room,
    RoomNote,
)
from friese_mcp.contrib.coordination.tools.escalate import (
    _ESCALATION_PREFIX,
    EscalateToHumanDispatcher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the EscalateToHumanDispatcher directly."""
    dispatcher = EscalateToHumanDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


def _make_escalation(**kwargs: Any) -> ApprovalRequest:
    """Create a pending escalation ApprovalRequest with defaults."""
    defaults = {
        "title": "Blocked",
        "description": "Need help",
        "requesting_agent": f"{_ESCALATION_PREFIX}worker-1",
    }
    defaults.update(kwargs)
    return ApprovalRequest.objects.create(**defaults)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEscalateCreate:
    """Tests for the create action."""

    def test_create_returns_escalation_id(self) -> None:
        """Create returns an escalation_id."""
        result = _dispatch(
            "create",
            {"title": "Blocked", "description": "Need decision", "requesting_agent": "planner"},
        )
        assert "escalation_id" in result
        assert ApprovalRequest.objects.filter(id=result["escalation_id"]).exists()

    def test_create_status_is_pending(self) -> None:
        """New escalation starts with status=pending."""
        result = _dispatch(
            "create",
            {"title": "T", "description": "D", "requesting_agent": "agent"},
        )
        assert result["status"] == "pending"

    def test_create_strips_prefix_in_response(self) -> None:
        """Response requesting_agent has the escalation prefix stripped."""
        result = _dispatch(
            "create",
            {"title": "T", "description": "D", "requesting_agent": "planner"},
        )
        assert result["requesting_agent"] == "planner"
        db_obj = ApprovalRequest.objects.get(id=result["escalation_id"])
        assert db_obj.requesting_agent == f"{_ESCALATION_PREFIX}planner"

    def test_create_with_room_posts_note(self) -> None:
        """Create with room_id posts a RoomNote to the specified room."""
        room = Room.objects.create(name="ops-room")
        result = _dispatch(
            "create",
            {
                "title": "Critical",
                "description": "Help needed",
                "requesting_agent": "agent",
                "room_id": str(room.id),
            },
        )
        assert RoomNote.objects.filter(room=room).exists()
        note = RoomNote.objects.get(room=room)
        assert "Critical" in note.content
        assert result["escalation_id"] is not None

    def test_create_with_invalid_room_is_nonfatal(self) -> None:
        """Create with a non-existent room_id does not raise an error."""
        result = _dispatch(
            "create",
            {
                "title": "T",
                "description": "D",
                "requesting_agent": "agent",
                "room_id": str(uuid.uuid4()),
            },
        )
        assert result["escalation_id"] is not None

    def test_create_scoped_to_tenant(self) -> None:
        """Create scopes the escalation to the request tenant."""
        tenant = CoordinationTenant.objects.create(name="T1")
        result = _dispatch(
            "create",
            {"title": "T", "description": "D", "requesting_agent": "r"},
            request=_request(tenant=tenant),
        )
        assert result["tenant_id"] == str(tenant.id)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEscalateGet:
    """Tests for the get action."""

    def test_get_returns_escalation(self) -> None:
        """Get returns the escalation dict for a valid ID."""
        a = _make_escalation()
        result = _dispatch("get", {"escalation_id": str(a.id)})
        assert result["escalation_id"] == str(a.id)

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent escalation_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"escalation_id": str(uuid.uuid4())})

    def test_get_does_not_return_plain_approvals(self) -> None:
        """Get raises LookupError for an approval that lacks the escalation prefix."""
        plain = ApprovalRequest.objects.create(
            title="Plain",
            description="No prefix",
            requesting_agent="agent",
        )
        with pytest.raises(LookupError):
            _dispatch("get", {"escalation_id": str(plain.id)})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEscalateList:
    """Tests for the list action."""

    def test_list_returns_escalations(self) -> None:
        """List returns escalation requests."""
        _make_escalation(title="E1")
        _make_escalation(title="E2")
        result = _dispatch("list", {})
        assert len(result["escalations"]) >= 2

    def test_list_excludes_plain_approvals(self) -> None:
        """List does not include non-escalation ApprovalRequests."""
        ApprovalRequest.objects.create(
            title="Plain",
            description="No prefix",
            requesting_agent="agent",
        )
        _make_escalation(title="Esc")
        result = _dispatch("list", {})
        for esc in result["escalations"]:
            assert not esc["requesting_agent"].startswith(_ESCALATION_PREFIX)

    def test_list_filters_by_status(self) -> None:
        """List filters by status when provided."""
        a = _make_escalation(title="Resolved")
        a.status = "approved"
        a.save()
        result = _dispatch("list", {"status": "approved"})
        ids = [x["escalation_id"] for x in result["escalations"]]
        assert str(a.id) in ids
        assert all(x["status"] == "approved" for x in result["escalations"])

    def test_list_tenant_isolation(self) -> None:
        """List is scoped to the request tenant."""
        t_a = CoordinationTenant.objects.create(name="A")
        t_b = CoordinationTenant.objects.create(name="B")
        _make_escalation(title="TA", tenant=t_a)
        _make_escalation(title="TB", tenant=t_b)
        result = _dispatch("list", {}, request=_request(tenant=t_a))
        assert all(x["tenant_id"] == str(t_a.id) for x in result["escalations"])


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEscalateResolve:
    """Tests for the resolve action."""

    def test_resolve_approve_sets_status(self) -> None:
        """Resolve with approved transitions status correctly."""
        a = _make_escalation()
        result = _dispatch(
            "resolve", {"escalation_id": str(a.id), "resolution": "approved"}
        )
        assert result["status"] == "approved"

    def test_resolve_reject_sets_status(self) -> None:
        """Resolve with rejected transitions status correctly."""
        a = _make_escalation()
        result = _dispatch(
            "resolve", {"escalation_id": str(a.id), "resolution": "rejected"}
        )
        assert result["status"] == "rejected"

    def test_resolve_stores_resolution_note(self) -> None:
        """Resolve stores the resolution_note."""
        a = _make_escalation()
        result = _dispatch(
            "resolve",
            {
                "escalation_id": str(a.id),
                "resolution": "approved",
                "resolution_note": "Proceed",
            },
        )
        assert result["resolution_note"] == "Proceed"

    def test_resolve_stores_resolved_by(self) -> None:
        """Resolve stores the resolved_by field."""
        a = _make_escalation()
        result = _dispatch(
            "resolve",
            {"escalation_id": str(a.id), "resolution": "approved", "resolved_by": "bob"},
        )
        assert result["resolved_by"] == "bob"

    def test_resolve_raises_if_not_pending(self) -> None:
        """Resolve raises ValueError when escalation is already resolved."""
        a = _make_escalation()
        a.status = "approved"
        a.save()
        with pytest.raises(ValueError):
            _dispatch("resolve", {"escalation_id": str(a.id), "resolution": "rejected"})
