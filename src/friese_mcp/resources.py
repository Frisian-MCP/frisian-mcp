"""MCP Resource registry and ResourceDefinition dataclass."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest


class ResourceNotFoundError(LookupError):
    """Raised when a requested resource URI is not in the registry."""


@dataclasses.dataclass(frozen=True)
class ResourceDefinition:
    """Immutable descriptor for a single MCP resource."""

    uri_template: str
    name: str
    fn: Callable[[str, HttpRequest], str]
    description: str = ""
    mime_type: str = "text/plain"


class ResourceRegistry:
    """
    Thread-safe registry for MCP resources.

    Resources are registered at startup via ``@mcp_resource`` and dispatched at
    request time.  The module-level :data:`resource_registry` singleton is the
    primary entry point.
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._resources: dict[str, ResourceDefinition] = {}
        self._lock: threading.Lock = threading.Lock()

    def register(self, definition: ResourceDefinition) -> None:
        """Register a :class:`ResourceDefinition`."""
        with self._lock:
            self._resources[definition.uri_template] = definition

    def list_resources(self) -> list[dict[str, Any]]:
        """Return the resource listing in MCP ``resources/list`` response format."""
        with self._lock:
            return [
                {
                    "uri": rd.uri_template,
                    "name": rd.name,
                    "description": rd.description,
                    "mimeType": rd.mime_type,
                }
                for rd in self._resources.values()
            ]

    def get_definition(self, uri: str) -> ResourceDefinition | None:
        """Return the :class:`ResourceDefinition` for *uri*, or ``None`` if not found."""
        with self._lock:
            return self._resources.get(uri)

    def read_resource(self, uri: str, request: HttpRequest) -> str:
        """
        Dispatch to the handler whose ``uri_template`` matches *uri*.

        Simple exact-match lookup: the caller passes a concrete URI and this
        method finds the first registered template that equals it.  Template
        variable expansion (e.g. ``rag://{source_id}/{doc_id}``) is the
        responsibility of the handler function itself.

        Args:
            uri: Concrete resource URI from the ``resources/read`` request.
            request: The current Django HTTP request.

        Returns:
            Text content returned by the handler.

        Raises:
            :exc:`ResourceNotFoundError`: No handler matches *uri*.

        """
        with self._lock:
            definition = self._resources.get(uri)

        if definition is None:
            raise ResourceNotFoundError(f"Resource not found: {uri!r}")

        return definition.fn(uri, request)


#: Module-level singleton imported by ``views.py`` and ``@mcp_resource``.
resource_registry: ResourceRegistry = ResourceRegistry()
