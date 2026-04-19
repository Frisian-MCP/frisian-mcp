"""Thread-safe registry of MCP tools with JSON Schema validation and permission enforcement."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

import jsonschema
import jsonschema.exceptions
from django.conf import settings
from django.http import HttpRequest
from rest_framework.permissions import BasePermission


def _camel_to_snake(name: str) -> str:
    """Convert a camelCase or PascalCase identifier to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _normalize_argument_keys(arguments: Any) -> Any:
    """
    Recursively convert all dict keys from camelCase to snake_case.

    Controlled by the ``FRIESE_MCP_NORMALIZE_INPUT_CASE`` Django setting
    (default ``True``).  Values are passed through unchanged so that string
    field content (e.g. exercise names) is never mutated.
    """
    if not isinstance(arguments, dict):
        return arguments
    return {_camel_to_snake(k): _normalize_argument_keys(v) for k, v in arguments.items()}


class ToolNotFoundError(LookupError):
    """Raised when a requested tool name is not in the registry."""


class ToolInputError(ValueError):
    """Raised when tool arguments fail JSON Schema validation."""


class _ToolEntry:
    __slots__ = ("description", "fn", "input_schema", "name", "permission_classes")

    def __init__(
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]],
    ) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.input_schema = input_schema
        self.permission_classes = permission_classes


class ToolRegistry:
    """
    Thread-safe registry for MCP tools.

    Tools are registered at startup via ``@mcp_tool`` or auto-discovery and
    dispatched at request time.  The module-level :data:`tool_registry`
    singleton is the primary entry point; instantiate ``ToolRegistry`` directly
    only when an isolated registry is required (e.g. in tests).
    """

    def __init__(self) -> None:
        """Initialise an empty, unlocked registry."""
        self._tools: dict[str, _ToolEntry] = {}
        self._lock: threading.Lock = threading.Lock()

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        description: str,
        input_schema: dict[str, Any],
        permission_classes: list[type[BasePermission]] | None = None,
    ) -> None:
        """
        Register a callable as a named MCP tool.

        Args:
            name: Unique tool name (e.g. ``"users.list"``).
            fn: Callable invoked as ``fn(arguments, request)``.
            description: Human-readable description for MCP tool listing.
            input_schema: JSON Schema (draft-07) describing expected arguments.
            permission_classes: DRF ``BasePermission`` subclasses that guard
                this tool.  Pass ``None`` or ``[]`` for unrestricted access;
                authentication and authorisation remain the host app's concern.

        """
        with self._lock:
            self._tools[name] = _ToolEntry(
                name=name,
                fn=fn,
                description=description,
                input_schema=input_schema,
                permission_classes=list(permission_classes or []),
            )

    def list_tools(self) -> list[dict[str, Any]]:
        """
        Return the tool listing in MCP ``tools/list`` response format.

        **Auth note:** This method returns all registered tools regardless of the caller's
        identity.  friese-mcp deliberately does not filter the tool manifest by permissions:
        the package does not own authentication or authorisation.  The host application is
        responsible for placing auth-gating in front of the MCP endpoint at the
        infrastructure level.  Object-level permission filtering (``has_object_permission``)
        is also not applied here — this is a known architectural gap documented for v2.
        """
        with self._lock:
            return [
                {
                    "name": entry.name,
                    "description": entry.description,
                    "inputSchema": entry.input_schema,
                }
                for entry in self._tools.values()
            ]

    def dispatch(
        self,
        request: HttpRequest,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """
        Validate, authorise, and invoke a registered tool.

        The method performs three steps in order:

        1. Look up the tool — raises :exc:`ToolNotFoundError` (a
           ``LookupError``) if absent.
        2. Validate *arguments* against the tool's JSON Schema — raises
           :exc:`ToolInputError` on failure.
        3. Evaluate each ``permission_class`` in declaration order — raises
           ``PermissionError`` on first denial.

        Args:
            request: The current Django HTTP request used for permission checks.
            name: Tool name to dispatch.
            arguments: Caller-supplied arguments validated against
                ``input_schema``.

        Returns:
            Whatever the tool callable returns.

        Raises:
            ToolNotFoundError: No tool with *name* is registered.
            ToolInputError: *arguments* fails JSON Schema validation.
            PermissionError: A permission class denies access.

        """
        with self._lock:
            entry = self._tools.get(name)

        if entry is None:
            raise ToolNotFoundError(f"No tool registered with name {name!r}")

        # IT-1: Normalize camelCase argument keys to snake_case so that MCP
        # clients (e.g. Claude) can send either convention and always reach the
        # underlying Django serializer fields.  Opt out by setting
        # FRIESE_MCP_NORMALIZE_INPUT_CASE = False in Django settings.
        if getattr(settings, "FRIESE_MCP_NORMALIZE_INPUT_CASE", True):
            arguments = _normalize_argument_keys(arguments)

        try:
            jsonschema.validate(instance=arguments, schema=entry.input_schema)
        except jsonschema.exceptions.ValidationError as exc:
            raise ToolInputError(exc.message) from exc

        for perm_class in entry.permission_classes:
            perm = perm_class()
            if not perm.has_permission(request, None):  # type: ignore[arg-type]
                raise PermissionError(f"Permission denied by {perm_class.__name__}")

        return entry.fn(arguments, request)


#: Module-level singleton imported by ``views.py`` and ``@mcp_tool``.
#: Import this directly rather than instantiating :class:`ToolRegistry`.
tool_registry: ToolRegistry = ToolRegistry()
