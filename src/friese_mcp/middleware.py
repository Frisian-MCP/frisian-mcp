"""Django-style middleware chain for MCP tool calls."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

_middleware_instances: list[Any] = []


def load_middleware() -> list[Any]:
    """
    Load and instantiate middleware classes from ``FRIESE_MCP_TOOL_MIDDLEWARE``.

    Reads the setting (default ``[]``), imports each dotted class path, and
    instantiates each class.  The resulting instances are stored in
    :data:`_middleware_instances` and any previously cached chain is cleared.

    Returns:
        The list of instantiated middleware objects.

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: On an invalid
            dotted path or an import failure.

    """
    global _middleware_instances  # pylint: disable=global-statement

    paths: list[str] = getattr(settings, "FRIESE_MCP_TOOL_MIDDLEWARE", [])
    instances: list[Any] = []
    for path in paths:
        module_path, _, class_name = path.rpartition(".")
        if not module_path:
            raise ImproperlyConfigured(
                f"FRIESE_MCP_TOOL_MIDDLEWARE: {path!r} is not a valid dotted Python path"
            )
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"FRIESE_MCP_TOOL_MIDDLEWARE: could not import {path!r}: {exc}"
            ) from exc
        cls = getattr(module, class_name, None)
        if cls is None:
            raise ImproperlyConfigured(
                f"FRIESE_MCP_TOOL_MIDDLEWARE: {module_path!r} has no attribute {class_name!r}"
            )
        instances.append(cls())

    _middleware_instances = instances
    return instances


def build_middleware_chain(
    tool_fn: Callable[[HttpRequest, str, dict[str, Any]], Any],
    middleware_instances: list[Any],
) -> Callable[[HttpRequest, str, dict[str, Any]], Any]:
    """
    Wrap *tool_fn* in the middleware chain and return the outermost callable.

    The first item in *middleware_instances* is outermost (called first).
    Each middleware receives ``(request, tool_name, arguments, call_next)``
    and must return the result.

    Args:
        tool_fn: The innermost callable — ``(request, tool_name, arguments) -> Any``.
        middleware_instances: Instantiated middleware objects in declaration order.

    Returns:
        A callable with signature ``(request, tool_name, arguments) -> Any``.

    """

    def base(request: HttpRequest, tool_name: str, arguments: dict[str, Any]) -> Any:
        return tool_fn(request, tool_name, arguments)

    chain: Callable[[HttpRequest, str, dict[str, Any]], Any] = base
    for mw in reversed(middleware_instances):

        def _wrap(
            current: Callable[[HttpRequest, str, dict[str, Any]], Any],
            middleware: Any,
        ) -> Callable[[HttpRequest, str, dict[str, Any]], Any]:
            def wrapped(
                request: HttpRequest, tool_name: str, arguments: dict[str, Any]
            ) -> Any:
                return middleware(request, tool_name, arguments, current)

            return wrapped

        chain = _wrap(chain, mw)
    return chain


def get_middleware_instances() -> list[Any]:
    """Return the currently loaded middleware instances."""
    return _middleware_instances
