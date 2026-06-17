# Feature: @mcp_tool — Manual Tool Registration

**Audience:** Developers registering custom tools or function-based views  
**Package version:** 1.0.x

---

## When to use

Auto-discovery registers DRF ViewSet actions automatically. Use `@mcp_tool` when you need to:

- Register a standalone function (not a ViewSet action)
- Register a function-based view (`@api_view`)
- Control the tool name, description, and input schema explicitly
- Write a tool that aggregates several ViewSet calls into one agent-facing operation
- Register a tool that does not map to any URL

---

## Signature

```python
from frisian_mcp import mcp_tool

@mcp_tool(
    name="my_tool",
    description="One-line description shown in tools/list.",
    input_schema={
        "type": "object",
        "properties": {
            "param": {"type": "string", "description": "..."}
        },
        "required": ["param"],
    },
    permission_classes=[IsAuthenticated],  # optional
    write=False,   # set True for read_write tier
    admin=False,   # set True for admin tier
)
def my_tool(arguments: dict, request: HttpRequest) -> dict:
    ...
```

The decorated function signature must be `(arguments: dict[str, Any], request: HttpRequest) -> Any`. The return value must be JSON-serialisable.

---

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | `str` | Yes | — | Unique tool name. Convention: `resource.action` (e.g. `orders.cancel`). |
| `description` | `str` | Yes | — | Human-readable description included in `tools/list`. |
| `input_schema` | `dict` | Yes | — | JSON Schema (draft-07) for argument validation. |
| `permission_classes` | `list` | No | `None` | DRF permission classes. `None` or `[]` = unrestricted. |
| `write` | `bool` | No | `False` | Set `True` → `permission_tier="read_write"`. |
| `admin` | `bool` | No | `False` | Set `True` → `permission_tier="admin"`. Overrides `write`. |

---

## Examples

### Basic read tool

```python
from django.http import HttpRequest
from rest_framework.permissions import IsAuthenticated
from frisian_mcp import mcp_tool

@mcp_tool(
    name="orders.cancel",
    description="Cancel an order by ID. Returns the updated order status.",
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "integer", "description": "Primary key of the order."},
        },
        "required": ["order_id"],
    },
    permission_classes=[IsAuthenticated],
    write=True,
)
def cancel_order(arguments: dict, request: HttpRequest) -> dict:
    from myapp.models import Order
    order = Order.objects.get(pk=arguments["order_id"])
    order.cancel()
    return {"order_id": order.pk, "status": order.status}
```

### Tool with no authentication requirement

```python
@mcp_tool(
    name="health.check",
    description="Returns server health status. No authentication required.",
    input_schema={"type": "object", "properties": {}},
    permission_classes=[],
)
def health_check(arguments: dict, request: HttpRequest) -> dict:
    return {"status": "ok"}
```

### Aggregating tool

```python
@mcp_tool(
    name="dashboard.summary",
    description="Returns a combined summary: open orders count, low-stock items, and recent alerts.",
    input_schema={"type": "object", "properties": {}},
    permission_classes=[IsAuthenticated],
)
def dashboard_summary(arguments: dict, request: HttpRequest) -> dict:
    from myapp.models import Order, Product, Alert
    return {
        "open_orders": Order.objects.filter(status="open").count(),
        "low_stock": list(Product.objects.filter(stock__lt=10).values("id", "name", "stock")),
        "alerts": list(Alert.objects.order_by("-created_at")[:5].values()),
    }
```

---

## Error handling

Raise standard exceptions — the gateway surfaces them correctly to the agent:

| Exception | Agent receives |
|-----------|---------------|
| `ValueError` | `isError=True`, `error` key with the message |
| `DRF ValidationError` | `isError=True`, `detail` dict with per-field messages |
| `PermissionError` | `isError=True`, `error` key with the message |
| `LookupError` | `isError=True` (METHOD_NOT_FOUND for unknown tool names) |

```python
@mcp_tool(
    name="orders.ship",
    description="Mark an order as shipped.",
    input_schema={
        "type": "object",
        "properties": {"order_id": {"type": "integer"}},
        "required": ["order_id"],
    },
    write=True,
)
def ship_order(arguments: dict, request: HttpRequest) -> dict:
    from myapp.models import Order
    try:
        order = Order.objects.get(pk=arguments["order_id"])
    except Order.DoesNotExist:
        raise ValueError(f"Order {arguments['order_id']} not found")
    if order.status != "paid":
        raise ValueError(f"Cannot ship order with status '{order.status}'")
    order.mark_shipped()
    return {"order_id": order.pk, "status": order.status}
```

---

## Permission tiers

Tools are visible in `tools/list` and callable only up to the caller's effective permission tier:

| Tier | Set via | Visible to |
|------|---------|-----------|
| `read` | default | all callers (including unauthenticated if `frisian_MCP_UNAUTHENTICATED_TIER="read"`) |
| `read_write` | `write=True` | authenticated callers with `read_write` or `admin` tokens |
| `admin` | `admin=True` | callers with `admin` tokens only |

Token tiers are set in the Django admin under **frisian MCP → Tokens** (when `frisian_mcp.contrib.tokens` is installed).

---

## Registration location

`@mcp_tool` registers at import time. The decorated function must be imported before `AppConfig.ready()` finishes, or before `tools/list` is first called. The recommended pattern is to define decorated tools in a `tools.py` module and import it from your `AppConfig.ready()`:

```python
# myapp/apps.py

from django.apps import AppConfig

class MyAppConfig(AppConfig):
    name = "myapp"

    def ready(self) -> None:
        import myapp.tools  # noqa: F401  — registers @mcp_tool decorated functions
```

---

## See also

- `features/dispatcher.md` — for grouping many tools into one dispatcher entry point
- `features/mcp-heavy.md` — for tools that return large responses
- [The Token Problem](../../../../../Guide/the-token-problem.md) — why tool surface size matters for agent context
