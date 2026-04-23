"""Tool-call middleware for contrib.coordination."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.utils import timezone


class WorkerHeartbeatMiddleware:
    """
    Stamp last_heartbeat on the authenticated worker on every tool call.

    Plugs into ``FRIESE_MCP_TOOL_MIDDLEWARE``.  Resolves the worker from
    ``request.user.worker_id`` when present.  Silently no-ops when no matching
    worker is found or when ``friese_mcp.contrib.coordination`` is not installed.

    Example settings::

        FRIESE_MCP_TOOL_MIDDLEWARE = [
            "friese_mcp.contrib.coordination.middleware.WorkerHeartbeatMiddleware",
        ]

    """

    def __call__(
        self,
        request: Any,
        tool_name: str,
        arguments: dict[str, Any],
        call_next: Callable[..., Any],
    ) -> Any:
        """Stamp heartbeat then delegate to call_next."""
        self._stamp_heartbeat(request)
        return call_next(request, tool_name, arguments)

    def _stamp_heartbeat(self, request: Any) -> None:
        """Update last_heartbeat=now and status=active for the authenticated worker."""
        user = getattr(request, "user", None)
        worker_id = getattr(user, "worker_id", None) if user is not None else None
        if not worker_id:
            return

        try:
            from friese_mcp.contrib.coordination.models import (  # pylint: disable=import-outside-toplevel
                Worker,
            )

            Worker.objects.filter(id=worker_id).update(
                last_heartbeat=timezone.now(),
                status="active",
            )
        except ImportError:
            pass
