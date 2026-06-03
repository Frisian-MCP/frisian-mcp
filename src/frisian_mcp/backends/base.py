"""
Abstract base classes and core dataclasses for the frisian-mcp backend layer.

The backend layer decouples *discovery* (finding DRF ViewSets and mapping them
to MCP tools) from *invocation* (calling a ViewSet action in response to a
``tools/call`` request).  This split enables mature multi-app DRF projects
running Django 5.x under ASGI to provide custom backends without touching
frisian-mcp's gateway or registry logic.

Two settings control which backends are loaded at startup::

    # settings.py
    FRISIAN_MCP_DISCOVERY_BACKEND = "myapp.backends.CustomDiscovery"
    FRISIAN_MCP_INVOCATION_BACKEND = "myapp.backends.AsyncInvocation"

When unset, :class:`~frisian_mcp.backends.discovery.DRFSyncDiscovery` and
:class:`~frisian_mcp.backends.invocation.SyncInvocation` are used.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any, Literal

from django.http import HttpRequest
from rest_framework.permissions import BasePermission

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolDefinition:  # pylint: disable=too-many-instance-attributes
    """
    Immutable descriptor for a single MCP tool.

    Produced by a :class:`BaseDiscoveryBackend` and consumed by a
    :class:`BaseInvocationBackend`.  ``frozen=True`` ensures that the
    definition cannot be mutated after it is registered with the
    :class:`~frisian_mcp.registry.ToolRegistry`.

    Attributes:
        name: Unique MCP tool name (e.g. ``"users.list"``).
        description: Human-readable description shown in ``tools/list``.
        input_schema: JSON Schema (draft-07) describing expected arguments.
        permission_classes: DRF permission classes inherited from the ViewSet.
        source: How this tool was registered — ``"auto"`` (discovery) or
            ``"decorator"`` (``@mcp_tool``).
        view_class: The DRF ViewSet class (``None`` for decorator tools).
        action: The ViewSet action name, e.g. ``"list"`` (``None`` for
            decorator tools).
        url_path: The full URL path (joined prefix + pattern) that the
            discovery backend matched this tool against.  PKG-22 uses the
            presence of ``/api/`` in this string to disambiguate basename
            collisions between UI and API ViewSets that share the same
            model object name.  Empty string for decorator tools.

    """

    name: str
    description: str
    input_schema: dict[str, Any]
    permission_classes: tuple[type[BasePermission], ...]
    source: Literal["auto", "decorator"]
    view_class: type | None = None
    action: str | None = None
    is_dispatcher: bool = False
    permission_tier: str = "read"
    url_path: str = ""
    is_write: bool = False


@dataclasses.dataclass
class ToolResult:
    """
    Return value from a :class:`BaseInvocationBackend`.

    Attributes:
        content: JSON-serialisable result value.
        is_error: ``True`` when the tool execution itself failed (as opposed
            to a protocol-level error).  The gateway wraps this in a
            ``tools/call`` response with ``"isError": true``.
        http_status: The HTTP status code from the underlying DRF response
            (e.g. 201 for creates, 204 for deletes, 200 for reads/updates).
            Used by the write-path lean envelope to populate ``status_code``.
            Defaults to 200 for backends that do not capture HTTP status.

    """

    content: Any
    is_error: bool = False
    http_status: int = 200


# ---------------------------------------------------------------------------
# Abstract base classes
# ---------------------------------------------------------------------------


class BaseDiscoveryBackend(ABC):
    """
    Abstract base for MCP tool discovery.

    Subclass and override :meth:`discover_tools` to implement custom scanning
    logic.  :meth:`get_input_schema` has a default implementation that derives
    a JSON Schema from a DRF serializer; override it for custom ViewSet
    hierarchies that do not follow the standard ``get_serializer_class()``
    contract.
    """

    @abstractmethod
    def discover_tools(self) -> list[ToolDefinition]:
        """Return all MCP tools visible to this backend."""

    def get_input_schema(  # pylint: disable=unused-argument
        self, view_class: type, action: str
    ) -> dict[str, Any]:
        """
        Derive a JSON Schema for a ViewSet action from its serializer.

        Falls back to ``{"type": "object"}`` when a serializer cannot be
        introspected (read-only ViewSets, custom ``get_serializer_class``
        implementations that require an active request, etc.).

        Args:
            view_class: A DRF ViewSet class.
            action: The action name (e.g. ``"list"``, ``"create"``).

        Returns:
            A JSON Schema object describing the expected tool arguments.

        """
        return {"type": "object"}


class BaseInvocationBackend(ABC):
    """
    Abstract base for MCP tool invocation.

    Subclass and override :meth:`invoke` to implement custom dispatch logic
    (e.g. async invocation, tenant-scoped execution, Celery task delegation).
    """

    @abstractmethod
    def invoke(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        request: HttpRequest,
    ) -> ToolResult:
        """
        Invoke a tool and return its result.

        Permission enforcement has already been performed by
        :class:`~frisian_mcp.registry.ToolRegistry` before this method is
        called.

        Args:
            tool: The :class:`ToolDefinition` to invoke.
            arguments: Caller-supplied arguments (already validated against
                ``tool.input_schema``).
            request: The original MCP gateway HTTP request (carries the
                authenticated user and any host-app middleware state).

        Returns:
            A :class:`ToolResult` with the JSON-serialisable result.

        Raises:
            ValueError: If the tool cannot be invoked by this backend
                (e.g. missing ``view_class`` for a sync backend).

        """
