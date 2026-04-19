"""
SyncInvocation — default synchronous MCP tool invocation backend.

Constructs a synthetic DRF-compatible request, instantiates the target
ViewSet, and calls the action method directly in the current thread.  This
works for any standard DRF ViewSet running under a synchronous WSGI server.

Projects that need async invocation (e.g. ASGI + Django Channels) should
subclass :class:`~friese_mcp.backends.base.BaseInvocationBackend` and point
``settings.FRIESE_MCP_INVOCATION_BACKEND`` at their custom class.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest

# RequestFactory is part of Django's stable public API and is safe to use in
# production code.  DRF's APIRequestFactory is a test-only subclass that adds
# a `format=` kwarg convenience; we don't use that feature, so the plain
# Django factory is the correct dependency here.
from django.test import RequestFactory as _RequestFactory
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.request import Request
from rest_framework.settings import api_settings

from friese_mcp.backends.base import BaseInvocationBackend, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


def _format_drf_validation_error(exc: DRFValidationError) -> str:
    """
    Flatten a DRF ``ValidationError.detail`` tree into a readable string.

    DRF ``detail`` can be a list of ``ErrorDetail``, a dict of field → errors,
    or a nested combination.  This function produces a concise human-readable
    summary suitable for returning to an MCP caller.
    """
    detail = exc.detail

    def _flatten(obj: Any) -> list[str]:
        if isinstance(obj, list):
            return [str(item) for item in obj]
        if isinstance(obj, dict):
            parts = []
            for field, errors in obj.items():
                errs = _flatten(errors)
                parts.append(f"{field}: {', '.join(errs)}")
            return parts
        return [str(obj)]

    return "; ".join(_flatten(detail))


# ViewSet actions that need a primary-key URL kwarg.
_DETAIL_ACTIONS: frozenset[str] = frozenset({"retrieve", "update", "partial_update", "destroy"})

# Map standard ViewSet action name → HTTP method for the synthetic request.
_ACTION_TO_HTTP: dict[str, str] = {
    "list": "get",
    "retrieve": "get",
    "create": "post",
    "update": "put",
    "partial_update": "patch",
    "destroy": "delete",
}

# HTTP methods that carry arguments as query parameters rather than a body.
_GET_METHODS: frozenset[str] = frozenset({"get", "head", "options"})


def _action_http_method(view_class: type, action_name: str) -> str:
    """
    Return the HTTP method string for a ViewSet action.

    Standard CRUD actions are resolved from :data:`_ACTION_TO_HTTP`.  For
    custom ``@action``-decorated methods, the DRF decorator stores the
    allowed HTTP methods in the function's ``mapping`` attribute; the first
    recognised method is returned.  Falls back to ``"post"`` when the action
    is unknown.

    Args:
        view_class: The DRF ViewSet class.
        action_name: The action name (e.g. ``"list"``, ``"summary"``).

    Returns:
        Lowercase HTTP method string (e.g. ``"get"``, ``"post"``).

    """
    if action_name in _ACTION_TO_HTTP:
        return _ACTION_TO_HTTP[action_name]
    action_func = getattr(view_class, action_name, None)
    if action_func is not None:
        mapping: dict[str, str] = getattr(action_func, "mapping", {})
        for method in ("get", "post", "put", "patch", "delete"):
            if method in mapping:
                return method
    return "post"


class SyncInvocation(BaseInvocationBackend):
    """
    Default synchronous invocation backend.

    Builds a synthetic :class:`~rest_framework.request.Request` from the
    tool arguments, instantiates the ViewSet with proper DRF initialisation,
    and returns the response data wrapped in a
    :class:`~friese_mcp.backends.base.ToolResult`.

    Permission enforcement is handled upstream by
    :class:`~friese_mcp.registry.ToolRegistry` before this method is called,
    so the synthetic request bypasses DRF's authentication/permission pipeline
    by forwarding the already-authenticated user from the original MCP request.
    """

    _factory: _RequestFactory = _RequestFactory()

    def invoke(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        request: HttpRequest,
    ) -> ToolResult:
        """
        Invoke *tool* synchronously and return its result.

        Args:
            tool: The :class:`~friese_mcp.backends.base.ToolDefinition` to
                invoke.  Must have non-``None`` ``view_class`` and ``action``.
            arguments: Validated tool arguments.
            request: The original MCP gateway HTTP request; its ``user``
                attribute is forwarded to the synthetic inner request.

        Returns:
            A :class:`~friese_mcp.backends.base.ToolResult` containing the
            ViewSet response data.

        Raises:
            ValueError: If ``tool.view_class`` or ``tool.action`` is ``None``.

        """
        if tool.view_class is None or tool.action is None:
            raise ValueError(
                f"SyncInvocation requires view_class and action on ToolDefinition "
                f"{tool.name!r}; use a custom InvocationBackend for decorator tools."
            )

        http_method = _action_http_method(tool.view_class, tool.action)
        view_kwargs, body_args, query_args = self._split_arguments(
            tool.action, http_method, arguments
        )
        inner_req = self._build_request(http_method, body_args, query_args, request)

        # Pass DRF's default parsers so the synthetic Request can deserialise
        # application/json bodies from write actions.
        parsers = [cls() for cls in api_settings.DEFAULT_PARSER_CLASSES]  # type: ignore[operator]
        drf_request = Request(inner_req, parsers=parsers)
        viewset = tool.view_class(
            request=drf_request,
            kwargs=view_kwargs,
            action=tool.action,
            format_kwarg=None,
        )
        viewset.request = drf_request
        viewset.kwargs = view_kwargs
        viewset.action = tool.action
        viewset.format_kwarg = None

        try:
            response = getattr(viewset, tool.action)(drf_request, **view_kwargs)
        except (DRFValidationError, DjangoValidationError):
            # Let validation errors bubble to the protocol layer (views.py) where
            # they are caught, formatted, and returned as structured isError=True
            # content.  Catching them here and converting to ToolResult.is_error
            # would route them through apps.py's RuntimeError wrapper, causing
            # the actionable field messages to be silently replaced with
            # "Internal tool error".
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "SyncInvocation error",
                extra={"tool": tool.name, "error": str(exc)},
            )
            return ToolResult(content={"error": str(exc)}, is_error=True)

        return ToolResult(content=self._extract_data(response))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _split_arguments(
        self,
        action: str,
        http_method: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """
        Partition *arguments* into view kwargs, body data, and query params.

        For detail actions, ``"id"`` or ``"pk"`` is extracted into
        *view_kwargs* as ``"pk"``.  GET-method actions (including custom
        ``@action(methods=["get"])`` handlers) put the remaining arguments
        into *query_args*; all other methods put them into *body_args*.

        Args:
            action: ViewSet action name, used to detect detail routes.
            http_method: Lowercase HTTP method string (e.g. ``"get"``).
            arguments: Caller-supplied tool arguments.

        Returns:
            A ``(view_kwargs, body_args, query_args)`` tuple.

        """
        remaining = dict(arguments)
        view_kwargs: dict[str, Any] = {}

        if action in _DETAIL_ACTIONS:
            for key in ("pk", "id"):
                if key in remaining:
                    view_kwargs["pk"] = remaining.pop(key)
                    break

        if http_method in _GET_METHODS:
            return view_kwargs, {}, remaining
        return view_kwargs, remaining, {}

    def _build_request(
        self,
        http_method: str,
        body_args: dict[str, Any],
        query_args: dict[str, Any],
        original: HttpRequest,
    ) -> HttpRequest:
        """
        Build a synthetic :class:`~django.http.HttpRequest` for the ViewSet.

        The original request's ``user`` is forwarded so that any host-app
        middleware state (JWT payload, tenant scope, etc.) remains accessible.

        Args:
            http_method: Lowercase HTTP method (e.g. ``"get"``, ``"post"``).
            body_args: Arguments to encode as the request body (write methods).
            query_args: Arguments to pass as query string (GET methods).
            original: The original MCP gateway HTTP request.

        Returns:
            A new :class:`~django.http.HttpRequest` suitable for wrapping in a
            DRF :class:`~rest_framework.request.Request`.

        """
        make = getattr(self._factory, http_method)

        if http_method in _GET_METHODS:
            inner: HttpRequest = make("/", data=query_args or None)
        elif http_method == "delete":
            inner = make("/")
        else:
            inner = make(
                "/",
                data=json.dumps(body_args),
                content_type="application/json",
            )

        # Forward the authenticated user from the original MCP gateway request.
        # Fall back to AnonymousUser when AuthenticationMiddleware has not been
        # configured (e.g. minimal test setups without middleware).
        inner.user = getattr(original, "user", AnonymousUser())
        return inner

    @staticmethod
    def _extract_data(response: Any) -> Any:
        """
        Extract serialisable data from a DRF or Django response object.

        Returns ``response.data`` for DRF ``Response`` objects, or the string
        representation for plain ``HttpResponse`` objects.
        """
        if hasattr(response, "data"):
            return response.data
        if hasattr(response, "content"):
            try:
                return json.loads(response.content)
            except (json.JSONDecodeError, AttributeError):
                return str(response.content)
        return str(response)
