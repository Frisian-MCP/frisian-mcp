"""Tests for the artifacts @mcp_dispatcher."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from friese_mcp.contrib.coordination.models import Artifact, CoordinationTenant
from friese_mcp.contrib.coordination.tools.artifacts import ArtifactsDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(tenant: Any = None) -> MagicMock:
    """Return a mock request with optional tenant."""
    req = MagicMock()
    req.user.tenant = tenant
    return req


def _dispatch(action: str, params: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Invoke the ArtifactsDispatcher directly."""
    dispatcher = ArtifactsDispatcher()
    method = getattr(dispatcher, action)
    return method(request or _request(), params)


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifactsUpsert:
    """Tests for the upsert action."""

    def test_upsert_creates_artifact(self) -> None:
        """Upsert creates a new artifact when none exists."""
        result = _dispatch("upsert", {"name": "plan.md", "content": "# Plan"})
        assert Artifact.objects.filter(id=result["artifact_id"]).exists()

    def test_upsert_initial_version_is_1(self) -> None:
        """First upsert creates version 1."""
        result = _dispatch("upsert", {"name": "spec.md", "content": "v1"})
        assert result["version"] == 1

    def test_upsert_increments_version(self) -> None:
        """Second upsert on same name creates version 2."""
        _dispatch("upsert", {"name": "notes.md", "content": "v1"})
        result = _dispatch("upsert", {"name": "notes.md", "content": "v2"})
        assert result["version"] == 2

    def test_upsert_preserves_old_versions(self) -> None:
        """Upsert creates new rows; old versions remain in the database."""
        _dispatch("upsert", {"name": "history.md", "content": "v1"})
        _dispatch("upsert", {"name": "history.md", "content": "v2"})
        assert Artifact.objects.filter(name="history.md").count() == 2

    def test_upsert_with_artifact_type(self) -> None:
        """Artifact type is stored correctly."""
        result = _dispatch(
            "upsert", {"name": "blueprint.md", "content": "x", "artifact_type": "plan"}
        )
        assert result["artifact_type"] == "plan"

    def test_upsert_default_type_note(self) -> None:
        """Default artifact type is 'note'."""
        result = _dispatch("upsert", {"name": "scratch.md", "content": "x"})
        assert result["artifact_type"] == "note"

    def test_upsert_tenant_scoped_versions(self) -> None:
        """Upsert counts versions per tenant independently."""
        t_a = CoordinationTenant.objects.create(name="A")
        t_b = CoordinationTenant.objects.create(name="B")
        _dispatch("upsert", {"name": "shared.md", "content": "ta-v1"}, request=_request(tenant=t_a))
        result = _dispatch(
            "upsert", {"name": "shared.md", "content": "tb-v1"}, request=_request(tenant=t_b)
        )
        assert result["version"] == 1


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifactsGet:
    """Tests for the get action."""

    def test_get_returns_artifact(self) -> None:
        """Get returns the artifact dict for a valid artifact_id."""
        a = Artifact.objects.create(name="find-me.md", content="hi", artifact_type="note")
        result = _dispatch("get", {"artifact_id": str(a.id)})
        assert result["artifact_id"] == str(a.id)
        assert result["name"] == "find-me.md"

    def test_get_raises_for_unknown_id(self) -> None:
        """Get raises LookupError for a non-existent artifact_id."""
        with pytest.raises(LookupError):
            _dispatch("get", {"artifact_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# get_latest
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifactsGetLatest:
    """Tests for the get_latest action."""

    def test_get_latest_returns_highest_version(self) -> None:
        """get_latest returns the highest-version artifact."""
        Artifact.objects.create(name="rl.md", content="v1", artifact_type="note", version=1)
        Artifact.objects.create(name="rl.md", content="v2", artifact_type="note", version=2)
        result = _dispatch("get_latest", {"name": "rl.md"})
        assert result["version"] == 2
        assert result["content"] == "v2"

    def test_get_latest_raises_when_not_found(self) -> None:
        """get_latest raises LookupError when no artifact has that name."""
        with pytest.raises(LookupError):
            _dispatch("get_latest", {"name": "nonexistent.md"})


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifactsList:
    """Tests for the list action."""

    def test_list_returns_all(self) -> None:
        """List with no filters returns all artifacts."""
        Artifact.objects.create(name="a1.md", content="x", artifact_type="note")
        Artifact.objects.create(name="a2.md", content="y", artifact_type="plan")
        result = _dispatch("list", {})
        assert len(result["artifacts"]) >= 2

    def test_list_filters_by_type(self) -> None:
        """List with artifact_type filter returns only matching artifacts."""
        Artifact.objects.create(name="n.md", content="x", artifact_type="note")
        Artifact.objects.create(name="p.md", content="y", artifact_type="plan")
        result = _dispatch("list", {"artifact_type": "plan"})
        assert all(a["artifact_type"] == "plan" for a in result["artifacts"])

    def test_list_respects_limit(self) -> None:
        """List respects the limit param."""
        for i in range(5):
            Artifact.objects.create(name=f"lim-{i}.md", content="x", artifact_type="note")
        result = _dispatch("list", {"limit": 2})
        assert len(result["artifacts"]) <= 2


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestArtifactsSearch:
    """Tests for the search action."""

    def test_search_matches_name(self) -> None:
        """Search finds artifacts whose name contains the query."""
        Artifact.objects.create(name="roadmap.md", content="no match", artifact_type="note")
        result = _dispatch("search", {"query": "roadmap"})
        assert len(result["results"]) >= 1

    def test_search_matches_content(self) -> None:
        """Search finds artifacts whose content contains the query."""
        Artifact.objects.create(
            name="other.md", content="deploy to production", artifact_type="note"
        )
        result = _dispatch("search", {"query": "production"})
        assert len(result["results"]) >= 1

    def test_search_case_insensitive(self) -> None:
        """Search is case-insensitive."""
        Artifact.objects.create(name="UPPER.md", content="x", artifact_type="note")
        result = _dispatch("search", {"query": "upper"})
        assert len(result["results"]) >= 1

    def test_search_respects_limit(self) -> None:
        """Search limits results to the limit param."""
        for i in range(5):
            Artifact.objects.create(name=f"needle-{i}.md", content="x", artifact_type="note")
        result = _dispatch("search", {"query": "needle", "limit": 3})
        assert len(result["results"]) <= 3

    def test_search_returns_query_echo(self) -> None:
        """Search response echoes the query string."""
        Artifact.objects.create(name="echo.md", content="x", artifact_type="note")
        result = _dispatch("search", {"query": "echo"})
        assert result["query"] == "echo"
