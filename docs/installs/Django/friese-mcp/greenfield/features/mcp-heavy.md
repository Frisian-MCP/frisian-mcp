# Feature: @mcp_heavy — Large-Response Negotiation

**Audience:** Developers working with tools that return large datasets  
**Package version:** 1.0.x

---

## The problem this solves

A tool that returns a list of 500 objects floods the agent's context window in a single call. The agent cannot reason or plan — it just spent its entire context budget receiving data it may not have needed. `@mcp_heavy` implements a two-call protocol: the first call returns a preview and a continuation token, and the second call returns only as much data as the agent actually needs.

See [Read-Response Filtering](../../../../../Guide/read-response-filtering.md) for the design rationale and measured numbers.

---

## How it works

**Call 1 — probe.** The tool executes, its result is cached (5-minute TTL), and the caller receives a probe envelope:

```json
{
  "preview": "<first 200 chars of the result>",
  "total_size": 48201,
  "available_modes": ["summary", "paginated", "filtered", "full"],
  "continuation_token": "<opaque token>"
}
```

**Call 2 — fetch.** Re-invoke the same tool with `continuation_token` + `mode`. The tool does not execute again — the cached result is served in the requested mode.

| Mode | Returns |
|------|---------|
| `summary` | First 10 dict keys / first 5 list items, values truncated to 100 chars |
| `paginated` | One page of results; pass `page` (default 1) and `page_size` (default `frisian_MCP_HEAVY_PAGE_SIZE` or 20) |
| `filtered` | Result filtered to the keys listed in `filter_keys` |
| `full` | Complete original result |

The five negotiation fields (`continuation_token`, `mode`, `page`, `page_size`, `filter_keys`) are automatically merged into the tool's `inputSchema` so agents see the protocol in `tools/list`.

---

## Signature

```python
from frisian_mcp import mcp_heavy

@mcp_heavy(
    name="products.search",
    description="Search products. Returns a probe on first call; pass continuation_token + mode to fetch.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search term"},
            "category": {"type": "string", "description": "Optional category filter"},
        },
    },
    permission_classes=[IsAuthenticated],
    write=False,
    admin=False,
)
def search_products(arguments: dict, request: HttpRequest) -> list:
    ...
```

The signature is identical to `@mcp_tool`. The difference is the gateway behaviour on the return value — it caches the result and returns a probe envelope instead of the raw data.

---

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | `str` | Yes | — | Unique tool name. |
| `description` | `str` | Yes | — | Human-readable description. |
| `input_schema` | `dict` | Yes | — | JSON Schema. Negotiation fields are merged in automatically. |
| `permission_classes` | `list` | No | `None` | DRF permission classes. |
| `write` | `bool` | No | `False` | Set `True` → `read_write` tier. |
| `admin` | `bool` | No | `False` | Set `True` → `admin` tier. |

---

## Full example

```python
from django.http import HttpRequest
from rest_framework.permissions import IsAuthenticated
from frisian_mcp import mcp_heavy

@mcp_heavy(
    name="inventory.list_all",
    description=(
        "List all inventory records. "
        "Returns a probe on first call — use continuation_token + mode to fetch data."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "warehouse": {"type": "string", "description": "Filter by warehouse ID"},
        },
    },
    permission_classes=[IsAuthenticated],
)
def list_inventory(arguments: dict, request: HttpRequest) -> list:
    from myapp.models import InventoryItem
    qs = InventoryItem.objects.select_related("product", "warehouse")
    if wh := arguments.get("warehouse"):
        qs = qs.filter(warehouse_id=wh)
    return list(qs.values("id", "product__name", "quantity", "warehouse__name"))
```

**Agent call 1 — probe:**

```json
{
  "method": "tools/call",
  "params": {
    "name": "inventory.list_all",
    "arguments": {"warehouse": "WH-01"}
  }
}
```

Response:

```json
{
  "preview": "[{\"id\": 1, \"product__name\": \"Widget A\", \"quantity\": 42 ...",
  "total_size": 52840,
  "available_modes": ["summary", "paginated", "filtered", "full"],
  "continuation_token": "abc123xyz"
}
```

**Agent call 2 — paginated fetch:**

```json
{
  "method": "tools/call",
  "params": {
    "name": "inventory.list_all",
    "arguments": {
      "continuation_token": "abc123xyz",
      "mode": "paginated",
      "page": 1,
      "page_size": 25
    }
  }
}
```

Response:

```json
{
  "items": [...],
  "page": 1,
  "page_size": 25,
  "total": 430,
  "has_more": true
}
```

---

## Continuation token expiry

Tokens expire after 5 minutes (`_HEAVY_CACHE_TTL = 300`). If the token has expired, the response is:

```json
{
  "error": "Continuation token expired or not found. Re-invoke without continuation_token to start a new negotiation."
}
```

The agent should retry without the token to get a fresh probe.

---

## Threshold backstop (secondary, v2)

Setting `frisian_MCP_AUTO_NEGOTIATE_THRESHOLD` in Django settings automatically wraps any tool response — including plain `@mcp_tool` tools — that exceeds the byte threshold in a probe envelope:

```python
# settings.py

# Auto-negotiate responses larger than 50 KB
frisian_MCP_AUTO_NEGOTIATE_THRESHOLD = 50_000
```

This is a safety net for tools you didn't know would return large responses. Prefer `@mcp_heavy` for explicit control — it documents the intention in the code and in `tools/list`.

---

## Page size setting

```python
# settings.py

# Default page size for paginated mode (default 20 if unset)
frisian_MCP_HEAVY_PAGE_SIZE = 50
```

---

## See also

- [Read-Response Filtering](../../../../../Guide/read-response-filtering.md) — design rationale and token efficiency measurements
- `features/mcp-tool.md` — for tools that return small responses
- `features/dispatcher.md` — for using heavy tools inside a dispatcher
