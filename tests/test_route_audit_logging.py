"""
Tests for PR-10 — the DOC-7 audit-context logging seam.

Every ``tools/call`` emits exactly one structured record on the dedicated
``frisian_mcp.audit`` logger carrying the values the request path already
computed: matched route (tier key), canonical mount path, effective ceiling
after the cap, effective tier, the addressed tool and — for dispatcher calls
only — its ``resource``/``action`` labels, and the resolved allow/deny
decision.  A durable sink later attaches a handler to that logger and consumes
the records verbatim; nothing is recomputed and nothing needs transformation.

The payload is routing vocabulary only: caller argument values, token
material, and user identity never appear.

All fixtures use neutral names (group ``catalog``, resources ``item`` /
``order``, flat tool ``ping``) per the package-neutrality ruling.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest
from django.test import override_settings

from frisian_mcp.registry import ToolRegistry
from frisian_mcp.route_views import route_views
from tests.test_route_wiring import (
    GATEWAY,
    GATEWAY_ELEVATED,
    _cfg,
    _make_registry,
    _mount,
    _post_jsonrpc,
    _StubUser,
    _tier_hook,
)

AUDIT_LOGGER = "frisian_mcp.audit"


@pytest.fixture()
def registry() -> ToolRegistry:
    """Return a freshly-built neutral registry (catalog group + ping)."""
    return _make_registry()


@pytest.fixture()
def clean_route_views() -> Generator[Any, None, None]:
    """Snapshot and restore the process-scoped RouteViewRegistry singleton."""
    with route_views._lock:  # noqa: SLF001
        saved = dict(route_views._views)  # noqa: SLF001
    yield route_views
    with route_views._lock:  # noqa: SLF001
        route_views._views = saved  # noqa: SLF001


def _audit_records(caplog: Any) -> list[logging.LogRecord]:
    """Return the captured audit-context records."""
    return [r for r in caplog.records if r.name == AUDIT_LOGGER]


def _call(view: Any, path: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """POST a tools/call for *name* to *view*."""
    return _post_jsonrpc(
        view,
        path,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        user=_StubUser(),
    )


@pytest.mark.usefixtures("clean_route_views")
class TestAuditContextSeam:
    """One record per resolved call, carrying the already-computed context."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_allowed_call_emits_full_context(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An allowed flat-tool call records route, path, tiers, and allow."""
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping")
        records = _audit_records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.route == "default"
        assert record.route_path == GATEWAY
        assert record.effective_ceiling == "read"  # secure default, post-cap
        assert record.effective_tier == "read"
        assert record.tool == "ping"
        assert record.decision == "allow"
        assert record.reason is None

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_route_denied_tool_records_deny_absent(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A deny-listed tool resolves to deny/absent."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("ping",)), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping")
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "absent"
        assert record.tool == "ping"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_tier_hidden_tool_records_deny_absent(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A tool above the effective tier resolves to deny/absent, not permission."""
        view = _mount(_cfg("default", GATEWAY), registry)  # ceiling read
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "item_create", {"name": "x"})
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "absent"
        assert record.effective_tier == "read"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_dispatcher_member_records_resource_labels(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dispatcher call records its resource/action config labels."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "catalog", {"resource": "item", "action": "list"})
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "absent"
        assert record.tool == "catalog"
        assert record.resource == "item"
        assert record.tool_action == "list"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_permission_class_denial_records_deny_permission(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A DRF permission-class denial resolves to deny/permission."""
        from rest_framework.permissions import BasePermission

        class _DenyAll(BasePermission):
            def has_permission(self, request: Any, view: Any) -> bool:
                return False

        reg = _make_registry()
        reg.register(
            name="ping_gated",
            fn=lambda arguments, request: {"ok": True},
            description="gated ping",
            input_schema={"type": "object", "properties": {}},
            permission_classes=[_DenyAll],
            permission_tier="read",
        )
        view = _mount(_cfg("default", GATEWAY), reg)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping_gated")
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "permission"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_no_caller_values_or_secret_material_in_payload(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Argument values never reach the audit record — labels only."""
        view = _mount(_cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY_ELEVATED, "item_create", {"name": "s3cret-value"})
        (record,) = _audit_records(caplog)
        assert record.decision == "allow"
        serialized = repr(vars(record))
        assert "s3cret-value" not in serialized

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_flat_tool_action_argument_is_not_logged(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resource/action keys on a FLAT tool are caller data, not labels."""
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping", {"action": "caller-data", "resource": "caller-data"})
        (record,) = _audit_records(caplog)
        assert record.tool_action is None
        assert record.resource is None

    def test_unknown_dispatcher_resource_is_not_logged_verbatim(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dispatcher resource that names no real member is caller data, not a label.

        Only resource/action that resolve to a real group member are logged; an
        arbitrary (potentially PII) value on the unknown-pair deny path must not
        reach the sink verbatim — unlike a real-but-denied resource, which does.
        """
        view = _mount(_cfg("default", GATEWAY, allow=("*",)), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "catalog", {"resource": "ssn-123-45-6789", "action": "list"})
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.resource is None
        assert record.tool_action is None
        assert "ssn-123-45-6789" not in repr(vars(record))

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_unknown_tool_name_is_sanitized_but_kept(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The probed tool name is kept for forensics but stripped of injection chars.

        V11-26 #10: on the unknown-tool path `tool_name` is caller input. CR's
        remedy (log a fixed 'unknown') would erase the probe an audit trail
        exists to record; sanitize-and-keep instead — control chars gone, the
        probed name preserved.
        """
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "secret_list\r\nFORGED entry")
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "absent"
        # Probe preserved (not dropped to a fixed label) ...
        assert record.tool is not None and "secret_list" in record.tool
        # ... but the CR/LF injection primitive is stripped.
        assert "\r" not in record.tool and "\n" not in record.tool

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_overlong_tool_name_is_truncated(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unbounded caller name is capped so it can't be a log-storage vector."""
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "z" * 5000)
        (record,) = _audit_records(caplog)
        assert record.tool is not None
        assert len(record.tool) < 200
        assert record.tool.endswith("…[truncated]")

    def test_continuation_expired_emits_audit_record(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pre-dispatch continuation short-circuit is audited (DOC-7 coverage gap)."""
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping", {"continuation_token": "does-not-exist"})
        (record,) = _audit_records(caplog)
        assert record.decision == "deny"
        assert record.reason == "continuation_expired"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_legacy_plain_view_records_request_path_and_no_route(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Plain mounts record route=None with the request path; tier uncapped."""
        from frisian_mcp.registry import tool_registry
        from frisian_mcp.views import McpView

        tool_registry.register(
            name="audit_probe",
            fn=lambda arguments, request: {"ok": True},
            description="probe",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read",
        )
        try:
            with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
                _call(McpView.as_view(), "mcp", "audit_probe")
        finally:
            tool_registry._tools.pop("audit_probe", None)  # noqa: SLF001
        (record,) = _audit_records(caplog)
        assert record.route is None
        assert record.route_path == "/mcp"
        assert record.effective_ceiling is None
        assert record.effective_tier == "admin"
        assert record.decision == "allow"

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_exactly_one_record_per_call(
        self, registry: ToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two calls, two records — the finally never double-fires."""
        view = _mount(_cfg("default", GATEWAY), registry)
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            _call(view, GATEWAY, "ping")
            _call(view, GATEWAY, "nonexistent_tool")
        records = _audit_records(caplog)
        assert len(records) == 2
        assert [r.decision for r in records] == ["allow", "deny"]
