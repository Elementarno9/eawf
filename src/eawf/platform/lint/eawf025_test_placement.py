"""EAWF025 -- test placement: ``tests/<kind>/<source-package-path>/``.

A test's address encodes two facts. The first segment under ``tests/``
is its kind (see :mod:`eawf.platform.lint.kind_taxonomy`); the rest
mirrors the source package it exercises, so ``src/eawf/kernel/state/``
is covered by ``tests/<kind>/kernel/state/``. When either half drifts,
the suite stops being navigable from the source tree and a subject ends
up tested twice under two kinds with neither owning the behaviour.

The rule is deliberately diff-scoped: the pre-taxonomy tree does not
mirror the source layout, so the hook feeds it only the paths a commit
ADDS. Sub-directories listed in
:data:`~eawf.platform.lint.kind_taxonomy.NON_KIND_TEST_DIRS` are exempt
entirely -- they partition by subsystem, not by kind, and are re-filed
by dedicated waves rather than by this gate.

Two checks run over the scoped set:

1. Placement -- the first segment is a declared kind and the remaining
   directory chain names a real package under ``src/eawf/``.
2. Kind exclusivity -- no subject (the path with its kind segment
   removed) is filed under two kinds at once. ``__init__.py`` and
   ``conftest.py`` are per-directory scaffolding rather than subjects,
   so they are excluded from this check.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from eawf.platform.lint.kind_taxonomy import (
    NON_KIND_TEST_DIRS,
    TESTS_ROOT,
    TestKind,
    kind_for_test_path,
    repo_relative_test_path,
)

RULE_CODE = "EAWF025"

#: Source package root the mirror chain is resolved against.
SOURCE_ROOT = "src/eawf"

#: Filenames that are per-directory scaffolding rather than a test
#: subject, so they may legitimately repeat under every kind.
SCAFFOLD_FILENAMES: frozenset[str] = frozenset({"__init__.py", "conftest.py"})


@dataclass(frozen=True)
class TestPlacementViolation:
    """One EAWF025 finding.

    Attributes:
        path: Repo-relative path of the offending test file.
        reason: Why the path violates the placement contract.
    """

    path: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF025``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``CODE reason`` one-liner (the path is the row prefix)."""
        return f"{RULE_CODE} {self.reason}"


def source_package_paths(repo_root: Path) -> frozenset[str]:
    """Return every package path under ``src/eawf/`` as a mirror chain.

    Args:
        repo_root: Repository root the source tree is resolved against.

    Returns:
        Forward-slash chains relative to ``src/eawf`` (``kernel``,
        ``kernel/state``, ...) plus ``""`` for the package root itself.
        An absent source tree yields just ``""``.
    """
    source = repo_root / SOURCE_ROOT
    chains = {""}
    if not source.is_dir():
        return frozenset(chains)
    for entry in source.rglob("*"):
        if entry.is_dir() and "__pycache__" not in entry.parts:
            chains.add(entry.relative_to(source).as_posix())
    return frozenset(chains)


def _subject(relative: str, kind: TestKind) -> str:
    """Return ``relative`` with its ``tests/<kind>/`` prefix removed."""
    return relative[len(f"{TESTS_ROOT}{kind.value}/") :]


def _check_test_path(
    path: str,
    *,
    source_packages: frozenset[str],
    non_kind_dirs: frozenset[str] = NON_KIND_TEST_DIRS,
) -> TestPlacementViolation | None:
    """Return the placement violation for one test path, or ``None``.

    Args:
        path: A candidate path, absolute or repo-relative.
        source_packages: Mirror chains that name a real source package
            (see :func:`source_package_paths`).
        non_kind_dirs: ``tests/`` sub-directories exempt from the rule.

    Returns:
        The violation, or ``None`` when the path conforms or falls
        outside the rule's surface (a non-Python file, a file directly
        under ``tests/``, or a file under an exempt sub-directory).
    """
    relative = repo_relative_test_path(path)
    if not relative.startswith(TESTS_ROOT) or not relative.endswith(".py"):
        return None
    remainder = relative[len(TESTS_ROOT) :]
    head, _, tail = remainder.partition("/")
    if not tail or head in non_kind_dirs:
        return None
    kind = kind_for_test_path(relative)
    if kind is None:
        return TestPlacementViolation(
            path=relative,
            reason=(
                f"{TESTS_ROOT}{head}/ is not a declared test kind; file the test under "
                "tests/<kind>/<source-package-path>/"
            ),
        )
    chain = tail.rsplit("/", 1)[0] if "/" in tail else ""
    if chain not in source_packages:
        return TestPlacementViolation(
            path=relative,
            reason=(
                f"mirror path {chain!r} is not a package under {SOURCE_ROOT}/; a test "
                "mirrors the source package it exercises"
            ),
        )
    return None


def _exclusivity_violations(
    kinds_by_subject: dict[str, dict[str, TestKind]],
) -> list[TestPlacementViolation]:
    """Return one violation per path whose subject spans two kinds."""
    violations: list[TestPlacementViolation] = []
    for subject, by_path in kinds_by_subject.items():
        if len({*by_path.values()}) < 2:
            continue
        filed = ", ".join(sorted({kind.value for kind in by_path.values()}))
        violations.extend(
            TestPlacementViolation(
                path=candidate,
                reason=(
                    f"subject {subject!r} is filed under two kinds ({filed}); a subject "
                    "has exactly one owning kind"
                ),
            )
            for candidate in sorted(by_path)
        )
    return violations


def check_test_paths(
    paths: list[str],
    *,
    source_packages: frozenset[str],
    non_kind_dirs: frozenset[str] = NON_KIND_TEST_DIRS,
) -> list[TestPlacementViolation]:
    """Return placement + kind-exclusivity violations across ``paths``.

    Args:
        paths: Candidate paths (typically the staged ADDED set).
        source_packages: Mirror chains that name a real source package.
        non_kind_dirs: ``tests/`` sub-directories exempt from the rule.

    Returns:
        Placement violations in input order, followed by kind-exclusivity
        violations. Only paths that pass the placement check take part in
        the exclusivity check, so a misplaced file is reported once.
    """
    violations: list[TestPlacementViolation] = []
    kinds_by_subject: dict[str, dict[str, TestKind]] = defaultdict(dict)
    for path in paths:
        violation = _check_test_path(
            path, source_packages=source_packages, non_kind_dirs=non_kind_dirs
        )
        if violation is not None:
            violations.append(violation)
            continue
        relative = repo_relative_test_path(path)
        kind = kind_for_test_path(relative)
        if kind is None or relative.rsplit("/", 1)[-1] in SCAFFOLD_FILENAMES:
            continue
        kinds_by_subject[_subject(relative, kind)][relative] = kind
    violations.extend(_exclusivity_violations(dict(kinds_by_subject)))
    return violations
