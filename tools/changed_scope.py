"""Changed-scope pytest selector -- a stateless map from a changed-file set
to the pytest targets worth running.

The selector answers one question: *given the files a diff touched, which
pytest paths should run?* It is a **pure function of the input file list** --
:func:`select_scope` performs string transforms only, reads no cache file, no
database, and no filesystem, so the same input always yields the same output
with no side effects. The only filesystem touch lives at the CLI boundary
(:func:`main`), which gathers the changed set from ``git`` and optionally
prunes selected paths that do not exist on disk before printing them.

Mapping rules:

- A changed **non-``.py``** file (a golden fixture, a template, a YAML) forces
  the full golden tier (:data:`GOLDEN_TIER` = ``tests/golden`` + the snapshot /
  perf tiers). Golden bytes are asserted against by the whole tier, so any
  non-source change re-runs all of it.
- A changed **``src/eawf/<pkg>/<module>.py``** selects the mirror test file
  (``tests/<pkg>/test_<module>.py``) *and* the package directory's tests
  (``tests/<pkg>``). The ``tests/`` tree mirrors ``src/eawf/`` package-for-
  package, so the mirror path is a pure string transform.
- A changed **test file** (``tests/**/test_*.py``) selects itself.
- Any other ``.py`` (a ``tools/`` script, a ``conftest.py``, the ``src/eawf``
  package root's own ``__init__.py``) contributes nothing on its own; the
  caller decides its own fallback.

The final scope is the sorted, de-duplicated union across the whole changed
set: one non-``.py`` change plus three source modules yields the golden tier
plus each module's mirror targets, once each.

Invocation::

    python3 tools/changed_scope.py [FILES ...]
    python3 tools/changed_scope.py --base origin/main [--existing-only]

With ``--base <ref>`` the changed set is ``git diff --name-only <ref>...HEAD``;
otherwise it is the positional ``FILES``. ``--existing-only`` prunes selected
paths absent from ``--repo-root`` (default: cwd) so the printed scope is safe
to hand straight to ``pytest``. The scope is printed space-joined on stdout.

Exit codes:

- ``0`` -- scope computed and printed (an empty scope prints a blank line).
- ``2`` -- bad CLI usage (argparse) or a failing ``git diff``.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# The golden / snapshot / perf tiers, forced whole whenever a non-``.py`` file
# changes. These directories assert against committed golden + snapshot bytes,
# so a changed template / fixture / YAML can invalidate any test in the tier.
GOLDEN_TIER: tuple[str, ...] = ("tests/golden", "tests/snapshots", "tests/perf")

_SRC_PREFIX = "src/eawf/"
_TESTS_PREFIX = "tests/"


@dataclass(frozen=True)
class ScopeSelection:
    """The pytest scope selected for a changed-file set.

    Attributes:
        full_golden: True iff at least one changed file was non-``.py`` and so
            forced the full golden tier into ``paths``.
        paths: The sorted, de-duplicated union of selected pytest targets.
    """

    full_golden: bool
    paths: tuple[str, ...]


def select_scope(changed: Iterable[str]) -> ScopeSelection:
    """Map a set of changed file paths to the pytest scope worth running.

    Pure and stateless: string transforms only, no filesystem, cache, or
    database access, so identical input yields identical output with no side
    effects. The input iterable is read but never mutated.

    Args:
        changed: An iterable of repo-relative changed-file paths. POSIX or
            Windows separators are both accepted (normalized to ``/``).

    Returns:
        A :class:`ScopeSelection` whose ``paths`` is the sorted union of the
        selected targets and whose ``full_golden`` flags a forced golden tier.

    Raises:
        TypeError: ``changed`` is a bare ``str`` / ``bytes`` (which would
            iterate characters) or any element is not a ``str``.
    """
    if isinstance(changed, (str, bytes)):
        raise TypeError(
            f"changed must be an iterable of path strings, not a bare string: {changed!r}"
        )
    selected: set[str] = set()
    full_golden = False
    for entry in changed:
        if not isinstance(entry, str):
            raise TypeError(f"changed entry must be str, got {type(entry).__name__}: {entry!r}")
        path = entry.strip().replace("\\", "/")
        if not path:
            continue
        if not path.endswith(".py"):
            full_golden = True
            selected.update(GOLDEN_TIER)
            continue
        selected.update(_targets_for_py(path))
    return ScopeSelection(full_golden=full_golden, paths=tuple(sorted(selected)))


def _targets_for_py(path: str) -> tuple[str, ...]:
    """Return the pytest targets for a single changed ``.py`` path."""
    name = PurePosixPath(path).name
    if path.startswith(_TESTS_PREFIX) and name.startswith("test_"):
        # A changed test file selects itself.
        return (path,)
    if path.startswith(_SRC_PREFIX):
        return _targets_for_src_module(path)
    # Any other .py (tools/, a conftest, the eawf package root __init__)
    # contributes nothing on its own.
    return ()


def _targets_for_src_module(path: str) -> tuple[str, ...]:
    """Map ``src/eawf/<pkg>/<module>.py`` to its mirror test file + package dir.

    The ``tests/`` tree mirrors ``src/eawf/`` package-for-package, so the map
    is a pure path transform: ``src/eawf/kernel/state/models.py`` ->
    (``tests/kernel/state``, ``tests/kernel/state/test_models.py``).
    """
    rel = PurePosixPath(path[len(_SRC_PREFIX) :])
    module = rel.stem
    pkg_rel = rel.parent
    if str(pkg_rel) == ".":
        # A module directly under src/eawf/ (e.g. _version.py). The mirror
        # "package dir" would be the whole tests/ tree -- too broad to select --
        # so emit only the mirror test file.
        if module == "__init__":
            return ()
        return (f"tests/test_{module}.py",)
    pkg_dir = f"tests/{pkg_rel}"
    if module == "__init__":
        # A package __init__ selects the package's tests, no mirror file.
        return (pkg_dir,)
    return (pkg_dir, f"{pkg_dir}/test_{module}.py")


def _git_changed_files(base: str, *, repo_root: Path) -> list[str]:
    """Return the files changed between ``base`` and ``HEAD`` via ``git diff``."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    """CLI shim: gather the changed set, print the selected pytest scope."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        help="explicit changed-file paths (mutually exclusive with --base)",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="git ref; the changed set is `git diff --name-only <base>...HEAD`",
    )
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help="prune selected paths that do not exist under --repo-root",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repo root for --base git diff + --existing-only checks (default: cwd)",
    )
    args = parser.parse_args(argv)

    if args.base and args.files:
        parser.error("pass either --base or explicit files, not both")

    if args.base:
        try:
            changed = _git_changed_files(args.base, repo_root=args.repo_root)
        except subprocess.CalledProcessError as exc:
            print(f"changed_scope: git diff failed: {exc.stderr.strip()}", file=sys.stderr)
            return 2
    else:
        changed = args.files

    scope = select_scope(changed)
    paths = scope.paths
    if args.existing_only:
        paths = tuple(p for p in paths if (args.repo_root / p).exists())
    print(" ".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
