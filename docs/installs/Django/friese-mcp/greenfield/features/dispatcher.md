# Feature: @mcp_dispatcher + @mcp_action

**Audience:** Developers managing large API surfaces  
**Package version:** 1.0.x

---

## The problem this solves

Auto-discovery registers one MCP tool per ViewSet action. A typical DRF project with 20 models exposes 100–200 tools; a large platform like Nautobot or NetBox exposes 1,000–2,000. Every tool's full schema loads into the agent's context window on `tools/list`. At hundreds of tools, the schema overhead alone exhausts the context budget before the agent can do any useful work.

The dispatcher pattern collapses N tools into 1. The agent calls a single dispatcher tool with an `action` parameter to route to the underlying operation. Context overhead stays constant regardless of how many actions the dispatcher covers.

See [Dispatcher Pattern](../../../../../Guide/dispatcher-pattern.md) and [The Token Problem](../../../../../Guide/the-token-problem.md) for the design rationale and measured token numbers.

---

## How it works

A dispatcher is a class where each method decorated with `@mcp_action` becomes a routable action. The class is registered as a single MCP tool. Agents call it with `{"action": "action_name", "params": {...}}`. Passing `action="help"` (or omitting `action`) returns a structured listing of all available actions.

---

## Decorators

### `@mcp_dispatcher`

```python
from frisian_mcp import mcp_dispatcher

@mcp_dispatcher(
    name="orders",
    description="Manage customer orders: list, create, retrieve, update, cancel.",
    permission_classes=[IsAuthenticated],  # optional gateway-level guard
)
class OrdersDispatcher:
    ...
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | `str` | Yes | Tool name in `tools/list`. |
| `description` | `str` | Yes | Human-readable description for the agent. |
| `permission_classes` | `list` | No | DRF permission classes for the dispatcher entry point. Per-action tiers are enforced separately. |

### `@mcp_action`

```python
from frisian_mcp import mcp_action

@mcp_action(
    name="list",
    description="List all orders. Supports status and date_from filters.",
    params={
        "status": "Filter by status: open | paid | shipped | cancelled",
        "date_from": "ISO 8601 date — return orders on or after this date",
        "limit": "Maximum number of results (default 20)",
    },
    write=False,
    admin=False,
)
def list_orders(self, arguments: dict, request: HttpRequest) -> list:
    ...
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | `str` | Yes | Action name used in the `action` field of a dispatch call. |
| `description` | `str` | Yes | Shown in help-mode responses. |
| `params` | `dict[str, str]` | No | Param name → human-readable hint. Shown in help mode. |
| `input_schema` | `dict` | No | Full JSON Schema for per-call validation. Optional; `params` is sufficient for most cases. |
| `write` | `bool` | No | `True` → `permission_tier="read_write"`. |
| `admin` | `bool` | No | `True` → `permission_tier="admin"`. |

---

## Full example

```python
# myapp/dispatchers.py

from django.http import HttpRequest
from rest_framework.permissions import IsAuthenticated
from frisian_mcp import mcp_action, mcp_dispatcher

@mcp_dispatcher(
    name="orders",
    description="Manage customer orders: list, retrieve, create, cancel.",
    permission_classes=[IsAuthenticated],
)
class OrdersDispatcher:

    @mcp_action(
        name="list",
        description="List orders, optionally filtered by status.",
        params={"status": "open | paid | shipped | cancelled"},
    )
    def list_orders(self, arguments: dict, request: HttpRequest) -> list:
        from myapp.models import Order
        qs = Order.objects.all()
        if status := arguments.get("status"):
            qs = qs.filter(status=status)
        return list(qs.values("id", "status", "total", "created_at")[:50])

    @mcp_action(
        name="retrieve",
        description="Get a single order by ID.",
        params={"order_id": "Primary key of the order."},
    )
    def retrieve_order(self, arguments: dict, request: HttpRequest) -> dict:
        from myapp.models import Order
        try:
            order = Order.objects.get(pk=arguments["order_id"])
        except Order.DoesNotExist:
            raise ValueError(f"Order {arguments['order_id']} not found")
        return {"id": order.pk, "status": order.status, "total": str(order.total)}

    @mcp_action(
        name="cancel",
        description="Cancel an open order.",
        params={"order_id": "Primary key of the order to cancel."},
        write=True,
    )
    def cancel_order(self, arguments: dict, request: HttpRequest) -> dict:
        from myapp.models import Order
        try:
            order = Order.objects.get(pk=arguments["order_id"])
        except Order.DoesNotExist:
            raise ValueError(f"Order {arguments['order_id']} not found")
        if order.status not in ("open", "paid"):
            raise ValueError(f"Cannot cancel order with status '{order.status}'")
        order.cancel()
        return {"order_id": order.pk, "status": order.status}
```

Import the dispatcher class from your AppConfig so it registers at startup:

```python
# myapp/apps.py

from django.apps import AppConfig

class MyAppConfig(AppConfig):
    name = "myapp"

    def ready(self) -> None:
        import myapp.dispatchers  # noqa: F401
```

---

## Agent interaction

The agent calls the dispatcher like any other tool:

```json
{
  "method": "tools/call",
  "params": {
    "name": "orders",
    "arguments": {
      "action": "list",
      "params": {"status": "open", "limit": 10}
    }
  }
}
```

Calling with `action="help"` or omitting `action` returns a structured listing:

```json
{
  "actions": [
    {"name": "list", "description": "List orders...", "params": {...}},
    {"name": "retrieve", "description": "Get a single order...", "params": {...}},
    {"name": "cancel", "description": "Cancel an open order.", "params": {...}, "tier": "read_write"}
  ]
}
```

---

## Dispatcher shadowing

When a dispatcher named `orders` is registered, auto-discovery suppresses any flat tools whose name prefix matches — e.g., `orders.list`, `orders.create`, `order.list` (singular form). This prevents duplicate tool registration. The dispatcher replaces the flat tool surface for that resource.

---

## Per-action permission tiers

The dispatcher entry point itself is always registered at `read` tier so it appears in `tools/list` for all callers. Per-action enforcement happens at dispatch time:

- A caller with only a `read` token calling a `write=True` action receives a `PermissionError` → `isError=True` response.
- The help response only shows actions the caller's tier can reach.

---

## See also

- [Dispatcher Pattern](../../../../../Guide/dispatcher-pattern.md) — design rationale and token efficiency measurements
- [The Token Problem](../../../../../Guide/the-token-problem.md) — why this matters at scale
- `features/mcp-tool.md` — for standalone tools that don't need a dispatcher
- `features/mcp-heavy.md` — for dispatcher actions that return large result sets
