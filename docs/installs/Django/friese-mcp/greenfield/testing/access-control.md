# Access Control

**Scenario:** Verify that authentication and authorization are enforced at the gateway level and at the tool level, and that misconfigured permission boundaries do not silently pass.

---

## What You Are Testing

frisian-mcp enforces access control at two layers:

1. **Gateway level** — `FRISIAN_MCP_AUTHENTICATION_CLASSES` and `FRISIAN_MCP_PERMISSION_CLASSES` gate the entire MCP view before any tool is reached.
2. **Tool level** — `permission_classes` on each `@mcp_tool` or `@mcp_dispatcher` registration are evaluated at dispatch time.

Both layers must hold independently. A tool with tight permission classes is not a substitute for a gateway that allows unauthenticated callers to reach write surfaces. Conversely, a locked gateway does not eliminate the need for per-tool permission classes on sensitive operations.

---

## Prerequisites

- frisian-mcp installed and `FRISIAN_MCP_AUTHENTICATION_CLASSES` configured
- At least one tool registered with `permission_classes=[IsAuthenticated]`
- At least one tool registered with `permission_classes=[]` (open tool — verify this is intentional)

---

## Test 1: Unauthenticated Request to Protected Gateway

**Setup:** Configure `FRISIAN_MCP_PERMISSION_CLASSES = ['rest_framework.permissions.IsAuthenticated']`. Send a `tools/list` request with no `Authorization` header.

**Pass condition:** 401 response. No tool listing returned.

**Fail condition:** 200 response with tool listing. Gateway permission classes are not being applied.

---

## Test 2: Invalid Token Is Rejected at Gateway

**Setup:** Send a `tools/list` request with `Authorization: Bearer invalid-token-value`.

**Pass condition:** 401 response. frisian-mcp's authentication classes reject the token before any tool logic runs.

---

## Test 3: Valid Token, Wrong Tier, Protected Tool

**Setup:**
- Token: `read-token` (tier `read`)
- Target: a tool registered with `write=True` on `@mcp_tool`

Attempt to invoke the write tool with the read token.

**Pass condition:** 403 response (PermissionError from the tool's `permission_classes`). The write tool does not execute.

---

## Test 4: Tool-Level Permission Classes Are Evaluated After Gateway

**Setup:** A tool registered with a custom `permission_class` that checks request user membership.

```python
class IsNetworkAdmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.groups.filter(name='network-admins').exists()

@mcp_tool(
    name="devices.delete_all",
    description="Delete all devices.",
    input_schema={"type": "object", "properties": {}},
    permission_classes=[IsAuthenticated, IsNetworkAdmin],
    admin=True,
)
def delete_all(arguments, request):
    ...
```

Attempt to invoke `devices.delete_all` with a valid token belonging to a user not in `network-admins`.

**Pass condition:** Permission denied. Tool does not execute.

**Pass condition (authorized user):** Tool executes successfully when called by a user in `network-admins`.

---

## Test 5: `@mcp_ignore` Completely Excludes a ViewSet

**Setup:** Apply `@mcp_ignore` to a ViewSet or an individual action.

```python
@mcp_ignore
class InternalAuditViewSet(viewsets.ModelViewSet):
    ...
```

Call `tools/list` with an admin token.

**Pass condition:** No tools corresponding to `InternalAuditViewSet` appear in the listing at any permission tier.

**Additional check:** Directly invoke the tool name (if you know it from the source code) without going through `tools/list`. The tool should not be reachable — it was never registered.

---

## Test 6: Gateway With No Permission Classes (Backwards-Compatible Default)

**Setup:** `FRISIAN_MCP_PERMISSION_CLASSES` is not set (default: `[]`).

**Pass condition:** Gateway allows all requests through to tool-level permission enforcement. Individual tools with `permission_classes` still gate access correctly. Tools with no `permission_classes` are accessible to all callers.

This is the default behavior. Verify it is intentional for your deployment before accepting it.

---

## Common Failures

| Symptom | Likely cause |
|---------|-------------|
| Unauthenticated caller reaches tools | `FRISIAN_MCP_PERMISSION_CLASSES` is empty and tools have no `permission_classes` |
| 401 on all requests despite valid token | Auth class not in `FRISIAN_MCP_AUTHENTICATION_CLASSES` — DRF default classes are used unless overridden |
| Custom permission class not firing | `permission_classes` not passed to `@mcp_tool` — check decorator call |
| `@mcp_ignore` tool still appears | `@mcp_ignore` applied after auto-discovery has already run — verify `AppConfig.ready()` order |

---

## Related Docs

- [Permission Visibility](permission-visibility.md)
- [Open-World Write Exposure](open-world-write-exposure.md)
- [Security Architecture](../../../../../Security/security.md)
