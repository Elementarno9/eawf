"""Pre-commit hook: spec test-path freshness (C03 layer 2 of 3).

Greps each staged ``.ea/specs/**/*.md`` for ``tests/`` paths and
verifies that every cited test file exists on disk in the working
tree. This is the second of the three-layer enforcement stack:

1. **Pydantic load** — :mod:`eawf.spec.wave` + :mod:`eawf.spec.validators`
   reject empty / non-existent paths at ``model_validate`` and loader
   call sites.
2. **Pre-commit (this script)** — catches the case where the test path
   existed at WaveSpec authoring time but was renamed / deleted between
   authoring and ``git commit`` (Pydantic re-runs only when a fresh
   load happens; staged spec markdown is not auto-revalidated).
3. **Audit DSL** — :mod:`eawf.audit_dsl` ``verify-implements`` kind
   (lands separately under W02) walks closed-wave specs at ship time.

Each layer catches the RC-1 stale-paths failure class on its own
(per the C03 brief §1); the redundancy is intentional.

Usage
-----

Configured under ``.pre-commit-config.yaml`` ``stages: [pre-commit]``.
Pre-commit passes the list of staged paths as argv[1:]. Paths outside
``.ea/specs/`` are ignored. Exit codes:

- ``0`` — every cited test path exists.
- ``1`` — at least one path is missing; diagnostic printed to stderr.

Regex grammar
-------------

The script scans for ``tests/`` followed by any non-whitespace
characters that are not punctuation that would clearly close a quoted
or markdown construct (``)``, ``]``, ``\\``, backtick, single quote,
double quote, ``>``). This deliberately matches both backtick-quoted
paths (```tests/unit/test_x.py```) and bare list items
(``- tests/unit/test_x.py``). Markdown link targets
``[label](tests/unit/test_x.py)`` are also matched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Regex matches a ``tests/`` token followed by one or more characters
# that are not whitespace and not punctuation that would close a quoted
# or markdown construct. Repo root is the cwd pre-commit sets when it
# invokes the hook.
_TEST_PATH_RE = re.compile(r"(tests/[^\s\)\]\\\`'\">]+)")

# Only files under this prefix are scanned; everything else passes
# through unmolested. The hook configures ``files:`` in
# ``.pre-commit-config.yaml`` so pre-commit pre-filters, but a defensive
# in-script filter keeps the hook idempotent when invoked manually.
_SPEC_PATH_PREFIX = ".ea/specs/"
_SPEC_PATH_SUFFIX = ".md"


def _scan_spec_file(spec_path: Path, project_root: Path) -> list[str]:
    """Return repo-relative test paths cited in ``spec_path`` that do not exist.

    Args:
        spec_path: Repo-relative path to a spec markdown file.
        project_root: Absolute path to the repo root.

    Returns:
        List of cited test paths (in source order, de-duplicated by
        first occurrence) that do not resolve to an existing file
        under ``project_root``. Empty list when the spec cites no
        test paths or every cited path exists.
    """
    absolute = project_root / spec_path
    if not absolute.is_file():
        # Staged-but-deleted (rename in flight); nothing to scan.
        return []
    body = absolute.read_text(encoding="utf-8")
    seen: set[str] = set()
    missing: list[str] = []
    for match in _TEST_PATH_RE.finditer(body):
        ref = match.group(1)
        if ref in seen:
            continue
        seen.add(ref)
        candidate = project_root / ref
        if not candidate.is_file():
            missing.append(ref)
    return missing


def main(argv: list[str]) -> int:
    """Pre-commit hook entry point.

    Args:
        argv: First element is the script path (ignored); remaining
            elements are the staged paths pre-commit passes through.

    Returns:
        ``0`` when every cited test path under every staged spec file
        exists on disk; ``1`` when at least one citation is stale.
    """
    project_root = Path.cwd()
    rejections: list[tuple[str, list[str]]] = []
    for raw in argv[1:]:
        path = raw.strip()
        if not path:
            continue
        if not path.startswith(_SPEC_PATH_PREFIX):
            continue
        if not path.endswith(_SPEC_PATH_SUFFIX):
            continue
        missing = _scan_spec_file(Path(path), project_root)
        if missing:
            rejections.append((path, missing))
    if not rejections:
        return 0
    sys.stderr.write("pre_commit_spec_paths: stale test citations under .ea/specs/:\n")
    for spec_path, missing in rejections:
        joined = ", ".join(repr(ref) for ref in missing)
        sys.stderr.write(f"  {spec_path}: missing {joined}\n")
    sys.stderr.write("fix: rename / restore the cited test file or update the spec.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
