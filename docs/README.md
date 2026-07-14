# frisian-mcp documentation

**Category:** index
**Slug:** docs-home

The user and operator documentation is **versioned by directory**. Each release
that changes documented behavior gets its own frozen directory, so a reader can
always find the docs that match the version they run.

## Start here

- **[v1.1 documentation](v1.1/)** — current. The per-route permission model,
  the `FRISIAN_MCP_ROUTES` config surface, tiers / allow-deny / absence, plus
  everything carried forward from v1.0.
- **[v1.0 documentation](v1.0/)** — frozen. The docs as they described v1.0.x
  behavior, kept as a provenance snapshot.

## Cross-version material (not split by version)

- **[Architecture Decision Records](ADR/)** — design records. An ADR is written
  once, for the release it lands in, and is not duplicated per version. Each ADR
  links to the doc set current at its authoring (ADR-009 → v1.0, ADR-010 → v1.1).
- **[Changelog](Changelog/)** — spans every release by nature.
- **[installs/](installs/)** — host-specific integration material (per-host
  configuration references). Host integration guides are maintained per host,
  not duplicated per package version.

## The versioning convention (for the next release)

When a release changes documented behavior:

1. **Freeze** the current version's user/operator docs into `docs/vX.Y/` — a
   verbatim snapshot, left unchanged from then on.
2. **Create** `docs/vX.(Y+1)/` as the new working set: copy the frozen tree,
   then add and update the docs for what changed.
3. **Leave ADRs, the Changelog, and `installs/` where they are** — they are
   cross-version and are not split.
4. **Point the current entry points at the new version** — this index, and the
   repository `README.md`, link to the newest `docs/vX.Y/` set; older versions
   stay reachable from this page.

The per-version split covers **user and operator docs** (getting started,
guides, configuration reference, security, troubleshooting). It does **not**
cover the ADR log, which is the cross-version design record.

## Scope note

The repository `README.md` stays setup-focused; the deep per-version material
lives here under `docs/`. Documentation examples use neutral, host-agnostic
names — a specific host is named only inside its own `installs/` folder.
