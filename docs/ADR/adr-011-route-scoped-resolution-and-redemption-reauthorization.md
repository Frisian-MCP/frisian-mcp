# ADR-011: The route surface is authoritative — scoped resolution and redemption re-authorization

**Date:** 2026-08-10
**Category:** adr
**Status:** Accepted
**Supersedes:** —
**Related:** ADR-002 (Dispatcher Pattern), ADR-004 (Write-Path Response Filtering), ADR-005 (Read-Response Filtering), ADR-008 (Permission-Aware Tool Discovery), ADR-010 (Per-Route Permission Model)

---

## Implementation status — read this before citing anything below

This ADR records **one decision**, now shipped in full. The distinction is
stated per-section and kept here because an accepted document that describes
unshipped behaviour as current is the precise defect ADR-005's amended
cache-layer section exists to correct.

| Decision | Status |
|---|---|
| §3 Route-scoped resolution of caller-derived names | **Shipped** (membership gate; verified live, §7.2) |
| §4 Re-authorization at redemption | **Shipped** (§7.4 — synthetic + mutation evidence; live verification deferred, §7.5) |
| §5 Grouped entries retain the server-resolved child | **Shipped** (§7.4) |
| §6 Route identity stays out of the owner key | **Shipped** (by construction — it was never in it) |
| §6 Refusal reuses the existing outcome | **Shipped** for §3 and §4 |

**One caveat travels with §4 and §5:** they are shipped and covered, but their
live verification is an open exception (§7.5), not a completed step.

## 1. Context

ADR-010 established that a route's surface is built by absence: a tool that is
not mounted on a route does not exist for callers of that route. That property
holds for **discovery** and for **direct invocation**, because both consult the
route's own `RouteView`.

Two paths did not consult it.

**Caller-derived names.** A dispatcher composes a target from caller-supplied
`resource` and `action`. Resolving that composed name against the global
registry rather than the route's mounted set would let a caller reach — or
learn the existence of — a tool the route does not expose. ADR-010's boundary
is built at mount time; a resolution step that ignores the mount undoes it.

**Redemption.** A continuation token is minted on one request and redeemed on
another. SEC-3 binds the token to the caller (ADR-005, *Continuation
ownership*). It does not bind it to the route, and the redemption path serves
the cached result without re-consulting any route surface. A token minted while
a tool was mounted therefore remains servable after the mount changes.

These were filed as separate findings. They are **one question**: *what does a
per-route cap actually contain?* Answering them independently risks two
answers to that question, so they are ruled together here.

## 2. Decision, in one sentence

**The route surface is authoritative at every point where a name is resolved or
a cached result is served — and route containment is an authorization check at
use time, not an ownership dimension.**

## 3. Caller-derived names resolve against the route-scoped mounted set

A name composed from caller input MUST be resolved against the mounted set of
the route the request arrived on, never against the global registry.

This closes the existence question **at the resolution step** rather than
downstream of it. A name outside the route's set is not looked up at all, so
there is no retrieved object whose attributes could differ, and no work
performed that a non-member would not also cause. Closing it downstream — by
resolving and then discarding — would leave both.

> **Status: shipped.** `_dispatcher_target_entry` gates on membership and
> returns before `tool_registry.get_entry()`. See §7 for the live evidence and
> §8 for what that ordering buys.

## 4. Redemption re-authorizes against the current route surface

Serving a cached continuation MUST re-evaluate the cached, server-resolved
target against the **current** `RouteView` of the route the redemption arrived
on — not against the route recorded at mint time, and not against the ceiling
alone.

Re-authorization evaluates the whole surface:

- mounted membership
- allow/deny carve-outs
- effective tier ceiling
- applicable capability and permission visibility

**A numeric ceiling comparison is insufficient.** Two routes may share a tier
ceiling and expose entirely different resources, because ADR-010's allow/deny
grammar carves the surface independently of tier. A control that compared
ceilings would look correct, pass a plausible test, and permit exactly the
cross-route service it was built to refuse.

> **Status: shipped.** `_redemption_target_authorized()` in `views.py`,
> evaluated after the SEC-3 owner check and before any mode is served, so
> redemption still short-circuits ahead of re-dispatch. The claim in the
> paragraph above is executable, not rhetorical: a ceiling-only implementation
> is a mutant that the suite kills (§7.4).

## 5. Grouped-call entries retain the server-resolved child target

A cache entry minted from a grouped call MUST record the child target the
server resolved, not only the outer dispatcher name.

Binding only the outer name leaves re-authorization with nothing to evaluate:
the dispatcher is mounted, so a membership check against it always passes,
while the child — the thing whose containment matters — is unrepresented. The
re-check in §4 is vacuous without this.

This is a **cache-entry shape change**. It is additive; entries lacking the
field are treated as unauthorizable and fall to §6's refusal, consistent with
the pre-SEC-3 legacy-entry handling already in the redemption path.

> **Status: shipped.** `_build_heavy_cache_entry()` records `resolved_target`
> at all four mint sites. Note which path mattered most: the
> `AUTO_NEGOTIATE_THRESHOLD` backstop is the *only* mint path currently
> reachable on the dispatcher shapes, so had that one site kept binding the
> outer name, §4 would have been vacuous for every grouped token in practice
> while appearing correct everywhere else.

## 6. Route identity is not an ownership dimension, and refusal is not a new outcome

**Route identity and ceiling MUST NOT enter `_heavy_owner_key()`.** SEC-3
answers *who minted this token*; route containment answers *may it be served
here, now*. Folding the second into the first would invalidate every
outstanding token whenever a route's configuration changed, and the caller
would experience that mass invalidation as ordinary expiry — indistinguishable
from the failure SEC-3 is meant to make legible.

**A route-based refusal returns the existing expired-or-not-found outcome.**
The precise reason is recorded in the audit log and never in the response.

This is not a convenience. Redemption already exposes two distinguishable
client outcomes, which is a token-validity oracle; it is tolerable only because
tokens are 128-bit and unguessable. A third outcome meaning *"valid token,
wrong route"* would disclose **server deploy state** — that this host serves
some other mount where the token would work — to any token holder, including
anonymous callers on open routes. That constraint was settled during 1.1.0 and
is reaffirmed here rather than re-argued.

## 7. Mandatory live verification

**Design-only ADRs re-create the problem this one addresses.** Any change
implementing §4 or §5 must carry live verification, or an explicit exception
with justification, recorded in the amendment that lands it.

### 7.1 Why synthetic tests cannot discharge this

The published schema declares no top-level `resource`. A schema-validating
client therefore **cannot** perform the injection §3 defends against — it nests
the field under `params`, where it is inert. A test built on the published
schema exercises "an extra parameter is ignored" and reports success, having
never reached the code path in question.

An attacker does not validate against the schema. **The vector is reachable and
the test for it is not**, so verification requires a client that ignores the
schema — a raw JSON-RPC caller.

### 7.2 Evidence recorded for §3

Run 2026-08-10 against the deployed demo host with a raw JSON-RPC client,
read-only calls:

| Probe | Result |
|---|---|
| Group dispatcher, own resource | served normally |
| Group dispatcher, **resource belonging to another group** | refused — unknown-tool, 404 |

The refusal confirms §3 in production rather than by inference.

### 7.3 Exception on record

**The class-dispatcher variant of §3 has never been verified live**, across
three projects. It requires a class dispatcher on a connector-reachable route;
the temporary harness that provided one was removed. The behaviour is covered
synthetically only.

This is recorded as an exception rather than omitted, per the rule in §7. It is
the same gap that let the original defect ship, and it remains open.

### 7.4 Evidence recorded for §4 and §5

§4 and §5 landed together, §5 first — it is the precondition, and building §4
against entries that bind only the outer name would have produced a check that
passes unconditionally.

Coverage is in `tests/test_adr011_redemption_reauthorization.py` (the four
surface dimensions, against real `RouteView`s built from a real registry, not
stubs) and `TestAdr011ResolvedTarget` in `tests/test_heavy_negotiation_matrix.py`
(the mint-site wiring, read off what the view actually stored).

**Verified by mutation, because passing tests prove less than killed mutants.**
Three mutants were introduced and the suite re-run:

| Mutant | Result |
|---|---|
| §4 check always authorizes | 6 tests fail |
| §4 checks tier/ceiling but skips the allow/deny carve-out | 3 fail, including the same-ceiling case |
| §5 mint wiring reverted to bind the outer name | 2 fail |

The second mutant is the point: it is precisely the plausible implementation
§4 warns about, and the same-ceiling test exists to kill it.

The third mutant is recorded because **before that test was added it killed
nothing** — the full suite passed with §5's wiring reverted, which would have
left §4 shipped, green, and vacuous. That is the failure mode this ADR is about,
reproduced inside the work that implements it.

### 7.5 Exception on record — §4 and §5 are not live-verified

Live verification of cross-route redemption needs **two mounts exposing
different surfaces** on a connector-reachable host, and a token minted on one
and replayed against the other. The demo host currently serves a single capped
mount, so the scenario cannot be posed without a route-configuration change and
a redeploy — outside the scope of the change that lands this, and not a
decision this seat makes alone.

Recorded as an exception per §7 rather than omitted, and deliberately alongside
§7.3 so the two open live gaps are read together rather than discovered
separately.

**What would discharge it:** a second mount whose allow/deny carve-out excludes
a member the first exposes, then one grouped probe on the permissive mount and
one redemption of that token against the restrictive one. The expected result
is the ordinary expired-or-not-found envelope, with
`continuation_route_not_authorized` in the audit log and nothing on the wire
distinguishing it from expiry.

## 8. Side-channel question

Whether route-scoped resolution leaves a **timing or enumeration channel** that
would undermine absence-as-boundary was raised as a condition of this decision.
Both were probed on 2026-08-10.

**Enumeration: no channel found.** A resource that exists on another mount and
one that exists nowhere produce a byte-identical error template, differing only
in the echoed input. A caller cannot distinguish the two cases.

**Timing: no channel detected**, and §3's ordering is why. Because membership is
checked *before* resolution, both cases fail the same comparison and neither
reaches a registry lookup — there is no work difference to measure. Sampling at
n=20 per class over the public internet found the **within-class spread
exceeding the between-class difference**, i.e. the classes are
indistinguishable at that resolution.

> **Limit of that result, stated deliberately.** It shows no channel detectable
> over a public network at that sample size and jitter. It does not exclude a
> sub-millisecond difference resolvable by a local attacker with many more
> samples. The measurement is *consistent with* the structural reason to expect
> no channel; it is not independent proof of absence. §3's ordering, not the
> measurement, is what makes the property hold.

**Multi-mount population remains open.** Whether a lower-privilege mount can
observe or force registry population that leaks existence across isolation
boundaries is not answered by §3 and overlaps the known uncapped-mount
enumeration behaviour, where tier-gate error shapes differ on routes without a
ceiling. §3 constrains resolution on *capped* routes; uncapped mounts are
deliberately unchanged.

## 9. Consequences

### Positive

- One answer to *what a per-route cap contains*, rather than two that may diverge.
- The existence question is closed at resolution, which also closes the timing
  question structurally rather than by tuning.
- Route configuration changes take effect on outstanding tokens without
  invalidating them, because containment is checked at use rather than bound at
  mint.
- No new client-visible outcome, so absence-as-boundary is preserved.

### Negative / risks

- §4 costs a route-surface evaluation on every redemption, on a path that
  previously short-circuited.
- §5 changes the cache-entry shape. Entries minted before it are unauthorizable
  and refuse under §6 — correct, and visible as a refusal wave across a deploy.
- §4 and §5 ship without live verification. That exception, and what would
  discharge it, is recorded in §7.5 rather than left implicit.
