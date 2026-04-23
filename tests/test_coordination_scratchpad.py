"""Tests for the scratchpad @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import CoordinationTenant, Project, Scratchpad
from friese_mcp.contrib.coordination.tools.scratchpad import ScratchpadDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the ScratchpadDispatcher directly."""
    dispatcher = ScratchpadDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScratchpadCreate:
    """Tests for the create action."""

    def test_create_returns_scratchpad_id(self) -> None:
        """Create returns a scratchpad_id."""
        result = _dispatch("create", {"title": "My Note"})
        assert "scratchpad_id" in result
        assert Scratchpad.objects.filter(id=result["scratchpad_id"]).exists()

    def test_create_stores_title(self) -> None:
        """Create stores the given title."""
        result = _dispatch("create", {"title": "Important Note"})
        assert result["title"] == "Important Note"

    def test_create_default_content_empty(self) -> None:
        """Create defaults content to empty string."""
        result = _dispatch("create", {"title": "Empty"})
        assert result["content"] == ""

    def test_create_stores_content(self) -> None:
        """Create stores given content."""
        result = _dispatch("create", {"title": "Note", "content": "Hello world"})
        assert result["content"] == "Hello world"

    def test_create_stores_agent_role(self) -> None:
        """Create stores the agent_role field."""
        result = _dispatch("create", {"title": "Note", "agent_role": "planner"})
        assert result["agent_role"] == "planner"

    def test_create_stores_session_id(self) -> None:
        """Create stores the session_id UUID."""
        session_id = str(uuid.uuid4())
        result = _dispatch("create", {"title": "Note", "session_id": session_id})
        assert result["session_id"] == session_id

    def test_create_stores_project_id(self) -> None:
        """Create stores the project_id UUID."""
        project = Project.objects.create(name="MyProject")
        result = _dispatch("create", {"title": "Note", "project_id": str(project.id)})
        assert result["project_id"] == str(project.id)

    def test_create_scoped_to_tenant(self) -> None:
        """Create scopes the scratchpad to the request tenant."""
        tenant = CoordinationTenant.objects.create(name="T1")
        result = _dispatch("create", {"title": "Note"}, request=_request(tenant=tenant))
        assert result["tenant_id"] == str(tenant.id)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScratchpadGet:
    """Tests for the get action."""

    def test_get_returns_scratchpad(self) -> None:
        """Get returns the scratchpad dict for a valid ID."""
        s = Scratchpad.objects.create(title="Find Me", content="data", agent_role="bot")
        result = _dispatch("get", {"scratchpad_id": str(s.id)})
        assert result["scratchpad_id"] == str(s.id)
        assert result["title"] == "Find Me"

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent scratchpad_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"scratchpad_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScratchpadUpdate:
    """Tests for the update action."""

    def test_update_overwrites_content(self) -> None:
        """Update with mode=overwrite replaces existing content."""
        s = Scratchpad.objects.create(title="Note", content="old")
        result = _dispatch("update", {"scratchpad_id": str(s.id), "content": "new"})
        assert result["content"] == "new"

    def test_update_default_mode_is_overwrite(self) -> None:
        """Update without mode param defaults to overwrite."""
        s = Scratchpad.objects.create(title="Note", content="original")
        result = _dispatch("update", {"scratchpad_id": str(s.id), "content": "replaced"})
        assert result["content"] == "replaced"

    def test_update_appends_content(self) -> None:
        """Update with mode=append concatenates content."""
        s = Scratchpad.objects.create(title="Note", content="Hello")
        result = _dispatch(
            "update", {"scratchpad_id": str(s.id), "content": " World", "mode": "append"}
        )
        assert result["content"] == "Hello World"

    def test_update_persists_to_db(self) -> None:
        """Update persists content change to the database."""
        s = Scratchpad.objects.create(title="Note", content="old")
        _dispatch("update", {"scratchpad_id": str(s.id), "content": "saved"})
        s.refresh_from_db()
        assert s.content == "saved"

    def test_update_raises_for_unknown_id(self) -> None:
        """Update raises LookupError for a non-existent scratchpad_id."""
        with pytest.raises(LookupError):
            _dispatch("update", {"scratchpad_id": str(uuid.uuid4()), "content": "x"})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScratchpadList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all scratchpads."""
        Scratchpad.objects.create(title="A", content="x", agent_role="r1")
        Scratchpad.objects.create(title="B", content="y", agent_role="r2")
        result = _dispatch("list", {})
        assert len(result["scratchpads"]) >= 2

    def test_list_filters_by_agent_role(self) -> None:
        """List filters by agent_role when provided."""
        Scratchpad.objects.create(title="A", content="x", agent_role="planner")
        Scratchpad.objects.create(title="B", content="y", agent_role="coder")
        result = _dispatch("list", {"agent_role": "planner"})
        assert all(s["agent_role"] == "planner" for s in result["scratchpads"])

    def test_list_filters_by_session_id(self) -> None:
        """List filters by session_id when provided."""
        sid = uuid.uuid4()
        other_sid = uuid.uuid4()
        Scratchpad.objects.create(title="A", content="x", session_id=sid)
        Scratchpad.objects.create(title="B", content="y", session_id=other_sid)
        result = _dispatch("list", {"session_id": str(sid)})
        assert all(s["session_id"] == str(sid) for s in result["scratchpads"])

    def test_list_filters_by_project_id(self) -> None:
        """List filters by project_id when provided."""
        proj_a = Project.objects.create(name="ProjA")
        proj_b = Project.objects.create(name="ProjB")
        Scratchpad.objects.create(title="A", content="x", project=proj_a)
        Scratchpad.objects.create(title="B", content="y", project=proj_b)
        result = _dispatch("list", {"project_id": str(proj_a.id)})
        assert all(s["project_id"] == str(proj_a.id) for s in result["scratchpads"])

    def test_list_tenant_isolation(self) -> None:
        """List is scoped to the request tenant."""
        t_a = CoordinationTenant.objects.create(name="A")
        t_b = CoordinationTenant.objects.create(name="B")
        Scratchpad.objects.create(title="TA", content="x", tenant=t_a)
        Scratchpad.objects.create(title="TB", content="y", tenant=t_b)
        result = _dispatch("list", {}, request=_request(tenant=t_a))
        tenant_ids = {s["tenant_id"] for s in result["scratchpads"]}
        assert tenant_ids == {str(t_a.id)}
