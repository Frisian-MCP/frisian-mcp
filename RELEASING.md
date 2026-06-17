# Releasing frisian-mcp

The release pipeline is in `.github/workflows/release.yml`.  It is **tag-driven**:
push a tag, the workflow takes over.  No manual upload, no API tokens, no
`twine` from a laptop.

## How tags map to publishes

| Tag pattern        | TestPyPI       | Smoke install + import | PyPI (prod) |
|--------------------|----------------|------------------------|-------------|
| `v1.0.11-rc.1`     | yes            | yes                    | **no** — stops here |
| `v1.0.11`          | yes            | yes                    | yes (environment-gated) |

The pre-release detection uses PEP 440 (`packaging.version.Version.is_prerelease`),
so any of `1.0.11rc1` / `1.0.11-rc.1` / `1.0.11a1` / `1.0.11b2` / `1.0.11.dev0`
will route to TestPyPI only.  Anything else (e.g. `1.0.11`, `1.1.0`) gets the
full pipeline.

## Authentication: Trusted Publishers (OIDC)

No long-lived API tokens are stored anywhere.  Authentication is OIDC via
**PyPI Trusted Publishers**, set up once per index.  The GitHub Actions
runner exchanges a short-lived OIDC token with PyPI on each publish; PyPI
validates it against the trusted-publisher config below.

### One-time setup on TestPyPI

1. Log in at <https://test.pypi.org>.
2. **Account settings → Publishing → Add a new pending publisher** (or, if
   the project already exists, **Your projects → frisian-mcp → Publishing**).
3. Fill in:

   | Field             | Value                                  |
   |-------------------|----------------------------------------|
   | PyPI project name | `frisian-mcp`                          |
   | Owner             | `Frisian-MCP` (GitHub org, verbatim)   |
   | Repository name   | `frisian-mcp` (just the repo)          |
   | Workflow filename | `release.yml`                          |
   | Environment name  | `testpypi`                             |

4. Save.

### One-time setup on PyPI (production)

Same form at <https://pypi.org>, identical fields **except** Environment
name → `pypi`.

### GitHub repository environments

In **GitHub → Settings → Environments**, create two environments matching
the names you registered with PyPI:

- `testpypi` — no protection rules required.  This is the default test
  channel; every tag goes here.
- `pypi` — **add Required reviewers** (set yourself).  This adds a human
  click-through gate before the production publish runs.  Even though the
  workflow already gates on "not a pre-release," this is an explicit
  second pair of eyes specifically on the prod side.

## Cutting a release candidate

```bash
# 1. Pick the version.  Use PEP 440 canonical form in pyproject.toml — no
#    dashes inside the rc segment.
sed -i '' 's/^version = .*/version = "1.0.12rc1"/' pyproject.toml
sed -i '' 's/^__version__ = .*/__version__ = "1.0.12rc1"/' src/frisian_mcp/__init__.py

# 2. Commit + tag.  The tag itself can be the prettier dash form — the
#    workflow normalises both to PEP 440 before comparing or installing.
git commit -am "Release v1.0.12rc1"
git tag v1.0.12-rc.1   # or v1.0.12rc1, both work
git push origin main --tags
```

The workflow runs through `build → testpypi-publish → testpypi-smoke` and
stops.  Verify the project page on test.pypi.org and confirm the smoke job
imported the wheel successfully.

## Cutting a production release

Same flow, no `rc` segment in either pyproject or tag:

```bash
sed -i '' 's/^version = .*/version = "1.0.12"/' pyproject.toml
sed -i '' 's/^__version__ = .*/__version__ = "1.0.12"/' src/frisian_mcp/__init__.py

git commit -am "Release v1.0.12"
git tag v1.0.12
git push origin main --tags
```

The pipeline runs `build → testpypi-publish → testpypi-smoke`, then **pauses
on the `pypi` environment gate** waiting for your manual approval in the
Actions UI.  Click approve → the prod publish runs.

## When the smoke step fails

The `testpypi-smoke` job installs the freshly-uploaded wheel from TestPyPI
and imports it.  Failures here typically mean one of:

- **Missing files in the wheel** — usually a `MANIFEST.in` / `pyproject.toml`
  `[tool.setuptools.packages.find]` mismatch.  Fix locally, bump the rc
  number (`rc1` → `rc2` — you cannot reuse the same version on TestPyPI),
  re-tag.
- **Version mismatch** — `frisian_mcp.__version__` does not equal the tag.
  Indicates pyproject and `src/frisian_mcp/__init__.py` drifted; the
  smoke job catches this before prod ever runs.
- **TestPyPI not yet indexed** — the workflow sleeps 60 s before installing.
  If the smoke still fails on first install, bump the sleep or re-run the
  workflow.

In all cases the prod publish does NOT run, because `pypi-publish` depends
on `testpypi-smoke` succeeding.

## Tag protection (recommended)

In **GitHub → Settings → Tags**, add a tag protection rule for `v*` that
restricts who can push tags.  Optional but worth it — once a tag is pushed,
the publish runs immediately, so you want intentional tag pushers only.

## What to do if something goes wrong after a publish

- **TestPyPI**: you can delete a release on test.pypi.org and re-upload
  with the same version.  TestPyPI explicitly allows this; PyPI does not.
- **PyPI**: you **cannot** re-upload the same version.  Yank (`pip
  install` won't pick the yanked version unless explicitly pinned) and
  release a patch.  Plan accordingly — the prod gate exists for this
  reason.
