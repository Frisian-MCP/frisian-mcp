"""Enforced package-neutrality guard (PR-16, project 31fb5184).

`frisian-mcp` is a *generic* Django MCP gateway.  Host-application vendor
schema — app labels, plugin resource names, product names — must never appear
in ``src/`` or ``tests/`` as **identifiers or fixture data**.  The grammar
knows exactly three things: ``group``, ``resource``, ``action``; encoding a
particular deployment's surface (``dcim``, ``ipam``, ``dns:arecord``, a product
name) into the package's own code or test data is the host-app coupling the
neutrality rule exists to strip out.

What is allowed, and why this guard tokenizes instead of grepping:

* **``#`` comments may name a host app for provenance** — e.g. explaining that
  a real-world serializer shape came from a specific deployment.  ``tokenize``
  lets us skip ``COMMENT`` tokens so that useful provenance survives while
  ``NAME`` (identifier) and ``STRING`` (literal / docstring) tokens are still
  checked.  A regex sweep cannot draw that line and would delete the commentary
  the rule deliberately keeps.

* **Legacy files predating the rule are grandfathered** in
  :data:`_LEGACY_ALLOWLIST`.  This is tech debt, not a blessing: the list is a
  burn-down, not a place to add new entries.  A new file that trips this guard
  is a real finding — rename the fixtures to neutral shape
  (``catalog`` / ``item`` / ``order`` / ``ping``), do not add it here.

See ADR-010 and the PM neutrality ruling (room 8cb8cc7b, 2026-07-09).
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

# Vendor tokens that must not appear as identifiers or string data.  Bounded by
# non-letters (so ``dcim_device`` and ``"dcim.view_device"`` match, but a longer
# benign word does not).  Deliberately excludes bare ``dns`` — too generic to
# ban without false-positives on legitimate protocol handling.
_VENDOR_TOKENS = (
    "nautobot",
    "netbox",
    "paperless",
    "openedx",
    "dcim",
    "ipam",
    "arecord",
)
_VENDOR_RE = re.compile(
    r"(?<![a-z])(" + "|".join(_VENDOR_TOKENS) + r")(?![a-z])",
    re.IGNORECASE,
)

# Files that contained vendor identifiers/fixture data before the guard existed.
# BURN-DOWN LIST — do not add to it.  Paths are relative to the repo root.
_LEGACY_ALLOWLIST = frozenset(
    {
        "tests/test_permission_aware_discovery.py",
        "tests/test_write_path_filtering.py",
        "tests/test_group_dispatcher.py",
        "tests/test_fk_m2m_schemas.py",
        "tests/test_contrib_oauth.py",
        "tests/test_invocation_host_permissions.py",
        "tests/test_tool_hints.py",
    }
)

_THIS_FILE = "tests/test_package_neutrality.py"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = ("src", "tests")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        files.extend((_REPO_ROOT / root).rglob("*.py"))
    return files


def _offending_tokens(path: Path) -> list[tuple[int, str, str]]:
    """Return (lineno, token-kind, matched-text) for vendor hits outside comments."""
    hits: list[tuple[int, str, str]] = []
    source = path.read_text(encoding="utf-8")
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                continue  # provenance comments are allowed
            if tok.type not in (tokenize.NAME, tokenize.STRING):
                continue
            match = _VENDOR_RE.search(tok.string)
            if match:
                kind = "identifier" if tok.type == tokenize.NAME else "string"
                hits.append((tok.start[0], kind, match.group(0)))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        # Fail loud: the scan covers files pytest never imports, so an
        # untokenizable file is not guaranteed to surface anywhere else.
        raise AssertionError(f"Could not tokenize {path}") from exc
    return hits


def test_no_vendor_names_in_package() -> None:
    """No host-app vendor token appears as an identifier or string outside comments."""
    violations: list[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _LEGACY_ALLOWLIST or rel == _THIS_FILE:
            continue
        for lineno, kind, text in _offending_tokens(path):
            violations.append(f"{rel}:{lineno}: vendor token {text!r} in {kind}")

    assert not violations, (
        "Host-app vendor names must not appear as identifiers or fixture data in "
        "src/ or tests/ (PR-16 neutrality rule). Rename to neutral shape "
        "(catalog/item/order/ping). Provenance in a '#' comment is allowed.\n"
        + "\n".join(sorted(violations))
    )


def test_legacy_allowlist_has_no_dead_entries() -> None:
    """Every grandfathered file still exists and still trips the guard.

    Keeps the burn-down honest: when a legacy file is cleaned up, its allowlist
    entry must be removed in the same change, or this fails.
    """
    stale: list[str] = []
    for rel in _LEGACY_ALLOWLIST:
        path = _REPO_ROOT / rel
        if not path.exists():
            stale.append(f"{rel}: allowlisted but missing")
        elif not _offending_tokens(path):
            stale.append(f"{rel}: allowlisted but now clean — remove from _LEGACY_ALLOWLIST")
    assert not stale, "Stale neutrality allowlist entries:\n" + "\n".join(sorted(stale))
