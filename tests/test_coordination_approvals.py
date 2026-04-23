"""Tests for the approvals @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import ApprovalRequest, CoordinationTenant
from friese_mcp.contrib.coordination.tools.approvals import ApprovalsDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the ApprovalsDispatcher directly."""
    dispatcher = ApprovalsDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


def _make_approval(**kwargs: Any) -> ApprovalRequest:
    """Create a pending ApprovalRequest with defaults."""
    defaults = {
        "title": "Test",
        "description": "Needs approval",
        "requesting_agent": "agent-x",
    }
    defaults.update(kwargs)
    return ApprovalRequest.objects.create(**defaults)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalsCreate:
    """Tests for the create action."""

    def test_create_returns_approval_id(self) -> None:
        """Create returns an approval_id."""
        result = _dispatch(
            "create",
            {"title": "Deploy", "description": "Deploy to prod", "requesting_agent": "bot"},
        )
        assert "approval_id" in result
        assert ApprovalRequest.objects.filter(id=result["approval_id"]).exists()

    def test_create_status_is_pending(self) -> None:
        """New approval starts with status=pending."""
        result = _dispatch(
            "create",
            {"title": "Deploy", "description": "Details", "requesting_agent": "bot"},
        )
        assert result["status"] == "pending"

    def test_create_stores_fields(self) -> None:
        """Create stores title, description, requesting_agent correctly."""
        result = _dispatch(
            "create",
            {"title": "Gate", "description": "Gate details", "requesting_agent": "planner"},
        )
        assert result["title"] == "Gate"
        assert result["description"] == "Gate details"
        assert result["requesting_agent"] == "planner"

    def test_create_scoped_to_tenant(self) -> None:
        """Create scopes the approval to the request tenant."""
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
class TestApprovalsGet:
    """Tests for the get action."""

    def test_get_returns_approval(self) -> None:
        """Get returns the approval dict for a valid ID."""
        a = _make_approval()
        result = _dispatch("get", {"approval_id": str(a.id)})
        assert result["approval_id"] == str(a.id)

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent approval_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"approval_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalsList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all approvals."""
        _make_approval(title="A1")
        _make_approval(title="A2")
        result = _dispatch("list", {})
        assert len(result["approvals"]) >= 2

    def test_list_filters_by_status(self) -> None:
        """List filters by status when provided."""
        a = _make_approval(title="Pending")
        a.status = "approved"
        a.save()
        result = _dispatch("list", {"status": "approved"})
        ids = [x["approval_id"] for x in result["approvals"]]
        assert str(a.id) in ids
        assert all(x["status"] == "approved" for x in result["approvals"])

    def test_list_tenant_isolation(self) -> None:
        """List is scoped to the request tenant."""
        t_a = CoordinationTenant.objects.create(name="A")
        t_b = CoordinationTenant.objects.create(name="B")
        _make_approval(title="TA", tenant=t_a)
        _make_approval(title="TB", tenant=t_b)
        result = _dispatch("list", {}, request=_request(tenant=t_a))
        assert all(x["tenant_id"] == str(t_a.id) for x in result["approvals"])


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalsApprove:
    """Tests for the approve action."""

    def test_approve_sets_status(self) -> None:
        """Approve transitions status to approved."""
        a = _make_approval()
        result = _dispatch("approve", {"approval_id": str(a.id)})
        assert result["status"] == "approved"

    def test_approve_stores_resolution_note(self) -> None:
        """Approve stores the resolution_note."""
        a = _make_approval()
        result = _dispatch(
            "approve", {"approval_id": str(a.id), "resolution_note": "LGTM"}
        )
        assert result["resolution_note"] == "LGTM"

    def test_approve_stores_resolved_by(self) -> None:
        """Approve stores the resolved_by field."""
        a = _make_approval()
        result = _dispatch("approve", {"approval_id": str(a.id), "resolved_by": "alice"})
        assert result["resolved_by"] == "alice"

    def test_approve_raises_if_not_pending(self) -> None:
        """Approve raises ValueError when approval is already resolved."""
        a = _make_approval()
        a.status = "rejected"
        a.save()
        with pytest.raises(ValueError):
            _dispatch("approve", {"approval_id": str(a.id)})


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalsReject:
    """Tests for the reject action."""

    def test_reject_sets_status(self) -> None:
        """Reject transitions status to rejected."""
        a = _make_approval()
        result = _dispatch("reject", {"approval_id": str(a.id)})
        assert result["status"] == "rejected"

    def test_reject_stores_resolution_note(self) -> None:
        """Reject stores the resolution_note."""
        a = _make_approval()
        result = _dispatch(
            "reject", {"approval_id": str(a.id), "resolution_note": "Too risky"}
        )
        assert result["resolution_note"] == "Too risky"

    def test_reject_raises_if_not_pending(self) -> None:
        """Reject raises ValueError when approval is not pending."""
        a = _make_approval()
        a.status = "approved"
        a.save()
        with pytest.raises(ValueError):
            _dispatch("reject", {"approval_id": str(a.id)})


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApprovalsCancel:
    """Tests for the cancel action."""

    def test_cancel_sets_status(self) -> None:
        """Cancel transitions status to cancelled."""
        a = _make_approval()
        result = _dispatch("cancel", {"approval_id": str(a.id)})
        assert result["status"] == "cancelled"

    def test_cancel_stores_reason(self) -> None:
        """Cancel stores reason in resolution_note."""
        a = _make_approval()
        result = _dispatch("cancel", {"approval_id": str(a.id), "reason": "No longer needed"})
        assert result["resolution_note"] == "No longer needed"

    def test_cancel_raises_if_not_pending(self) -> None:
        """Cancel raises ValueError when approval is not pending."""
        a = _make_approval()
        a.status = "approved"
        a.save()
        with pytest.raises(ValueError):
            _dispatch("cancel", {"approval_id": str(a.id)})
