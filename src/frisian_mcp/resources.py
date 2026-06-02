"""MCP Resource registry and ResourceDefinition dataclass."""

from __future__ import annotations

import dataclasses
import inspect
import re
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
    fn: Callable[..., str]
    description: str = ""
    mime_type: str = "text/plain"


def _match_uri_template(template: str, uri: str) -> dict[str, str] | None:
    """Return extracted variables if *uri* matches level-1 *template*, else ``None``."""
    segments = re.split(r"(\{[^}]+\})", template)
    pattern_parts: list[str] = []
    for segment in segments:
        if segment.startswith("{") and segment.endswith("}"):
            var_name = segment[1:-1]
            if not var_name.isidentifier():
                return None
            pattern_parts.append(f"(?P<{var_name}>[^/]+)")
        else:
            pattern_parts.append(re.escape(segment))
    try:
        match = re.fullmatch("".join(pattern_parts), uri)
    except re.error:
        return None
    return match.groupdict() if match is not None else None


def _handler_accepts_variables(fn: Callable[..., Any]) -> bool:
    """Return ``True`` if *fn* declares at least 3 positional parameters."""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    ]
    return len(positional) >= 3


@dataclasses.dataclass(frozen=True)
class _ProviderEntry:
    """A pair of list/read callables registered via register_provider()."""

    list_fn: Callable[[HttpRequest], list[dict[str, Any]]]
    read_fn: Callable[[str, HttpRequest], str | None] | None = None


class ResourceRegistry:
    """
    Thread-safe registry for MCP resources.

    Resources are registered at startup via ``@mcp_resource`` and dispatched at
    request time.  Dynamic, request-scoped resources (e.g. per-tenant DB lookups)
    are supported via :meth:`register_provider`.  The module-level
    :data:`resource_registry` singleton is the primary entry point.
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._resources: dict[str, ResourceDefinition] = {}
        self._providers: list[_ProviderEntry] = []
        self._lock: threading.Lock = threading.Lock()

    def register(self, definition: ResourceDefinition) -> None:
        """Register a :class:`ResourceDefinition`."""
        with self._lock:
            self._resources[definition.uri_template] = definition

    def register_provider(
        self,
        list_fn: Callable[[HttpRequest], list[dict[str, Any]]],
        read_fn: Callable[[str, HttpRequest], str | None] | None = None,
    ) -> None:
        """
        Register a dynamic resource provider invoked at request time.

        Unlike static ``@mcp_resource`` registrations, providers are called
        on every ``resources/list`` or ``resources/read`` request, enabling
        per-request DB queries for tenant-scoped resources.

        Args:
            list_fn: Callable ``(request) -> list[dict]`` contributing entries
                to the ``resources/list`` response.  Each dict should contain
                at least ``uri`` and ``name`` keys.
            read_fn: Optional callable ``(uri, request) -> str | None`` that
                attempts to read a resource by URI.  Return ``None`` to pass
                to the next provider.  Providers are tried in registration order.

        """
        with self._lock:
            self._providers.append(_ProviderEntry(list_fn=list_fn, read_fn=read_fn))

    def list_resources(self, request: HttpRequest | None = None) -> list[dict[str, Any]]:
        """
        Return the resource listing in MCP ``resources/list`` response format.

        Merges static registrations with entries from all registered providers.
        When *request* is ``None`` (e.g. startup introspection), providers are
        skipped and only static resources are returned.

        Args:
            request: The current HTTP request, forwarded to each list provider.

        """
        with self._lock:
            static = [
                {
                    "uri": rd.uri_template,
                    "name": rd.name,
                    "description": rd.description,
                    "mimeType": rd.mime_type,
                }
                for rd in self._resources.values()
            ]
            providers = list(self._providers)

        if request is None or not providers:
            return static

        dynamic: list[dict[str, Any]] = []
        for entry in providers:
            dynamic.extend(entry.list_fn(request))
        return static + dynamic

    def get_definition(self, uri: str) -> ResourceDefinition | None:
        """Return the :class:`ResourceDefinition` for *uri*, or ``None`` if not found."""
        with self._lock:
            return self._resources.get(uri)

    def read_resource(self, uri: str, request: HttpRequest) -> str:
        """
        Dispatch to the handler whose ``uri_template`` matches *uri*.

        Lookup order:

        1. Static registry (exact-match on ``uri_template``).
        2. Static registry (RFC-6570 level-1 template match) — extracted
           variables are passed as a third argument when the handler declares
           three positional parameters, otherwise called with two.
        3. Registered providers in registration order — the first ``read_fn``
           that returns a non-``None`` value wins.

        Args:
            uri: Concrete resource URI from the ``resources/read`` request.
            request: The current Django HTTP request.

        Returns:
            Text content returned by the handler or provider.

        Raises:
            :exc:`ResourceNotFoundError`: No handler or provider matches *uri*.

        """
        with self._lock:
            all_resources = dict(self._resources)
            providers = list(self._providers)

        # 1. Exact match.
        definition = all_resources.get(uri)
        if definition is not None:
            return definition.fn(uri, request)

        # 2. Template match.
        for tmpl, defn in all_resources.items():
            variables = _match_uri_template(tmpl, uri)
            if variables is not None:
                if _handler_accepts_variables(defn.fn):
                    return defn.fn(uri, request, variables)
                return defn.fn(uri, request)

        # 3. Provider fallback.
        for entry in providers:
            if entry.read_fn is not None:
                result = entry.read_fn(uri, request)
                if result is not None:
                    return result

        raise ResourceNotFoundError(f"Resource not found: {uri!r}")


#: Module-level singleton imported by ``views.py`` and ``@mcp_resource``.
resource_registry: ResourceRegistry = ResourceRegistry()
