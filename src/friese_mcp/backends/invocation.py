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
import re
from io import BytesIO
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest, QueryDict
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.settings import api_settings

from friese_mcp.backends.base import BaseInvocationBackend, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


#: Detail dict keys that are wrapper artefacts (not real field names).  When
#: encountered during flattening, the value is emitted without a ``key:``
#: prefix — this is what prevents the string-in-string nesting that surfaces
#: when a host APIException carries ``{"error": "..."}`` and the envelope
#: wraps it again as ``{"error": "{'error': '...'}"}``.
_WRAPPER_DETAIL_KEYS: frozenset[str] = frozenset({"error", "detail"})

# Canonical UUID pattern (RFC 4122, case-insensitive).  Used to distinguish
# bare UUID strings — valid as-is for PrimaryKeyRelatedField — from human-
# readable name/slug strings that need to be wrapped as {"name": value} before
# reaching a host serializer that only accepts UUID or dict form.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)

# Param key names that signal a "list body" bulk-create convention.  When a write
# action's body_args dict has exactly one of these keys and its value is a list,
# the list is unwrapped and sent as the JSON array body so that host serializers
# that accept "[{...}, ...]" for bulk-create (e.g. Nautobot's create and bulk_create)
# receive the expected shape instead of {"objects": [...]}.
_LIST_BODY_KEYS: frozenset[str] = frozenset({"objects", "data", "items", "_items"})


def _is_fk_property(prop_schema: dict[str, Any]) -> bool:
    """
    Return ``True`` when *prop_schema* matches the FK ``oneOf`` pattern.

    The dispatcher emits ``{"oneOf": [{"type": "string"}, {"type": "object",
    ...}]}`` for ``PrimaryKeyRelatedField`` and custom natural-key/PK hybrid
    fields (see :data:`~friese_mcp.backends.discovery._FK_ITEM_SCHEMA`).
    ``SlugRelatedField`` deliberately emits ``{"type": "string"}`` (no
    ``oneOf``) so this predicate returns ``False`` for slug fields — their
    bare-string values must not be wrapped.
    """
    one_of = prop_schema.get("oneOf", [])
    if len(one_of) < 2:
        return False
    has_string = any(isinstance(s, dict) and s.get("type") == "string" for s in one_of)
    has_object = any(isinstance(s, dict) and s.get("type") == "object" for s in one_of)
    return has_string and has_object


def _normalize_fk_value(value: Any) -> Any:
    """
    Wrap a bare non-UUID string as ``{"name": value}``; leave everything else unchanged.

    Host serializers that support natural-key lookup (e.g. Nautobot's
    ``WritableNestedSerializer``) accept both bare UUID strings and dict forms
    ``{id, pk, name, slug}`` but reject bare name/slug strings.  Wrapping the
    value as ``{"name": ...}`` lets the host serializer resolve it without
    requiring the caller to know the object's UUID.

    UUID strings are left unchanged — they are already valid as bare FK values
    for ``PrimaryKeyRelatedField`` and natural-key hybrids alike.
    """
    if isinstance(value, str) and not _UUID_RE.match(value):
        return {"name": value}
    return value


def _extract_list_body(body_args: dict[str, Any]) -> list[Any] | None:
    """
    Return the list value when *body_args* matches the bulk-create convention.

    When a caller passes ``{"objects": [{...}, ...]}`` (or ``"data"``/``"items"``)
    as the only key in the body dict, the nested list is the intended JSON array
    body for host serializers that accept ``[{...}, ...]`` for bulk-create.
    Returns ``None`` when the dict does not match the convention (normal single-
    object create).
    """
    if len(body_args) == 1:
        key, value = next(iter(body_args.items()))
        if key in _LIST_BODY_KEYS and isinstance(value, list):
            return value
    return None


def _normalize_fk_arguments(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """
    Normalize bare non-UUID string values for FK and M2M fields in *arguments*.

    Walks the body payload before it is forwarded to the host ViewSet.  For
    each argument whose JSON Schema property matches the FK ``oneOf`` pattern
    (see :func:`_is_fk_property`):

    * If the value is a plain string and **not** a UUID → wrap as
      ``{"name": value}`` so host serializers with natural-key support can
      resolve it.
    * UUID strings, dicts, and non-string values pass through unchanged.

    For array (M2M) fields whose ``items`` match the FK pattern, the same
    normalization is applied element-wise to each list item.

    Fields with ``{"type": "string"}`` schemas (e.g. ``SlugRelatedField``,
    plain ``CharField``) are intentionally skipped — bare strings are the
    expected form for those fields.

    Args:
        arguments: Caller-supplied body arguments (before request construction).
        schema: The ``ToolDefinition.input_schema`` for the tool being invoked.

    Returns:
        A shallow copy of *arguments* with FK fields normalized; the original
        is not mutated.

    """
    props = schema.get("properties", {})
    if not props:
        return arguments

    result = dict(arguments)
    for field_name, value in arguments.items():
        prop_schema = props.get(field_name)
        if prop_schema is None:
            continue
        if _is_fk_property(prop_schema):
            result[field_name] = _normalize_fk_value(value)
        elif prop_schema.get("type") == "array" and _is_fk_property(
            prop_schema.get("items", {})
        ):
            if isinstance(value, list):
                result[field_name] = [_normalize_fk_value(item) for item in value]
    return result


def _flatten_error_detail(detail: Any) -> str:
    """
    Flatten a DRF ``detail`` tree to a single human-readable string.

    Handles the three shapes DRF emits for ``APIException.detail``:

    * ``str`` / ``ErrorDetail`` — returned as-is via ``str()``.
    * ``list`` — items joined with ``"; "``.
    * ``dict`` — entries rendered as ``"field: <flattened>"``, except for keys
      in :data:`_WRAPPER_DETAIL_KEYS` (``"error"``, ``"detail"``) which emit
      only the value to avoid ``"error: error: ..."``-style nesting.
    """
    if isinstance(detail, list):
        return "; ".join(_flatten_error_detail(item) for item in detail)
    if isinstance(detail, dict):
        parts: list[str] = []
        for field, errors in detail.items():
            errs = _flatten_error_detail(errors)
            if str(field).lower() in _WRAPPER_DETAIL_KEYS:
                parts.append(errs)
            else:
                parts.append(f"{field}: {errs}")
        return "; ".join(parts)
    return str(detail)


def _format_drf_validation_error(exc: DRFValidationError) -> str:
    """Flatten a DRF ``ValidationError.detail`` tree into a readable string."""
    return _flatten_error_detail(exc.detail)


def _exception_envelope_message(exc: BaseException) -> str:
    """
    Render *exc* for the ``ToolResult.content`` envelope without nested wrapping.

    DRF :class:`~rest_framework.exceptions.APIException` subclasses
    (``PermissionDenied``, ``NotAuthenticated``, ``Throttled``, ``NotFound``,
    ``ValidationError``, …) carry structured ``.detail``.  Calling ``str()``
    on them yields a repr of that structure (e.g.
    ``"{'error': 'You do not have permission...'}"``), which then gets
    wrapped a second time when we set ``{"error": str(exc)}`` — producing the
    string-in-string envelope reported in PKG-13 follow-up.

    Prefer ``.detail`` (flattened) when present; fall back to ``str(exc)`` for
    plain exceptions that have no DRF detail attribute.
    """
    detail = getattr(exc, "detail", None)
    if detail is None:
        return str(exc)
    return _flatten_error_detail(detail)


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

    def invoke(  # pylint: disable=too-many-locals
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
        # PKG-24: pre-flight FK normalization.  Bare non-UUID strings for
        # PrimaryKeyRelatedField / natural-key hybrid fields (e.g. Nautobot's
        # WritableNestedSerializer) are wrapped as {"name": value} so the host
        # serializer can resolve them.  SlugRelatedField and plain CharField
        # fields are identified by their {"type": "string"} schema and skipped.
        #
        # Bulk-create convention: if the body dict has exactly one key in
        # _LIST_BODY_KEYS (e.g. {"objects": [{...}, ...]}) the list is unwrapped
        # so the host serializer receives a JSON array body rather than a nested
        # dict.  FK normalization is skipped for list bodies (the host serializer
        # accepts the items as-is; agents should provide UUIDs or {"name": ...}
        # dicts directly in each element).
        if body_args:
            list_body = _extract_list_body(body_args)
            if list_body is not None:
                body_args = list_body  # type: ignore[assignment]
            else:
                body_args = _normalize_fk_arguments(body_args, tool.input_schema)
        inner_req = self._build_request(http_method, body_args, query_args, request)

        # Pass DRF's default parsers so the synthetic Request can deserialise
        # application/json bodies from write actions.
        parsers = [cls() for cls in api_settings.DEFAULT_PARSER_CLASSES]  # type: ignore[operator]
        drf_request = Request(inner_req, parsers=parsers)

        # Pre-cache user/auth on the DRF Request so it never invokes
        # _authenticate() on the synthetic inner request.  The synthetic request
        # has no Authorization header (auth happened on the outer MCP gateway
        # request), so a lazy ``drf_request.user`` access would resolve to
        # AnonymousUser — silently breaking any host serializer that calls
        # ``self.context['request'].user.is_authenticated`` or queryset-scoping
        # helpers like ``.restrict(user, 'view')`` for permission-aware FK
        # lookups.  Setting the private slots directly skips the lazy path.
        drf_request._user = getattr(  # pylint: disable=protected-access
            request, "user", AnonymousUser()
        )
        drf_request._auth = getattr(  # pylint: disable=protected-access
            request, "auth", None
        )
        drf_request._authenticator = None  # pylint: disable=protected-access

        # Populate accepted_renderer / accepted_media_type so ViewSets that access
        # these attributes (standard since DRF 3.14) do not raise AttributeError.
        # APIView.dispatch() normally calls perform_content_negotiation() which sets
        # these; the synthetic path bypasses dispatch, so we do it explicitly here.
        (
            drf_request.accepted_renderer,
            drf_request.accepted_media_type,
        ) = DefaultContentNegotiation().select_renderer(
            drf_request,
            [cls() for cls in api_settings.DEFAULT_RENDERER_CLASSES],  # type: ignore[operator]
            None,
        )

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

        # Run the standard DRF lifecycle hook so any host-app logic that lives
        # in initial() — per-user queryset scoping, tenancy filtering, RBAC
        # overlays, request.version setup, throttles — fires before the
        # action.  Without this, SyncInvocation silently bypasses
        # object-level permission scoping and leaks rows the caller has no
        # right to see.
        #
        # _ignore_model_permissions bypasses DjangoObjectPermissions (and all
        # subclasses, e.g. Nautobot's TokenPermissions) has_permission() check.
        # Without it, every non-superuser token needs a host-app ObjectPermission
        # configured per model — impractical for large surfaces (1500+ Nautobot
        # models).  Our MCP tier system is the primary access gate; the host
        # app's queryset restriction (restrict_queryset / .restrict()) still
        # fires, so callers without ObjectPermissions see an empty result set
        # rather than a 403.  Write operations remain protected by Nautobot's
        # post-save _validate_objects() which rolls back creates that fall
        # outside the restricted queryset.
        viewset._ignore_model_permissions = True  # pylint: disable=protected-access
        try:
            viewset.initial(drf_request, **view_kwargs)
        except (DRFValidationError, DjangoValidationError):
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # PermissionDenied, NotAuthenticated, Throttled, etc. surface here.
            # Convert to a structured tool error so MCP clients see a clean
            # denial rather than a 500.  Use _exception_envelope_message so
            # DRF APIException.detail is unwrapped instead of str()-ified into
            # a string-in-string envelope.
            message = _exception_envelope_message(exc)
            logger.warning(
                "SyncInvocation: viewset.initial() denied call",
                extra={"tool": tool.name, "error": message},
            )
            return ToolResult(content={"error": message}, is_error=True)

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
            message = _exception_envelope_message(exc)
            logger.exception(
                "SyncInvocation error",
                extra={"tool": tool.name, "error": message},
            )
            return ToolResult(content={"error": message}, is_error=True)

        # Response normalisation lives in its own try/except so that a failure
        # to render DRF-native types (e.g. an unrenderable custom field) becomes
        # a structured tool-level error instead of a 500 from the JSON-RPC
        # envelope encoder upstream.
        try:
            content = self._extract_data(response)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "SyncInvocation response normalisation failed",
                extra={"tool": tool.name, "error": str(exc)},
            )
            return ToolResult(
                content={"error": f"Failed to serialise response: {exc}"},
                is_error=True,
            )

        return ToolResult(content=content)

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
        req = HttpRequest()
        req.method = http_method.upper()
        req.path = "/"
        req.META["SERVER_NAME"] = "localhost"
        req.META["SERVER_PORT"] = "80"

        qs = urlencode(query_args, doseq=True) if query_args else ""
        req.META["QUERY_STRING"] = qs
        req.GET = QueryDict(qs)

        if http_method not in _GET_METHODS and http_method != "delete" and body_args:
            body_bytes = json.dumps(body_args).encode("utf-8")
            req.META["CONTENT_TYPE"] = "application/json"
            req.META["CONTENT_LENGTH"] = str(len(body_bytes))
            req._stream = BytesIO(body_bytes)  # type: ignore[attr-defined]  # pylint: disable=protected-access
        else:
            req._stream = BytesIO(b"")  # type: ignore[attr-defined]  # pylint: disable=protected-access
        # DRF 3.17 _load_stream() accesses _request._read_started directly; the
        # attribute is not initialised in HttpRequest.__init__ but only set on
        # first read().  Set it explicitly so the synthetic request works.
        req._read_started = False  # type: ignore[attr-defined]  # pylint: disable=protected-access

        # Forward the authenticated user from the original MCP gateway request.
        # Fall back to AnonymousUser when AuthenticationMiddleware has not been
        # configured (e.g. minimal test setups without middleware).
        req.user = getattr(original, "user", AnonymousUser())
        return req

    @staticmethod
    def _extract_data(response: Any) -> Any:
        """
        Extract JSON-safe data from a DRF or Django response object.

        Handles four shapes:

        * **HTTP 204 No Content** (the canonical destroy / DELETE response):
          returns a structured ``{"deleted": True, "status": 204}`` envelope
          so MCP clients see a clear success signal instead of ``None`` (which
          upstream wraps as the misleading ``{"error": ""}`` content seen in
          PKG-13).
        * **DRF Response with non-None data**: routes ``response.data``
          through DRF's :class:`~rest_framework.renderers.JSONRenderer` and
          parses the result back to JSON-safe primitives.  This converts
          DRF-native types — ``uuid.UUID``, ``datetime``,
          :class:`decimal.Decimal`, ``OrderedDict`` subclasses — into the
          string / number forms that the upstream stdlib ``json.dumps()``
          can serialise without raising :class:`TypeError`.
        * **DRF Response with data=None but a non-204 success status**:
          returns ``{"status": <code>}`` so the envelope stays well-formed.
        * **Plain Django HttpResponse**: parses ``response.content`` as JSON,
          falling back to its string repr.

        The JSONRenderer normalisation generalises GAP-NAUTO-G — any DRF host
        app whose serializers return UUID PKs, datetime fields, or Decimal
        fields would otherwise crash the JSON-RPC envelope encoder.
        """
        status_code = getattr(response, "status_code", None)

        # 204 No Content — structured success envelope, never None.
        if status_code == 204:
            return {"deleted": True, "status": 204}

        if hasattr(response, "data"):
            data = response.data
            if data is None:
                # Some custom actions return Response(status=2xx) with no body.
                # Surface a non-empty envelope rather than None so callers and
                # MCP clients can render the result without a fake error wrap.
                return {"status": status_code} if status_code is not None else {}
            rendered = JSONRenderer().render(data)
            if not rendered or rendered == b"null":
                return None
            return json.loads(rendered)

        if hasattr(response, "content"):
            try:
                return json.loads(response.content)
            except (json.JSONDecodeError, AttributeError):
                return str(response.content)
        return str(response)
