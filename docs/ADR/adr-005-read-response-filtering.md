# ADR 005: Read-Path Response Filtering via @mcp_heavy

**Category:** reference  
**Slug:** adr-005-read-response-filtering  
**Status:** Accepted  
**Date:** 2026-06-02

---

## Context

When an MCP agent calls a list or read endpoint, the underlying DRF ViewSet executes the query and serializes the results. The full serialized payload is returned in the tool response. For a small result set — a handful of records — this is fine. The problem is that the agent does not know how many records match the query before making the call.

A filter like `site=hq-1` might match 5 devices or 5,000. The agent constructs the call without knowing which. If the query matches 5,000 records and no pagination is in force, the full serialized result — potentially hundreds of thousands of tokens — is loaded into the agent's context window in a single operation. The context window is exhausted before the agent has evaluated a single record.

This is structurally different from the write-path problem. On write paths, the agent provided all the data, so the echo is a known quantity. On read paths, the result size is determined by the data stored in the system — unknown to the agent at call time, and potentially unbounded.

Three approaches were considered:

**Hard pagination limits** — DRF's `PAGE_SIZE` setting truncates result sets to a maximum page size. This is necessary but not sufficient. A bare list response returns only the records, not the total count. The agent receives a slice of results with no indication that it is a slice — it cannot distinguish "the 50 records I asked for" from "all 50 records matching my query." It cannot make an informed decision about whether to paginate, refine the filter, or proceed.

**Response streaming** — return records incrementally as the query executes. This prevents the full result from loading at once, but requires streaming MCP transport support (not universally available) and does not solve the core information problem: the agent still does not know how many total records exist.

**Probe-then-fetch** — separate the response into two operations: a probe call that returns metadata (total count, estimated size, pagination cursor) and a fetch call that retrieves data on a page-by-page basis. The agent learns the shape of the result before committing to receiving it. The agent can decide to refine the filter, work with a smaller page, or proceed with pagination.

The probe-then-fetch pattern is the right solution. It gives the agent the information it needs to make a decision while keeping the initial context cost bounded to a small metadata response.

## Decision

frisian-mcp implements read-path response filtering two ways: an explicit `@mcp_heavy` tool registration for hand-authored heavy tools, and the `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` byte-size backstop that applies the same probe behavior to auto-discovered ViewSet actions.

**Decorator usage:**

```python
from frisian_mcp.decorators import mcp_heavy

# @mcp_heavy is a registration factory (name/description/input_schema) wrapping a
# (arguments, request) callable — not a bare wrapper on a ModelViewSet method.
@mcp_heavy(
    name="devices.search",
    description="Search devices; returns a probe envelope with size and negotiation modes.",
    input_schema={"type": "object", "properties": {"site": {"type": "string"}}},
)
def search_devices(arguments, request):
    ...
```

Auto-discovered `ModelViewSet` actions are covered without a decorator by setting `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` (see below).

**Two-call pattern:**

When a heavy tool (or an auto-discovered action over the negotiation threshold) returns a large result, the first call functions as a probe. The response is a probe envelope:

- `preview` — a truncated preview of the result so the agent can see its shape
- `total_size` — full serialized response size in bytes
- `available_modes` — the retrieval modes (`summary`, `paginated`, `filtered`, `full`) the agent may request
- `continuation_token` — opaque token that fetches the cached result in a chosen mode
- `usage` — how to redeem the token: where the negotiation fields go, and what omitting `mode` returns. An agent mid-negotiation is not re-reading `tools/list`, so the envelope that advertises the modes must also say how to reach them. Advertising reachable modes without disclosing their placement is what made all of them look unreachable.

The agent receives the metadata it needs to make a decision. If `total_size` is small, it fetches the full result; if large, it applies additional filters or fetches deliberately in `paginated` mode. The context window is not pre-filled with records the agent may never use.

Subsequent data is fetched by re-invoking the same tool with the `continuation_token`, optionally selecting a `mode`, and continuing as needed. The tool must therefore **accept** both fields as inputs; see **Negotiation argument placement** and **Default retrieval mode** below for where they go and what omitting `mode` means.

**Automatic threshold negotiation:**

The `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` setting (in bytes) triggers automatic `@mcp_heavy` behavior on any ViewSet action — even those not explicitly decorated — when the response size exceeds the threshold. This provides a safety net for large Django applications where not every ViewSet has been individually reviewed.

**The backstop is on by default.** With no setting present it applies at a built-in default of 25,000 bytes; the setting raises or lowers that threshold, and an explicit `None` **disables** the backstop entirely. It is not opt-in — an operator who adds the setting believing they are switching the backstop on is confirming a default that was already active, and one who sets `None` believing it is the default is switching it off.

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50000  # bytes — raise the built-in default
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = None   # disable the backstop entirely
```

Disabling it is a deliberate act with a consequence worth stating: on a host with the backstop off, a tool that negotiates only via the backstop returns its full payload unbounded. The built-in default is deliberately conservative so that undecorated high-cardinality endpoints probe first without per-host configuration.

**Relationship to the discovery backend:**

The discovery backend marks `@mcp_heavy` tools in their generated schema so agents can see — in `tools/list` — that a tool returns paginated metadata rather than a complete dataset. This hint improves tool selection: an agent reading tool descriptions knows which operations require pagination planning and which return complete results.

**Cache layer and continuation-token capability:**

Probe and lean-write results are cached server-side under `_HEAVY_CACHE_PREFIX` plus a cryptographically random, opaque server-issued continuation token. The token is not derived from query parameters and does not encode query state or caller identity. The cache entry stores the result together with server-side ownership metadata. On continuation, frisian-mcp resolves the opaque token, verifies that the current caller is authorized to resume that cached result, and only then serves the selected response mode. The same cache/ownership mechanism is shared by the read-path negotiation described here and the write-path continuation mechanism described in ADR-004.

Rationale: an accepted ADR must not tell future implementations to derive a bearer-like cache locator from caller-controlled query parameters. The shipped design correctly uses an unpredictable opaque capability plus independent server-side authorization binding.

**Negotiation argument placement:**

Response-negotiation controls are MCP protocol arguments belonging to the outer tool invocation. `continuation_token`, and when applicable `mode`, `page`, `page_size`, and `filter_keys`, are placed at the **top level** of the tool argument object. They MUST NOT be nested inside `params`.

The rule is stated without reference to the tool's shape, because it must hold for all of them. A flat `@mcp_heavy` tool carries its own fields and has neither `action` nor `params`; a class dispatcher carries `action` and `params`; a group dispatcher carries `resource`, `action`, and `params`. Top level is the only description true of all three — for the dispatcher shapes the negotiation fields are siblings of those keys, and for the flat shape there are no siblings to name. Describing placement by naming sibling keys is wrong for at least one shape whichever set is chosen.

`params` remains the host/action-specific passthrough namespace. Protocol controls MUST be consumed by the MCP negotiation/dispatcher layer before host action validation and MUST NOT be taught to host serializers, filtersets, ViewSets, or other Django application code as synthetic application parameters.

Because `mode`, `page`, `page_size`, and `filter_keys` can also be legitimate host-domain parameter names, their presence alone MUST NOT cause them to be stripped from an ordinary first call. They become negotiation controls in continuation context. `continuation_token` is the only unambiguous protocol-only key.

Rationale: this preserves a generic installable Django/DRF package boundary. Host applications do not need to know about the MCP continuation protocol, and legitimate application fields named `mode`, `page`, `page_size`, or `filter_keys` remain usable.

**Default retrieval mode:**

A continuation token redeemed **without** a `mode` returns a bounded response wherever bounding is meaningful:

| Redemption | Result |
|---|---|
| bare token, collection result | one bounded page at the server default page size |
| bare token, non-collection result | the complete object — it is already bounded |
| explicit `mode="full"` | the complete cached result, always |
| unrecognised `mode` | a validation error naming the supported modes |

The complete result therefore requires an explicit `mode="full"`, and an omitted or mistyped `mode` cannot silently select the most expensive response. Pagination applies to collections; a single object is returned whole because chunking its serialization is strictly worse than returning it, at every page size. Stating the default as "one page" without that qualification would be false for every single-object read and every write confirmation.

This rule governs **both** the read-path continuation tokens described here and the write-path continuation tokens of ADR-004. There is one cache and one redemption path, with no token type to discriminate on; a change to the default reaches both. ADR-004's write-path mechanism inherits this default and does not define its own.

**Continuation ownership:**

A continuation token is a cache locator, not sufficient authorization to read the cached result. Every cached continuation entry is bound server-side to the caller/security context that created it. Redemption MUST verify the binding before serving cached data. The binding includes the originating MCP tool identity, authentication credential identity, effective permission tier, authenticated user identity when present, and agent-connection identity when the route is agent-scoped. For a grouped dispatcher, the originating tool identity is the **outer MCP dispatcher tool**, not the inner host resource/action selected through `resource` / `action` / `params`.

Client-supplied transport/session identifiers such as `Mcp-Session-Id` MUST NOT be an ownership dimension. They are not authorization credentials, can legitimately drift between probe and redemption calls, and binding continuation ownership to them can make a token issued to an otherwise unchanged authorized caller unredeemable. Excluding transport-session identity does not relax the credential/user/tier/tool/agent-connection replay boundaries.

Where the caller presents no authenticating credential, the binding degenerates to the tool and tier dimensions alone and provides no isolation *between* anonymous callers. Continuation tokens issued on unauthenticated routes MUST therefore be treated as bearer capabilities with the same exposure as the data they return.

The exact serialized owner-key representation is implementation detail and need not be stable across releases; the security dimensions and redemption invariant are the architectural contract.

## Why Not DRF Default Pagination Alone

DRF's built-in pagination truncates result sets but does not provide the metadata the agent needs. The difference is:

**DRF default pagination** returns a slice of records. The agent receives records but may not know a slice was taken. It cannot decide whether the slice is complete or partial without inspecting `count` and `next` — which are only present if the ViewSet uses a pagination class that includes them.

**`@mcp_heavy`** guarantees the structured metadata is always present in the response, independent of the ViewSet's pagination class configuration. The decorator enforces this contract at the MCP layer rather than relying on DRF configuration being set correctly for every ViewSet.

Using both — DRF `PAGE_SIZE` plus `@mcp_heavy` annotation — is the recommended production configuration. DRF pagination limits database query cost; `@mcp_heavy` ensures the agent receives actionable metadata regardless of internal configuration details.

## Consequences

**Positive.** Agents calling list endpoints on large datasets no longer risk context window exhaustion from a single call. The first response is bounded to a small metadata envelope plus the first page of results, regardless of total dataset size.

**Positive.** The agent gains information it would not have from a plain paginated response: the total record count, estimated data size, and a cursor for deliberate pagination. These allow better agent decision-making (refine the filter, work with a summary, or paginate knowingly).

**Positive.** The decorator is additive. Applying `@mcp_heavy` to a ViewSet does not change its behavior for non-MCP callers. The standard DRF response path is unaffected; only MCP-routed calls receive the probe-first response.

**Positive.** The auto-negotiate threshold provides a passive safety net. Large responses on undecorated ViewSets are automatically handled without requiring explicit decoration of every ViewSet in a large application.

**Negative.** List operations that previously returned results in one call now require two calls when the agent chooses to paginate: one probe, then one or more page fetches. For agents fetching small, known-bounded datasets — a filter that reliably returns only a handful of records — the probe call is overhead without benefit.

**Negative.** The decorator requires the developer to identify which ViewSets serve large result sets. While auto-negotiation provides a fallback, the optimal configuration — explicit `@mcp_heavy` decoration on the right ViewSets — requires that review.

**Negative.** The two-call pattern introduces dependency on the server-side cache. If the cache is cleared or expires between the probe call and the first page fetch, the agent receives an error on the continuation call. Cache TTL must be set generously enough for realistic agent interaction pacing.

The cost of an extra round-trip on large list operations is far smaller than the cost of exhausting the context window. The probe-then-fetch pattern is the right default for production MCP servers against any dataset that could exceed a few dozen records.

## Validation

The `@mcp_heavy` pattern was validated against a large open-source Django application with production data. Without the decorator, a list call returning a full dataset would produce response payloads that scale linearly with record count, quickly overwhelming any practical context window. With the decorator in place, the first response is bounded to a predictable metadata envelope and a single page of results, giving the agent the total count and pagination cursor needed to proceed.

The same cache infrastructure introduced for `@mcp_heavy` was subsequently reused for the write-path continuation token (ADR-004), confirming the design is general enough to serve both read and write response filtering needs.

## Amendments

### 2026-08-07 — negotiation contract, cache-token semantics, and continuation ownership

Carried by branch `bug/Heavy-response-continuation-unredeemable` (code changes in commit `0011e3c` and its successors; this amendment is documentation only and changes no behaviour). Status remains **Accepted** — this is an amendment, not a supersession.

Anyone who implemented against the previous text should read item (c) first: the superseded wording specified a cache key derived from query parameters, which is a deterministic, caller-predictable locator for another caller's cached result. That guidance stood accepted from 2026-06-02 until this amendment. The shipped implementation never followed it.

**(c) Cache layer — corrected.** Under **Cache layer**, previously:

> "Probe results are cached server-side keyed by `_HEAVY_CACHE_PREFIX` + a token derived from the query parameters. The continuation token returned in the probe response encodes this cache key."

Now: the token is cryptographically random, opaque, and server-issued; it is not derived from query parameters and encodes neither query state nor caller identity; the entry carries server-side ownership metadata and redemption verifies authorization before serving. The heading is now **Cache layer and continuation-token capability**.

This was an editing slip rather than an outgrown design: the probe-envelope field list in the same document already described `continuation_token` as an "opaque token", so the ADR contradicted itself from the day it was accepted.

**(d) Continuation ownership — new.** The ADR previously said nothing about ownership of a cached continuation; the constraint existed only in the implementation. Now recorded: the binding dimensions, the requirement to verify before serving, that grouped dispatchers bind the **outer** tool identity, that client-supplied transport/session identifiers MUST NOT bind, and that on unauthenticated routes the binding degenerates and tokens must be treated as bearer capabilities.

**(a) Negotiation argument placement — new.** Previously unspecified in any ADR. Now recorded as a decision of this ADR — not derived from ADR-002 or ADR-007, neither of which specified the composition rule. Stated shape-neutrally (top level, never inside `params`) because the three tool shapes do not share a sibling key set. Also records that four of the five fields may collide with legitimate host-domain parameter names and so are protocol controls only in continuation context.

**(b) Default retrieval mode — changed contract.** Previously the document did not state what a bare `continuation_token` returns; the implementation returned the complete result, and an unrecognised mode silently fell back to it. Now recorded, matching the shipped behaviour as of this release: bare redemption is bounded where bounding is meaningful, `full` requires an explicit `mode="full"`, and an unrecognised mode is a validation error. This is a behaviour change, ruled and implemented in the same release as this amendment so the ADR and the code state the same contract. It governs ADR-004's write-path tokens equally, since both share one cache and one redemption path.

**Sweep — three further corrections in the same pass.**

- The two-call paragraph previously read "re-invoking the same tool with the `continuation_token` **and a `mode`**", which under the amended (b) would imply `mode` is required. It now states that `mode` is optional and that the tool must accept both fields as inputs.
- The probe-envelope field list documented four fields; the envelope returns five. `usage` — which carries the placement instruction and the cost of omitting `mode` — was missing.
- The auto-negotiate threshold was presented as a setting to add, implying the backstop is opt-in. It ships **on** at a 25,000-byte default; the setting adjusts the threshold and an explicit `None` disables it. The previous framing could lead an operator to disable the backstop while believing they were confirming the default.

**Not changed.** The Context, the three considered approaches, "Why Not DRF Default Pagination Alone", Consequences, and Validation are untouched. No section was restructured, retitled, or restyled beyond the one heading named above.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
