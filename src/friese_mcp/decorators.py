"""Decorators for explicit MCP tool registration and auto-discovery exclusion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from rest_framework.permissions import BasePermission

from friese_mcp.registry import tool_registry
from friese_mcp.resources import ResourceDefinition, resource_registry

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])
_AnyT = TypeVar("_AnyT")
_ClassT = TypeVar("_ClassT", bound=type)


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


def mcp_action(
    name: str,
    description: str,
    params: dict[str, str] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> Callable[[_CallableT], _CallableT]:
    """
    Mark a method as a dispatchable action on an ``@mcp_dispatcher`` class.

    The decorator stores metadata on the function without altering its behaviour.
    ``@mcp_dispatcher`` scans for this marker when the class is decorated.

    Args:
        name: Action name used in ``action`` parameter dispatch (e.g. ``"create"``).
        description: Human-readable description shown in help-mode responses.
        params: Optional mapping of param names to human-readable hints.
        input_schema: Optional JSON Schema (draft-07) for per-call validation.

    Returns:
        The original method, unchanged, with ``_mcp_action_meta`` attached.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        fn._mcp_action_meta = {  # type: ignore[attr-defined]  # pylint: disable=protected-access
            "name": name,
            "description": description,
            "params": params or {},
            "input_schema": input_schema,
        }
        return fn

    return decorator


def mcp_dispatcher(
    name: str,
    description: str,
) -> Callable[[_ClassT], _ClassT]:
    """
    Register a class as a named MCP dispatcher tool.

    Each method decorated with ``@mcp_action`` becomes a dispatchable action.
    The class is instantiated once at decoration time; ``request`` is passed
    per-call.  Callers may omit ``action`` or pass ``action="help"`` to receive
    a structured listing of all actions.

    Args:
        name: Unique MCP tool name (e.g. ``"tasks"``).
        description: Human-readable description shown in ``tools/list``.

    Returns:
        The original class, unchanged, registered as a side-effect.

    """

    def decorator(cls: _ClassT) -> _ClassT:
        # pylint: disable=import-outside-toplevel
        from friese_mcp.backends.dispatcher import (
            ActionEntry,
            DispatcherMeta,
            _build_dispatcher_input_schema,
            _make_dispatcher_invoke,
        )

        action_map: dict[str, ActionEntry] = {}
        for attr in vars(cls).values():
            if callable(attr):
                action_meta = getattr(attr, "_mcp_action_meta", None)
                if action_meta is not None:
                    entry = ActionEntry(
                        name=action_meta["name"],
                        description=action_meta["description"],
                        params=action_meta["params"],
                        input_schema=action_meta["input_schema"],
                        method=attr,
                    )
                    action_map[entry.name] = entry

        meta = DispatcherMeta(name=name, description=description, actions=action_map)
        invoke_fn = _make_dispatcher_invoke(cls, meta)
        input_schema = _build_dispatcher_input_schema(meta)
        tool_registry.register(
            name=name,
            fn=invoke_fn,
            description=description,
            input_schema=input_schema,
            is_dispatcher=True,
        )
        return cls

    return decorator


def mcp_resource(
    uri_template: str,
    name: str,
    description: str = "",
    mime_type: str = "text/plain",
) -> Callable[[_CallableT], _CallableT]:
    """
    Register the decorated callable as an MCP resource.

    The decorated function must accept ``(uri: str, request: HttpRequest)``
    and return the resource contents as a string.

    Args:
        uri_template: Resource URI (may use ``{variable}`` placeholders).
        name: Human-readable resource name shown in ``resources/list``.
        description: Optional description shown in ``resources/list``.
        mime_type: MIME type of the returned content.  Defaults to ``"text/plain"``.

    Returns:
        The original callable, unchanged, registered as a side-effect.

    """

    def decorator(fn: _CallableT) -> _CallableT:
        resource_registry.register(
            ResourceDefinition(
                uri_template=uri_template,
                name=name,
                fn=fn,  # type: ignore[arg-type]
                description=description,
                mime_type=mime_type,
            )
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
