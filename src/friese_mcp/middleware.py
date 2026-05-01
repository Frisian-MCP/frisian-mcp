"""
Middleware utilities for friese-mcp.

This module hosts two distinct middleware concepts:

1. **Tool-call middleware chain** — a per-tool callable pipeline configured via
   :setting:`FRIESE_MCP_TOOL_MIDDLEWARE`.  See :func:`load_middleware` and
   :func:`build_middleware_chain`.

2. **Django HTTP request middleware** — :class:`McpTrailingSlashMiddleware`
   normalises trailing slashes on the MCP gateway path so that Django's
   :class:`~django.middleware.common.CommonMiddleware` does not 301-redirect
   ``/mcp`` to ``/mcp/`` (MCP clients such as Claude.ai and Cursor do not follow
   the redirect).  This middleware is auto-installed by
   :class:`~friese_mcp.apps.FrieseMcpConfig.ready`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse

_middleware_instances: list[Any] = []

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default URL prefix at which the MCP gateway is mounted.  Override via
#: ``settings.FRIESE_MCP_URL_PREFIX`` (e.g. ``"/api/mcp"``).
DEFAULT_MCP_URL_PREFIX: str = "/mcp"


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


# ---------------------------------------------------------------------------
# HTTP request middleware
# ---------------------------------------------------------------------------


def _get_mcp_url_prefix() -> str:
    """
    Return the configured MCP gateway URL prefix as an absolute path.

    Reads :setting:`FRIESE_MCP_URL_PREFIX` (default :data:`DEFAULT_MCP_URL_PREFIX`)
    and normalises it to a leading-slash, no-trailing-slash form so that the
    middleware can compare against ``request.path_info`` directly.

    Returns:
        The normalised prefix, e.g. ``"/mcp"`` or ``"/api/mcp"``.

    """
    prefix: str = getattr(settings, "FRIESE_MCP_URL_PREFIX", DEFAULT_MCP_URL_PREFIX)
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1 and prefix.endswith("/"):
        prefix = prefix.rstrip("/")
    return prefix


class McpTrailingSlashMiddleware:
    """
    Strip a single trailing slash from the MCP gateway path before routing.

    Django's :class:`~django.middleware.common.CommonMiddleware` issues a 301
    redirect from ``/mcp`` to ``/mcp/`` when :setting:`APPEND_SLASH` is ``True``
    (the default).  MCP clients such as Claude.ai and Cursor strip the trailing
    slash from the configured server URL and **do not follow** the resulting
    redirect, which causes the connection to fail silently.

    This middleware runs **before** ``CommonMiddleware`` and rewrites
    ``request.path`` and ``request.path_info`` from ``/mcp/`` to ``/mcp`` (and
    similarly for any configured prefix).  The friese-mcp URLconf accepts both
    forms via ``re_path(r"^/?$", ...)`` so the request is dispatched normally
    after rewriting; ``CommonMiddleware`` then sees an already-clean path and
    skips the redirect.

    For non-MCP paths the middleware is a no-op.

    Configuration:
        * ``FRIESE_MCP_URL_PREFIX`` — absolute URL prefix at which the gateway
          is mounted.  Defaults to :data:`DEFAULT_MCP_URL_PREFIX`.

    Auto-installation:
        :meth:`friese_mcp.apps.FrieseMcpConfig.ready` inserts this middleware
        into ``settings.MIDDLEWARE`` immediately before ``CommonMiddleware``,
        so host applications do not need to register it manually.

    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """
        Cache the downstream view callable.

        Args:
            get_response: The next callable in Django's middleware chain.

        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """
        Rewrite the request path when it targets the MCP gateway.

        Specifically, when ``request.path_info`` equals ``"<prefix>/"`` (for
        the configured prefix), strip the trailing slash from both
        ``request.path`` and ``request.path_info`` so that
        :class:`~django.middleware.common.CommonMiddleware` does not redirect.

        Args:
            request: The incoming HTTP request.

        Returns:
            The downstream response.

        """
        prefix = _get_mcp_url_prefix()
        path_info = request.path_info
        if path_info == prefix + "/":
            request.path_info = prefix
            # ``request.path`` includes any SCRIPT_NAME prefix; rebuild it
            # consistently by stripping a single trailing slash if present.
            if request.path.endswith("/") and len(request.path) > 1:
                request.path = request.path[:-1]
        return self.get_response(request)
