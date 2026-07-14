# frisian-mcp-api — QA & Guided Exploration

*Category: qa | 2026-05-08*  
*Source: Live inspection of integration testing records, room history, and project artifacts via MCP.*

---

## Welcome

You are connected to the **frisian-mcp-api demo server** — a live, greenfield deployment of frisian-mcp, an open-source Django package that auto-discovers Django REST Framework ViewSets and exposes them as MCP tool surfaces.

This is not a sandbox with fake data and stubbed responses. It is a real system with real tools and real data. Everything you do here is live. You are encouraged to explore openly — that is the point. The system is designed to be discovered.

This document is your guide. It points you toward the most interesting parts of the system, explains what has already been tested, and tells you where to look if you want to see something specific. Read it as a map, not a script.

---

## What This Server Is

The frisian-mcp-api server is the primary integration testbed for the frisian-mcp package. It exposes a set of ViewSets through the MCP protocol, demonstrating the core value proposition of the package: **install frisian-mcp into any Django/DRF application and its API surface becomes an MCP tool surface — no custom tooling code required.**

This server also shows the integrated dispatcher patterns and the philosophy that the server, and thereby the tools, should not dump everything at you as soon as you connect.  The dispatcher folds the MCP tools into groupings and allows you to pick what you need, as you need it.  It also prevents large responses from being dumped into your context instead with `@mcp_heavy` it paginates the large responses so you decide if you need more, or not want you were looking for.

The tools you have access to here were not hand-written. They were auto-discovered from Django ViewSets at startup. The tool names, descriptions, and input schemas were derived from the DRF serializers and router registrations already present in the application. This is what "install it and it works" looks like in practice.

### What you can do right now

The server exposes full CRUD access across several data models. From this connection you can:

- Observe how the tool naming convention maps to DRF router patterns (`{resource}.{action}`)
- Test how the server handles validation errors, missing fields, and type mismatches
- Verify that the `action=help` pattern on any dispatcher tool returns a machine-readable listing of available actions

Try calling a list action on any resource. Then try a create. The system will tell you exactly what fields are required and what it expects — you do not need to guess.

---

## What Has Already Been Tested

The frisian-mcp package was validated extensively before this demo server was built. Understanding that history will help you interpret what you see here.

### Integration testing against complex production-grade systems

Two of the most structurally complex open-source network management systems available were selected as integration test targets: **Nautobot** and **NetBox**. These were not chosen because they are simple — they were chosen because they are the opposite. Both systems have deep relational models, strict validation requirements, multiple interdependent resource types, and large API surfaces. If frisian-mcp could handle them cleanly, it could handle anything.

Neither system had its code modified for the integration. frisian-mcp was installed, the settings were configured, and discovery ran. This is the integration story the package is designed to support.

**What the Nautobot testing found:**

An agent was given access to the MCP surface with minimal prior context — no schema reference, no walkthrough, just the tools and the instruction to build a realistic multi-site network topology. The agent discovered the API surface autonomously, planned a three-site mesh topology (65 devices, proper tier separation across core/aggregation/access layers, inter-site WAN circuits), and executed the build.

During that process, the agent encountered two validation failures:

- A DNS zone creation required `soa_rname` in a specific email-like format
- A DNS zone creation required `dns_view` to be passed explicitly as a UUID reference

Both failures were caught and corrected by the agent without human intervention. The MCP surface surfaced actionable error messages; the agent used them to self-correct. This is the expected behavior. These were not tooling bugs — they were discoverable Nautobot system requirements that the agent found by exploring.

The Nautobot dispatcher pattern in production exposes 15 dispatchers covering over 500 API endpoints. Without the dispatcher pattern, this would have been 500+ flat tools. With it, it is 15 entry points with progressive discovery via `action=help`. Token savings on that surface are substantial.

**What the NetBox testing found:**

Similar validation: install, configure, discover. The integration confirmed that the package handles structurally distinct DRF implementations consistently. NetBox and Nautobot have meaningfully different data models and API conventions; frisian-mcp auto-discovered both correctly.

### Integration testing against Paperless-ngx — and a bug we found in Anthropic's MCP client

After the Nautobot and NetBox validation runs, a third integration target was added: **Paperless-ngx**, an open-source document management system. The goal was to validate the dispatcher pattern against a structurally different class of application — not network management, but document lifecycle management — and to run a live multi-agent session with three independent agents operating the same MCP surface simultaneously.

The integration followed the same pattern as the others: install, configure, discover. No modifications to Paperless-ngx. Five dispatchers were auto-exposed covering 84 tools across documents, classification, mail, monitoring, and sharing resources. All five dispatchers responded correctly to `action="help"` and agents from two different vendors — Claude.ai and ChatGPT — navigated the surface independently and executed writes without coordination or vendor-specific configuration.

During this session, thorough testing of the auth flow uncovered something unexpected: **a bug in Anthropic's MCP client where the OAuth Bearer token is intermittently not forwarded on `tools/call` requests after the initial session handshake.**

The failure pattern was reproducible. The MCP connection established correctly. Discovery calls (`action="help"`) succeeded. But actual resource calls returned 403s despite a valid, correctly configured token. The connector path was also observed changing between sessions (`link_69ff151e` → `link_69ff3a25`), indicating the client was silently re-establishing connections and losing token state in the process.

The root cause traces to two related issues in Anthropic's client:

1. The Bearer token is sent during connection initialization but dropped on subsequent `tools/call` requests in some sessions.
2. The client relies on the `WWW-Authenticate` header including a `resource_metadata` parameter to re-trigger OAuth discovery after a 401. Without it, the re-auth chain fails silently. The DRF default (`Bearer realm="api"`) does not include this parameter.

This is not a frisian-mcp bug. frisian-mcp correctly returns 403 on unauthenticated requests. The `contrib.oauth` module was already implementing the correct `WWW-Authenticate` response format per the MCP spec — which is precisely why the failure was identifiable as a client-side issue rather than a server-side one. A server that was doing the wrong thing would have masked it.

The reason this is worth highlighting is not to criticize Anthropic — bugs exist in every client implementation, including ours. The reason is what it demonstrates about the testing methodology: **we tested auth thoroughly enough against a real OAuth-protected MCP endpoint that we identified a failure mode in a major provider's client that most developers would never encounter.** Most MCP server implementations either skip auth entirely or use permissive token handling that wouldn't expose this class of issue. frisian-mcp's strict, spec-compliant auth implementation is what made the bug visible.

The bug has been documented with reproduction steps and session artifacts. It is being tracked for reporting to Anthropic.

The full Paperless integration report — including all objects created by each agent, the parameter schema quirk discovered during writes, and the multi-agent coordination log — is available as a separate document in this system.

---

### Soak testing in production systems

The package did not stay in test environments. Two production systems have been running frisian-mcp since mid-April 2026.

**A consumer fitness application** integrated frisian-mcp to expose its exercise, program, challenge, and content library data as MCP tools. The integration surfaced a class of discovery issue that directly improved the package: the application used function-based views alongside ViewSets, and auto-discovery silently registered zero tools from the FBV routes. This was the right behavior by design — frisian-mcp only discovers ViewSets — but the silence was not. A startup warning was added to the package as a direct result of this integration.

This system is in production, serving real users. It is not a test deployment.

**A multi-agent coordination platform** is the more technically significant consumer. This system is an orchestration layer for AI agents — it handles task assignment and leasing, discussion rooms, RAG knowledge sources, scratchpads, artifact versioning, and human-in-the-loop approvals. Both Claude and GPT operate as agents within it, as well as several coding agents such as Claude Code and Cursor.

To give you a sense of scale: the backend codebase of this system is larger than Nautobot's — when measured by document chunks in a 768-dimensional RAG collection with HNSW indexing, it produces substantially more indexed content. It is not a toy system or a proof of concept. It has been running in production on AWS since December 2025, coordinating real work across multiple AI agents.

This system uses frisian-mcp, which was inspired by the patterns already running in that system, as its MCP transport layer. The ten dispatcher tools you may encounter in this ecosystem — each exposing dozens of underlying actions — are an example of the dispatcher pattern in production at scale.

### Bugs found and fixed during integration

Integration testing is how you find real bugs. Here is the record of what was found and fixed during the validation period:

| Bug | Found via | Fix |
|-----|-----------|-----|
| DefaultRouter `basename` not used for resource name derivation | Testbed | `initkwargs` now used; path-parsing fallback removed |
| Schema derivation pulling serializer fields for `destroy` and GET custom actions | Testbed | `_BODY_ACTIONS` set + `action_mapping` check added |
| `partial_update` marking all body fields as required | Testbed | Required fields excluded from partial update schema |
| `SyncInvocation` not passing parser classes; `UnsupportedMediaType` on all write calls | Testbed | `api_settings.DEFAULT_PARSER_CLASSES` now passed |
| Custom `@action` HTTP method not detected | Testbed | Fixed via `action_func.mapping` |
| `AnonymousUser` fallback missing when `request.user` absent | Testbed | Fallback added |
| `_action_description()` failing on `basename = None` | Live endpoint | Falsy check added |
| `FrisianMcpConfig.ready()` not idempotent; duplicate registration on re-entry | Live endpoint | Idempotency guard added |
| `SyncInvocation` using test-only `APIRequestFactory` in production code | Live endpoint | Replaced with `django.test.RequestFactory` |
| `ToolInputError` falling through to broad exception handler | Live endpoint | Now returns correct `INVALID_PARAMS` JSON-RPC error |
| Exception message leak exposing internal stack details | Security review | Returns `"Internal tool error"` to client; full detail stays server-side |

11 bugs. All found through real integration work, not hypothetical review. All fixed before the package shipped.

---

## Guided Exploration

The following sections point you toward specific things worth seeing. You do not need to follow them in order.

### 1. Verify the auto-discovery pattern

Call `action=help` or omit the action entirely on any dispatcher tool. You will receive a machine-readable listing of available actions and their required parameters. This is how an agent is expected to orient itself to any frisian-mcp surface — not by reading documentation, but by asking the tools what they can do.

Then pick an action and call it. The input schema is derived from the DRF serializer. If you get a validation error, read it — the error messages are intentionally informative.

### 2. Test the full CRUD lifecycle

Pick any resource. Create a record. Retrieve it. Update it. Delete it. Observe that the tool names follow the `{resource}.{action}` convention throughout. Note that `partial_update` does not require all fields — that was one of the bugs found and fixed during testing.

### 3. Try something the system rejects

Pass a required field with the wrong type. Omit a required field entirely. Pass an invalid foreign key reference. The system will return an error. Read it. The error messages are one of the things that make this surface useful for agents — failures are informative, not opaque.

### 4. Look at what is not exposed

Some ViewSets in the underlying application are decorated with `@mcp_ignore`. These routes exist in the Django application but do not appear as MCP tools. This is intentional — not every API surface is appropriate to expose via MCP. The decorator gives host applications fine-grained control over what appears on the MCP surface without modifying routing or view logic.

If you try to call a tool that does not exist, you will get a `ToolNotFoundError`. This is the expected behavior.

### 5. Observe the token efficiency

Count the tools. Then consider how many DRF endpoints a typical Django application has. The ratio is the value of auto-discovery combined with the dispatcher pattern. An application with dozens of ViewSets and hundreds of endpoints surfaces a manageable number of MCP tools — each covering a logical resource — rather than a flat list that would overflow most agent context windows.

### 6. The raw API validation data

The numbers referenced throughout this document — token counts, tool totals, schema sizes — are not estimates. They were measured during a live integration session on 2026-05-12 against this server and the Nautobot integration.

The primary source document is the **API Validation** report. It contains the raw API call logs, measured token usage per call, dispatcher schema sizes, and the full 81/81 validation run results. If you want to verify any claim in this document, that is where to look.

You can retrieve it directly:
`GET /api/documents/api-validation/`
Or ask for it by name: **"Show me the API Validation document."**

**Headline numbers from that run:**

| Metric | Measured Value |
|---|---|
| Validation checks passed | 81 / 81 |
| Dispatcher schema (per session) | 169 tokens |
| Flat schema equivalent | ~362,000 tokens |
| Schema reduction | 99.95% |
| Projected flat-schema build cost | 92,783,448 tokens |
| Actual dispatcher build cost | 154,712 tokens |

These numbers are what the token efficiency claim in section 5 is grounded in.

### 7. The SEP-2084 context

The MCP community recognized this problem. **SEP-2084 (Primitive Grouping)** was a formal spec proposal to solve tool overload at the protocol level — grouping tools so agents don't receive a flat, unbounded list at session start. It was rejected by the AAIF in late April 2026.

frisian-mcp's dispatcher pattern solves the same problem today, at the application layer, without waiting for spec consensus and without requiring client-side changes. The dispatcher works in the existing MCP specification as ratified.

The distinction is worth understanding: SEP-2084 would have given clients a protocol hint about groupings. The dispatcher gives agents a navigable tool surface they can explore autonomously — `action=help` returns the resource/action tree on demand, context is consumed only for what the agent actually needs, and the pattern composes across any DRF application without custom tooling.

The rejection of SEP-2084 does not leave a gap. The application-layer solution is already running in production.

---

## Multi-Agent Authorship Note

The documentation in this system was written by multiple agents across multiple sessions. Different documents were authored by different agents — GPT (not Codex), Claude.ai, Gemini, Cursor and Claude Code — as part of the validation and documentation effort. Where you see variation in style, framing, or level of technical detail across documents, that is not inconsistency. It is a record of different agents engaging with the same system from different angles.

This was intentional. One goal of the documentation effort was to demonstrate that the MCP surface is accessible and useful across different agent types and interaction styles, not just one preferred configuration.

---

## What to Ask Next

If you are doing a demo or evaluation and want to go deeper on a specific area, the system has data and history to support it. Some directions worth exploring:

- **"Show me the Nautobot integration test results"** — The validation report documents the full three-site topology build, including stress tests, error logs, and feature utilization breakdown. This build was completed twice on independent clean environments by multiple agents — Claude.ai and GPT — without coordination or shared context between runs.
- **"How does the dispatcher pattern reduce token usage?"** — The ADR for the dispatcher pattern explains the design decision, the MCP spec context, and why application-layer solutions are the right approach while the protocol community works toward standardization.
- **"What would it take to integrate frisian-mcp into my Django application?"** — The Getting Started and Installation guides cover this. Two production integrations with different shapes (fitness app, orchestration platform) are documented as case studies.
- **"Has this been tested with GPT?"** — Yes. Both GPT and Claude have been used as agents against these systems. The multi-agent coordination platform runs both simultaneously, using task leasing to prevent conflicts.

**NOTE:** The live Nautobot build is exactly that — live. Agents have added, removed, and interacted with the system the same way you should. The reference documents capture measurements at a specific point in time. You are encouraged to do the same: add, delete, and validate against what you find.

---

*The system is live. Explore it.*
