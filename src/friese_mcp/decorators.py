"""Decorators for explicit MCP tool registration and auto-discovery exclusion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from rest_framework.permissions import BasePermission

from friese_mcp.registry import tool_registry

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])
_AnyT = TypeVar("_AnyT")


def mcp_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    permission_classes: list[type[BasePermission]] | None = None,
) -> Callable[[_CallableT], _CallableT]:
    """
    Register the decorated callable as a named MCP tool.

    The decorated function must accept ``(arguments: dict[str, Any],
    request: HttpRequest)`` and return a JSON-serialisable value.

    Example::

        @mcp_tool(
            name="orders.cancel",
            description="Cancel an order by ID.",
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "integer"}},
                "required": ["order_id"],
            },
            permission_classes=[IsAuthenticated],
        )
        def cancel_order(
            arguments: dict[str, Any], request: HttpRequest
        ) -> dict[str, Any]:
            ...

    Args:
        name: Unique MCP tool name (e.g. ``"orders.cancel"``).
        description: Human-readable description shown in ``tools/list``.
        input_schema: JSON Schema (draft-07) for argument validation.
        permission_classes: DRF permission classes that guard this tool.
            Pass ``None`` or ``[]`` for unrestricted access.

    Returns:
        The original callable, unchanged, registered as a side-effect.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        tool_registry.register(
            name=name,
            fn=fn,
            description=description,
            input_schema=input_schema,
            permission_classes=permission_classes,
        )
        return fn

    return decorator


def mcp_ignore(obj: _AnyT) -> _AnyT:
    """
    Mark a ViewSet class or action method so auto-discovery skips it.

    Sets ``_mcp_ignore = True`` on the target; the auto-discovery pass
    inspects this attribute before registering tools.

    Can be applied to an entire ViewSet class or to individual action methods::

        @mcp_ignore
        class InternalViewSet(ModelViewSet):
            ...

        class UserViewSet(ModelViewSet):
            @mcp_ignore
            def private_action(self, request: HttpRequest) -> Response:
                ...

    Args:
        obj: A ViewSet class or action method to exclude from MCP discovery.

    Returns:
        The original object, unchanged except for the ``_mcp_ignore`` marker.

    """
    obj._mcp_ignore = True  # type: ignore[attr-defined]  # pylint: disable=protected-access
    return obj
