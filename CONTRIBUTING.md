# Contributing to frisian-mcp

**Category:** guide  
**Slug:** contributing  
**Audience:** Developers contributing to the frisian-mcp package

---

## Overview

frisian-mcp welcomes contributions. This guide covers the fork-based workflow, code quality standards, and the PR process. Following these steps keeps the review cycle short and the history clean.

---

## Fork Workflow

### 1. Fork the repository

Fork `Frisian-MCP/frisian-mcp` on GitHub. Clone your fork locally:

```bash
git clone https://github.com/<your-username>/frisian-mcp.git
cd frisian-mcp
```

Add the upstream remote so you can pull in future changes:

```bash
git remote add upstream https://github.com/Frisian-MCP/frisian-mcp.git
```

### 2. Create a feature branch

Never commit directly to `main`. Create a branch from the latest upstream `main`:

```bash
git fetch upstream
git checkout -b feat/your-feature-name upstream/main
```

Branch naming conventions:

| Prefix | Use case |
|--------|----------|
| `feat/` | New feature or capability |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `refactor/` | Code restructure, no behavior change |
| `chore/` | Tooling, deps, CI |

### 3. Make your changes

Keep commits small and focused. Each commit should represent one logical change. Write commit messages in the imperative mood: `Add dispatcher caching layer`, not `Added` or `Adds`.

### 4. Sync with upstream before submitting

Before opening a PR, rebase onto the latest upstream `main` to reduce merge conflicts for reviewers:

```bash
git fetch upstream
git rebase upstream/main
```

Resolve any conflicts, then push:

```bash
git push origin feat/your-feature-name
```

### 5. Open a pull request

Open the PR against `Frisian-MCP/frisian-mcp` `main`. Fill in the PR template (see [PR Checklist](#pr-checklist) below).

---

## Development Setup

### Install with dev dependencies

```bash
pip install -e ".[dev]"
```

This installs the package in editable mode along with all linting and testing dependencies.

### Pre-commit hooks

frisian-mcp uses [pre-commit](https://pre-commit.com/) to run linting checks automatically before every commit.

Install the hooks once after cloning:

```bash
pip install pre-commit
pre-commit install
```

Run all hooks manually against the full codebase:

```bash
pre-commit run --all-files
```

The hooks run mypy and pylint on every staged Python file. A commit is rejected if either tool reports errors. Fix all errors before committing — do not use `--no-verify`.

---

## Linting Standards

### mypy — static type checking

frisian-mcp enforces strict type annotations throughout the codebase. All public functions and methods require type annotations. The mypy configuration is in `pyproject.toml` under `[tool.mypy]`.

Run mypy directly:

```bash
mypy src/frisian_mcp/
```

Key rules:

- All function parameters and return values must be annotated
- `Any` is permitted only where a third-party library forces it — document the reason with a comment
- `Optional[X]` is equivalent to `X | None`; prefer the union syntax on Python 3.10+
- Use `TYPE_CHECKING` guards for imports that are only needed for annotations

Common fixes:

```python
# Wrong — missing return type
def get_tool_list(self):
    ...

# Correct
def get_tool_list(self) -> list[ToolDefinition]:
    ...
```

```python
# Wrong — implicit Any via untyped dict
def build_response(data):
    ...

# Correct
def build_response(data: dict[str, object]) -> MCPResponse:
    ...
```

### pylint — code quality

pylint is configured in `pyproject.toml` under `[tool.pylint]`. The project targets a score of **10/10**.

Run pylint directly:

```bash
pylint src/frisian_mcp/
```

Enforced rules include:

- No unused imports or variables
- No shadowed builtins
- Maximum function length: 50 lines (refactor into helpers beyond this)
- Maximum file length: 400 lines
- Docstrings required on all public classes and functions (one-line summaries are fine)
- No broad `except Exception` without re-raising or logging

Disable a rule inline only when the library genuinely requires it, and always include the reason:

```python
# pylint: disable=protected-access  # testing internal cache eviction
result = dispatcher._cache._store
```

---

## Pre-commit Hook Configuration

The `.pre-commit-config.yaml` at the repo root configures all hooks. The relevant hooks in order:

1. **trailing-whitespace** — removes trailing whitespace
2. **end-of-file-fixer** — ensures files end with a newline
3. **check-yaml** — validates YAML syntax
4. **mypy** — type checking via `mypy src/frisian_mcp/`
5. **pylint** — code quality via `pylint src/frisian_mcp/`

All hooks must pass before the commit is accepted. If a hook auto-fixes a file (trailing whitespace, newlines), stage the fix and commit again:

```bash
git add -u
git commit
```

---

## Testing

Run the test suite before opening a PR:

```bash
pytest
```

New features require accompanying tests. Bug fixes require a regression test that would have caught the bug. Tests live under `tests/` and mirror the package structure.

Coverage is measured but not gated at a hard threshold. Aim to keep coverage above 85% on changed files.

---

## Versioning

frisian-mcp follows a semantic-ish scheme. Apply the bump that matches the change **in the same PR** — reviewers check for it. The rules are ordered by precedence; apply the **first** that matches:

1. **New feature → minor bump.** For example, the per-route permission work was the `1.0.12 → 1.1.0` minor; subsequent `src/` fixes on top of it then advanced the patch level (the current release is `1.1.1`). A feature that also touches `src/` still takes the minor bump — the feature rule wins, and you do **not** additionally apply the patch rule.
2. **Any *non-feature* change that touches `src/`** (bug fix, refactor, performance) **→ patch bump of `+0.0.1`** (`1.1.0 → 1.1.1 → 1.1.2 …`), required even for a one-line fix.
3. **A change that does not touch `src/` does not bump.** Changes confined to `tests/`, `docs/`, or tooling/config leave the version untouched.

The version has two **authoritative** sources, which must stay in sync:

- `pyproject.toml` — `version = "…"`
- `src/frisian_mcp/__init__.py` — `__version__ = "…"`

Update both in the same commit; a mismatch between them is a release bug. `README.md` also displays the version as an **informational copy** (the badge line near the top) — update it to match when you bump, but it is not one of the authoritative sources.

> **Structured release notes are planned, not yet set up.** Per-change release notes — the workflow NTC/Nautobot drives with [towncrier](https://towncrier.readthedocs.io/) — are intended for a future release but are **not** wired up yet. Until then, record user-facing changes under `[Unreleased]` in the changelog (see the PR checklist below). Do not add towncrier as part of an unrelated PR; this note exists so the versioning rule and that future tooling stay consistent.

---

## PR Checklist

Before requesting review, verify:

- [ ] Branch is rebased onto the latest `upstream/main`
- [ ] `pre-commit run --all-files` passes with zero errors
- [ ] `mypy src/frisian_mcp/` reports no new errors
- [ ] `pylint src/frisian_mcp/` score is 10/10
- [ ] `pytest` passes with no failures
- [ ] New public functions/classes have docstrings
- [ ] New features have tests; bug fixes have a regression test
- [ ] If the PR touches `src/`, the version is bumped in **both** `pyproject.toml` and `src/frisian_mcp/__init__.py` (see [Versioning](#versioning))
- [ ] CHANGELOG entry added under `[Unreleased]` if the change is user-facing
- [ ] PR description explains *why* the change is needed, not just *what* it does

---

## Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/) Code of Conduct. Be direct, constructive, and professional in all interactions.

---

*Document maintained alongside the frisian-mcp source.*
