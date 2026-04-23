"""Artifacts @mcp_dispatcher — versioned document storage for coordination."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest

from friese_mcp.contrib.coordination.models import Artifact
from friese_mcp.contrib.coordination.utils import get_tenant, scope_qs
from friese_mcp.decorators import mcp_action, mcp_dispatcher


def _artifact_dict(a: Artifact) -> dict[str, Any]:
    """Serialize an Artifact to a plain dict."""
    return {
        "artifact_id": str(a.id),
        "name": a.name,
        "artifact_type": a.artifact_type,
        "content": a.content,
        "version": a.version,
        "created_by": a.created_by,
        "project_id": str(a.project_id) if a.project_id else None,
        "tenant_id": str(a.tenant_id) if a.tenant_id else None,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _get_artifact_or_raise(artifact_id: str, request: HttpRequest) -> Artifact:
    """Return the Artifact scoped by tenant, or raise LookupError."""
    qs = scope_qs(Artifact.objects.all(), request)
    try:
        return qs.get(id=artifact_id)
    except Artifact.DoesNotExist as exc:
        raise LookupError(f"Artifact {artifact_id!r} not found.") from exc


@mcp_dispatcher("artifacts", "Store and retrieve versioned artifacts (plans, specs, notes).")
class ArtifactsDispatcher:
    """Dispatcher for versioned artifact management."""

    @mcp_action(
        "upsert",
        "Create a new artifact or increment its version if it already exists.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Artifact name (unique within project)."},
                "content": {"type": "string", "description": "Artifact text content."},
                "artifact_type": {
                    "type": "string",
                    "description": "Type: note, plan, spec, or custom (default: note).",
                },
                "project_id": {"type": "string", "description": "Optional project UUID."},
                "created_by": {"type": "string", "description": "Creating agent role."},
            },
            "required": ["name", "content"],
        },
    )
    def upsert(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new artifact version (immutable history, version increments)."""
        name: str = params["name"]
        project_id = params.get("project_id")
        tenant = get_tenant(request)
        base_qs = scope_qs(
            Artifact.objects.filter(
                name=name,
                project_id=uuid.UUID(project_id) if project_id else None,
            ),
            request,
        )
        existing = base_qs.order_by("-version").first()
        new_version: int = (existing.version + 1) if existing else 1
        artifact = Artifact.objects.create(
            name=name,
            content=params["content"],
            version=new_version,
            artifact_type=params.get("artifact_type", "note"),
            project_id=uuid.UUID(project_id) if project_id else None,
            created_by=params.get("created_by", ""),
            tenant=tenant,
        )
        return _artifact_dict(artifact)

    @mcp_action(
        "get",
        "Get a specific artifact version by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "Artifact UUID."},
            },
            "required": ["artifact_id"],
        },
    )
    def get(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return a single artifact by ID."""
        return _artifact_dict(_get_artifact_or_raise(params["artifact_id"], request))

    @mcp_action(
        "get_latest",
        "Get the latest version of an artifact by name.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Artifact name."},
                "project_id": {
                    "type": "string",
                    "description": "Limit lookup to a specific project.",
                },
            },
            "required": ["name"],
        },
    )
    def get_latest(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return the highest-version artifact matching name (and project)."""
        project_id = params.get("project_id")
        qs = scope_qs(Artifact.objects.filter(name=params["name"]), request)
        if project_id:
            qs = qs.filter(project_id=project_id)
        artifact = qs.order_by("-version").first()
        if artifact is None:
            raise LookupError(f"No artifact named {params['name']!r} found.")
        return _artifact_dict(artifact)

    @mcp_action(
        "list",
        "List artifacts with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project UUID."},
                "artifact_type": {
                    "type": "string",
                    "description": "Filter by type (note/plan/spec/custom).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Max artifacts to return (default 50).",
                },
            },
        },
    )
    def list(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Return artifacts scoped by tenant, optionally filtered."""
        qs = scope_qs(Artifact.objects.all(), request)
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        if artifact_type := params.get("artifact_type"):
            qs = qs.filter(artifact_type=artifact_type)
        limit: int = params.get("limit", 50)
        return {"artifacts": [_artifact_dict(a) for a in qs[:limit]]}

    @mcp_action(
        "search",
        "Search artifacts by name or content.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Text to search for (case-insensitive, matches name or content)."
                    ),
                },
                "project_id": {"type": "string", "description": "Limit to a specific project."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max results to return (default 20).",
                },
            },
            "required": ["query"],
        },
    )
    def search(self, request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
        """Case-insensitive search on artifact name and content."""
        query: str = params["query"]
        qs = scope_qs(
            Artifact.objects.filter(name__icontains=query)
            | Artifact.objects.filter(content__icontains=query),
            request,
        ).order_by("name", "-version").distinct()
        if project_id := params.get("project_id"):
            qs = qs.filter(project_id=project_id)
        limit: int = params.get("limit", 20)
        return {"results": [_artifact_dict(a) for a in qs[:limit]], "query": query}
