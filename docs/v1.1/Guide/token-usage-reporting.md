# Token Usage Reporting

**Category:** guide  
**Slug:** token-usage-reporting  
**Audience:** Operators enabling per-call token visibility, and developers/agents consuming the `_usage` block

---

## What This Document Is

The dispatcher pattern, `@mcp_heavy`, and `@mcp_light` exist to keep agent context
windows from being exhausted by tool schemas, read payloads, and write echoes.
Those savings are real, but until now they were invisible from inside a session —
an operator tuning dispatch groups or a developer profiling an agent workflow had
no per-call measurement of what a request actually cost.

**Token usage reporting** closes that gap. It attaches an opt-in `_usage` block to
each successful dispatcher result, reporting the token cost of the tool's input
schema, the request arguments, and the emitted result — measured with the same
tokenizer agents are billed against. It is a first-class observability feature of
the dispatcher pattern, not a debug flag or a stub toggle.

It is **off by default**, additive when off (responses are byte-identical to a
build without the feature), and stateless — there is no persistence, no ledger,
and no billing. It measures a single call and reports the numbers; nothing is
stored or summed across calls.

For *why* these token costs matter and where they come from, see
[The Token Problem at MCP Scale](the-token-problem.md). This guide covers how to
turn the measurement on and how to read it.

---

## The `_usage` Block

When reporting is enabled for a call, the `tools/call` result carries a `_usage`
block as a **sibling** of `content` and `isError` — it is never merged into the
serialized tool payload inside `content[0].text`:

```json
{
  "content": [{"type": "text", "text": "{ ...tool result or lean envelope... }"}],
  "isError": false,
  "_usage": {
    "schema_tokens": 15,
    "request_tokens": 3,
    "result_tokens": 10,
    "total_tokens": 28,
    "encoding": "cl100k_base"
  }
}
```

Placing `_usage` as a sibling (rather than inside the payload) keeps it uniform
across every response shape — tool payloads are sometimes JSON objects, sometimes
arrays (full reads, served-heavy lists), sometimes bare strings — and keeps
`result_tokens` honest, since it counts exactly the `content[0].text` that was
emitted, with no self-reference.

### Field Semantics

| Field | What it counts |
|---|---|
| `schema_tokens` | The tool `inputSchema` **as surfaced to this caller** — the tier/permission-filtered schema, or the constant group schema for a dispatcher. Serialized with `json.dumps` and counted. Never the full raw-registry schema. |
| `request_tokens` | The original inbound tool arguments for this call, serialized and counted — what the agent actually spent to make the request. |
| `result_tokens` | The exact emitted `content[0].text` for the exit path taken (lean envelope, probe, full result, served-heavy) — counted directly. On heavy/threshold paths this is the small **probe** actually returned, never the multi-MB cached blob. |
| `total_tokens` | The integer sum of the three counts above. |
| `encoding` | Provenance of the counts: `"cl100k_base"` for real tokenizer counts, `"approx-char4"` for the character-based fallback (see [Encoding and the Optional Dependency](#encoding-and-the-optional-dependency)). |

All four counts are non-negative integers. Counting is per-call and stateless.
The `_usage` block itself is never counted (it is a sibling, not part of
`content`).

> **A note on `schema_tokens` and capped routes.** The count is always taken from
> the schema the caller can already see — the same visibility lens `tools/list`
> uses. On a tier-capped or permission-filtered route this is the reduced schema,
> so `_usage` never discloses the size or shape of a capability the caller is not
> authorized to reach.

---

## Enabling Reporting

Reporting is resolved from three layers, in order of authority. Two are
operator-controlled settings; the third is a per-request transport flag.

### Layer 0 — Global default (setting)

`FRISIAN_MCP_USAGE_REPORTING` — default `False`. This is the baseline for the
whole gateway. Left at its default, the feature ships off for every existing
consumer.

Prefer a real Python boolean (`True` / `False`). A **string** value is accepted
too, but it is interpreted as a token, not by Python truthiness: `"on"` / `"true"`
/ `"yes"` / `"1"` enable, and `"off"` / `"false"` / `"no"` / `"0"` — plus any
unrecognized string — resolve to **off**. This is deliberate: a bare string like
`"false"` is Python-truthy, so counting it by truthiness would silently turn
reporting *on* under a common config typo. The global layer parses tokens exactly
the way the per-request flag does, so `"false"` means off here just as it does on
the wire.

### Layer 1 — System policy (setting)

`FRISIAN_MCP_USAGE_REPORTING_POLICY` — one of `"allow"`, `"deny"`, or `None`
(default). This is the operator's authoritative control:

- `"deny"` — forces reporting **off** and **locks it there**. A per-request flag
  can never re-enable a denied system. This is the setting to reach for when
  `_usage` must not appear on a surface at all.
- `"allow"` — turns reporting on by default, while still letting an individual
  request opt out.
- `None` (default) — defers to the per-request flag, then to the global default.

> **The policy must be the exact string `'allow'` or `'deny'`** (case-insensitive,
> surrounding whitespace tolerated). Any other value — a non-string such as
> `b"deny"` or a list, or a dirty string like `"deny"` with a trailing NUL — is
> **not** honored and defers to the lower layers. It does **not** fail safe to
> deny: a deny-intended value that isn't a clean `'allow'`/`'deny'` string
> silently falls through, so a per-request flag could then enable reporting the
> operator meant to forbid. A startup system check, **`frisian_mcp.W014`**, warns
> whenever `FRISIAN_MCP_USAGE_REPORTING_POLICY` is set to such an unhonored value,
> so the misconfiguration is loud at boot rather than silent at request time. Fix
> the value (or unset it) to clear the warning.

### Layer 2 — Per-request flag (transport)

A caller expresses a per-call preference at the **transport** level — not as a
tool argument — via either:

- the request header **`X-Frisian-MCP-Usage`**, or
- the query parameter **`usage`** (for example `?usage=on`).

Recognized values are case-insensitive: `on` / `1` / `true` / `yes` enable;
`off` / `0` / `false` / `no` disable; anything else (including an empty or
malformed value) is treated as **unset** and can never silently enable the
feature. **The header wins over the query parameter** when both are present.

Keeping the flag at the transport layer — rather than adding it to a tool's
`arguments` — means the dispatcher `inputSchema` is byte-identical whether or not
the feature exists. There is no `tools/list` change, no schema version bump, and
an off response is unchanged.

> **Enabling it from a connector.** Because the flag is transport-level, the MCP
> **client/connector must send it** — as the `X-Frisian-MCP-Usage` header, or a
> `?usage=on` query parameter on the endpoint URL. A model driving the connection
> **cannot** turn reporting on from inside a tool call: putting `usage` in a tool's
> `arguments` (for example `params: {usage: true}`) is treated as a queryset filter
> and rejected with `422 Unknown filter field`, not as an opt-in. See
> [Connect an Agent → Enabling token-usage reporting](connect-agent.md#enabling-token-usage-reporting)
> for per-client header/query setup.

---

## Precedence: the Authoritative-Deny Rule

The three layers resolve to a single on/off decision per call. **System `deny` is
authoritative and is checked first** — a request flag can never re-enable a denied
system. After that, an explicit request flag wins; then `allow`; then the global
default:

```text
if system policy == "deny":   OFF        # authoritative — request cannot re-enable
if request flag is set:       request flag (on/off)
if system policy == "allow":  ON
otherwise:                    global default (ships OFF)
```

The full truth table (system policy × request flag) is the shared source of truth:

| System \ Request | *unset* | *on* | *off* |
|---|---|---|---|
| **deny** | OFF | **OFF** (deny wins) | OFF |
| **allow** | ON | ON | OFF (opts out) |
| **unset** | global default (ships OFF) | ON | OFF |

The load-bearing cells: the default of unset/unset is **OFF**; `deny` × `on` is
**OFF** (the bypass that must not exist); `allow` × `off` is **OFF** (a caller can
always opt out); and unset × `on` is **ON** (a caller can opt in when the operator
has not taken a position).

---

## Model-Visible Usage: the In-Content Line

The `_usage` block above is a **sibling** of `content` — it lives beside the tool
payload, not inside it. That is exactly right for the operator/harness use case:
the gateway (or the code driving the MCP call) reads `result._usage` to measure
what a call cost. But a model driving a standard MCP client typically receives
only the `content` array (and sometimes `_meta`), so **the agent itself never sees
the sibling `_usage`**. When the goal is for the *agent to read and report its own
cost*, the sibling alone does not serve it.

The **in-content usage line** closes that gap. It is a separate, opt-in surface
that — when enabled — appends the same usage numbers as an extra `content` item
the model can read. It changes *where* usage is surfaced; it never changes
*whether* usage is computed.

### Enabling the in-content line

This is its own opt-in, on top of (and subordinate to) the master reporting gate:

- **Setting** `FRISIAN_MCP_USAGE_IN_CONTENT` — a boolean, default `False`. (Like
  the master setting, a string value is parsed as a token, so a config-confused
  `"false"` resolves to off rather than silently enabling.)
- **Per-request** header **`X-Frisian-MCP-Usage-Content`** or query parameter
  **`usage_content`** (for example `?usage=on&usage_content=on`). Same tri-state
  token grammar as the master flag; **the header wins over the query parameter**.

Crucially, this surface has **no `allow`/`deny` policy of its own**. It can only
choose where usage appears *within an already-enabled master decision* — it can
never turn usage on. The master `resolve_usage_reporting` gate is consulted first
and short-circuits on system `deny`, so **`deny` (or any OFF) suppresses both the
sibling and the in-content line**; no `usage_content=on` can resurrect a denied
or disabled system.

### Interaction matrix (master × content-surface)

| Master resolved | Content surface | sibling `_usage` | in-content line |
|---|---|---|---|
| OFF / `deny` | (any) | absent | absent |
| ON | OFF (default) | present | absent |
| ON | ON | present | present (identical numbers) |

There is no "line without sibling" state: the line is strictly additive to the
sibling, which remains the canonical caller-side record.

### The emitted shape

The line is appended as a **second `content` item** — `content[0]` (the tool
payload the agent parses) is never touched:

```json
{
  "content": [
    {"type": "text", "text": "{ ...tool result or lean envelope... }"},
    {"type": "text", "text": "_usage: {\"schema_tokens\": 15, \"request_tokens\": 3, \"result_tokens\": 10, \"total_tokens\": 28, \"encoding\": \"cl100k_base\"}"}
  ],
  "isError": false,
  "_usage": {"schema_tokens": 15, "request_tokens": 3, "result_tokens": 10, "total_tokens": 28, "encoding": "cl100k_base"}
}
```

The line is **labeled JSON**: the literal prefix `_usage:` (with a trailing space)
followed by the same five-key block as the sibling, serialized with the same
`json.dumps` boundary. The
prefix disambiguates it when a client concatenates text blocks, and the JSON body
is machine-parseable so an agent can reliably extract the numbers to self-report.
The numbers are identical to the sibling — it is one computed block feeding both
surfaces, never a second tokenization.

> **`result_tokens` still measures `content[0]` only.** The appended line lives at
> `content[-1]` and is **not** counted — just like the sibling, its own tokens are
> not part of `result_tokens`. So the on-the-wire invariant holds byte-for-byte: a
> re-tokenize of `content[0].text` equals the reported `result_tokens`. The line's
> own tokens are a small, bounded, deliberate non-count, not a discrepancy.

### Why in-content now, and the forward path to `_meta`

The MCP spec's `_meta` field is the more spec-aligned home for machine-readable,
non-payload metadata like this. It is deliberately **not** used here yet: whether
a client forwards `_meta` through to the model is not a settled, guaranteed
behavior across clients today, so an agent cannot rely on receiving it. The
in-content line is the **reliable** way to put usage in front of the model right
now.

This is a forward note, not a commitment: when `_meta` forwarding becomes
dependable, adding a `_meta` emission is a small, additive change — a new opt-in
surface alongside the sibling and the line, breaking neither. It would be a
no-contract-change / `+x.x.1` patch, not a breaking revision.

---

## Encoding and the Optional Dependency

Counts are produced with the pinned tiktoken **`cl100k_base`** encoding — the same
encoding used to reason about agent context budgets elsewhere in the package.

tiktoken is an **optional dependency**, installed via the `frisian-mcp[usage]`
extra:

```bash
pip install 'frisian-mcp[usage]'
```

The feature never hard-fails and never 500s a response. When tiktoken is not
installed — or its encoding cannot load, for example because there is no network
to fetch the BPE ranks on first use — counting falls back to a deterministic
character-based approximation, `ceil(len(text) / 4)`, and the `encoding` field
reports **`"approx-char4"`** instead of `"cl100k_base"`.

The guarantee extends beyond a missing tokenizer: the entire enabled path is
fail-safe. If building or serializing the usage block ever raises, the result is
returned **unchanged** — no `_usage` sibling, no partial in-content line — and a
warning is logged. The block is fully computed before the response is touched, so
no half-applied usage state is ever emitted. An opt-in observability block can
never turn a good `tools/call` into an error.

That provenance field is deliberate: it lets an operator tell the difference
between *reporting is off* (no `_usage` block at all) and *reporting is on but the
tokenizer is approximate* (`_usage` present, `encoding: "approx-char4"`). The
block never silently disappears because a dependency is missing.

> **For accurate, stable counts in production**, install the `[usage]` extra and,
> in network-restricted environments, pre-cache the `cl100k_base` ranks (via
> `TIKTOKEN_CACHE_DIR`) so the encoding loads offline. Without the extra, `_usage`
> still reports — it simply reports the approximation.

---

## Boundaries

Token usage reporting is intentionally narrow. It is **not**:

- **A stored usage record.** No usage *data* is written to a database, cache, or
  log by the feature — the counts are never stored, aggregated, or emitted to a log
  stream. Each `_usage` block is computed for one call and returned inline. (The one
  thing the feature can write to a log is an operational **failure** warning: on the
  fail-safe path, if building or serializing the block ever raises, a warning is
  logged and the response is returned unchanged. That is an error signal about the
  reporting machinery — never a record of any usage numbers.)
- **A ledger or aggregation.** There is no cross-call sum, no per-agent running
  total, no session accounting. Counts are per-call only.
- **Billing.** The numbers are an observability signal, not a metering or
  chargeback record.
- **An error-path feature (v1).** Only successful results (`isError: false`)
  carry `_usage`. Error results do not, by design.

When reporting resolves off — the default, and unconditionally under a system
`deny` — no counting is performed and the result is returned untouched, so there
is no measurable cost on the default path.

---

## Reading the Numbers

A quick way to sanity-check a deployment: enable reporting for a single call with
the query parameter and inspect the block.

- `schema_tokens` reflects the tool surface the caller was given — for a
  dispatcher, this is the small constant group schema, which is the whole point of
  the dispatcher pattern. A large `schema_tokens` on a supposedly-collapsed
  surface is a signal the grouping is not doing its job.
- `request_tokens` is what the agent spent phrasing the call.
- `result_tokens` is what the response cost the agent's context. On a `@mcp_heavy`
  or auto-negotiated path, a small `result_tokens` next to a large underlying
  dataset is the read-filtering working as intended; on a `@mcp_light` write, a
  small `result_tokens` is the lean envelope doing its job.

Read alongside [The Token Problem at MCP Scale](the-token-problem.md), the
`_usage` block turns the package's context-saving claims into a number you can
watch per call.

---

*Document maintained alongside the frisian-mcp source. The `_usage` contract —
placement, field semantics, and the precedence truth table — is the source of
truth for the feature's behavior.*
