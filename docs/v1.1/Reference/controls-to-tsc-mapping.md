# Controls-to-TSC Mapping

This document maps frisian-mcp features to selected SOC 2 Trust Services
Criteria (TSC). It is a planning and evidence crosswalk for an operator's
control program, not an attestation. frisian-mcp is **SOC 2-aligned** where
configured and operated as described below; an operator remains responsible
for its environment, policies, evidence retention, and assessment.

The mappings identify features that can support a control. They do not mean a
feature alone satisfies a criterion, and they do not replace a risk assessment
or an independent auditor's evaluation.

## Scope and evidence handling

The package runs inside a Django host application. The host's identity
provider, Django/DRF permissions, object-level authorization, deployment
pipeline, log collector, retention policy, and incident process are outside
this package. Collect evidence from both the package and those surrounding
systems.

For every period under review, preserve the deployed configuration, a release
or configuration-change record, relevant `mcp_doctor` output, and logs from
both the host and the configured audit sink. Protect evidence according to its
sensitivity; do not put bearer tokens, request arguments, or personal data in
an evidence bundle unless the operator has separately approved that handling.

## SOC 2 Trust Services Criteria crosswalk

| TSC area | frisian-mcp feature | How the feature supports the control | Example evidence | Operator responsibilities and limits |
| --- | --- | --- | --- | --- |
| **CC6.1 — logical access** | OAuth 2.0 (`contrib.oauth`), static Bearer tokens (`contrib.tokens`), gateway permission classes, and `FRISIAN_MCP_UNAUTHENTICATED_TIER` | Authenticated requests can be associated with an OAuth client/user or token; static tokens have an active state and `last_used_at` value, and the gateway can restrict an unauthenticated surface to a low tier. | Authentication configuration, OAuth client/token administration records (including active state and last use), and successful/denied host authentication logs. | Require authentication where appropriate, protect signing keys and credentials, rotate/revoke credentials, and review access. A randomized route path is defense in depth, not authentication. |
| **CC6.2 / CC6.3 — authorization and least privilege** | Permission tiers, `FRISIAN_MCP_ROUTES`, route allow/deny lists, permission-aware discovery, and host Django/DRF authorization | A route may expose only its configured tool subset and tier ceiling. Discovery can omit tools outside an identity's permissions, while invocations execute through the host authorization stack. | Versioned settings, route-surface audit or `mcp_doctor` output, `tools/list` results for representative least-privilege accounts, and host permission assignments. | Configure a minimum-privilege execution identity and test actual invocation authorization. Discovery filtering is not execution enforcement; host object-level controls remain necessary. |
| **CC7.2 / CC7.3 — monitoring and evaluation of security-relevant events** | Dedicated `frisian_mcp.audit` logger for resolved `tools/call` decisions | The logger emits structured routing context including route, route path, effective tier/ceiling, tool labels, decision, and reason. It deliberately excludes argument values, credentials, and user identity. A sink handler can forward these records for review. | Audit-sink configuration, sample allowed and denied events, log-retention configuration, alert/review records, and time-synchronization evidence from the host/platform. | Attach and operate a durable sink; set retention, access controls, review/alert thresholds, and incident procedures. The package emits records but does not provide a log store, monitoring service, or incident response process. |
| **CC8.1 — change management** | Version-controlled package/configuration, startup checks, `mcp_doctor`, and the per-route surface audit | Configuration validation and the doctor command can detect malformed or unexpectedly empty route surfaces before or after deployment; `--strict` can make route findings fail a CI gate. | Pull requests and approvals, CI results, release/deployment records, configuration diffs, `python manage.py check`, and `python manage.py mcp_doctor --strict` output. | Use an approved change process, segregate production access as needed, test changes, and retain approvals. The package does not approve, deploy, or record operator changes. |
| **Confidentiality — C1.1** | Authenticated/protected routes, route-level absence, tier ceilings, host authorization, HMAC-stored static-token digests, and minimized audit context | These mechanisms can limit access to MCP-exposed data, avoid persisting raw static-token values, and avoid placing tool arguments or credentials in the package's audit-context record. | Route and authentication configuration, permission tests, audit payload samples, and host/platform encryption and key-management records. | Classify confidential data, enforce transport/storage encryption, govern disclosure and retention, and configure host object permissions. frisian-mcp does not classify data or provide storage-lifecycle controls. |

## HIPAA and PCI-DSS documentation crosswalk

This section is a documentation exercise using the same access-control and
logging controls. It does not determine whether a deployment is subject to
HIPAA or PCI-DSS, nor does it establish compliance with either framework.

| Framework requirement | Supporting frisian-mcp capability | Evidence and boundary |
| --- | --- | --- |
| **HIPAA 45 CFR §164.312(b) — audit controls** | The `frisian_mcp.audit` logger records each resolved MCP tool-call decision with routing and authorization context; host application logs can supply the associated identity and resource activity. | Retain sink configuration, representative allow/deny events, host identity/activity logs, access reviews, and retention evidence. Configure the sink and procedures to record and examine activity in systems containing ePHI. The package does not itself retain logs, identify ePHI, or perform review. |
| **PCI-DSS access logging** | Gateway authentication/authorization features and the audit-context logger can support logging and review of MCP access decisions. | Correlate package audit events with host, identity-provider, and infrastructure logs. Configure PCI-DSS-required event fields, retention, review, alerting, time synchronization, and access restrictions in the surrounding environment; the package's intentionally minimal audit payload does not contain every PCI-DSS logging field. |

## Verification checklist

Before relying on a mapping, an operator should at minimum:

1. Require an appropriate authentication method and use non-administrative,
   minimum-privilege service identities.
2. Review each route's allow/deny lists and tier ceiling; run `mcp_doctor` after
   configuration changes (use `--strict` when route findings should gate CI).
3. Demonstrate both denied and allowed tool calls using representative accounts,
   including host object-level authorization where applicable.
4. Attach a protected, durable handler to `frisian_mcp.audit` and verify that
   its events are retained and reviewed with correlated host identity logs.
5. Keep change approvals, test results, deployed configuration, and access/log
   review evidence for the period required by the operator's program.

## Implementation anchors

The feature claims above are grounded in
`src/frisian_mcp/views.py` (`_log_audit_context`),
`src/frisian_mcp/contrib/tokens/models.py` (static-token digest, active state,
and last-use metadata), and
`src/frisian_mcp/management/commands/mcp_doctor.py` (configuration audit).

See [Security-First MCP Architecture](../Security/security.md),
[Permission-Aware Discovery — Security Guidance](../Guide/permission-aware-discovery-security.md),
and [`mcp_doctor`](../Guide/mcp-doctor.md) for configuration and operational
details.
