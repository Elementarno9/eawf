"""Which paths under ``.ea/`` version control carries, and which it must not.

Whether a file under ``.ea/`` is committed used to be convention spread
across a ``.gitignore`` comment, a config leaf, and whatever the writer
happened to do. Convention has two failure modes and this tree has hit
both: a firehose or a regenerable projection lands in version control
and never leaves, and a file that has to survive a clone is matched by
an ignore rule nobody re-read, so a fresh checkout is missing it and
nothing says so.

This module makes the answer a declaration. A path is matched against a
table of :class:`PathClass` rows and the most specific matching row
wins, so the table is order-independent and a row added in the wrong
place cannot quietly shadow another. A path under ``.ea/`` that matches
no row raises rather than defaulting, which is what makes the
classification total: a new directory has to be declared before its
first file can be tracked.

The table agrees with the storage tiers rather than restating them.
A row that holds a tiered collection's bytes names its
:class:`~eawf.kernel.store.tiers.StorageTier`, so the document and the
append-only ledgers are committed while the firehose, the local store
and the derived projections are not.

:func:`census_findings` is the enforcement. It takes two facts a caller
collects from git -- which paths are tracked, and which probe paths the
ignore rules match -- and reports every way the tree disagrees with the
declaration. Keeping the git calls out of this module keeps the
classification a pure function that a test can drive with a fixture.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.store.tiers import StorageTier

#: The directory the classification is total over: a tracked path under
#: this prefix that matches no declared row is a census failure.
CENSUS_SURFACE_PREFIX: Final = ".ea/"

#: The segment substituted for a wildcard when a row is turned into one
#: representative path for the ignore probe.
PROBE_SEGMENT: Final = "_census_probe"


class CommitPolicy(StrEnum):
    """The whole vocabulary: a path is committed, or it is not."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


class CommitPolicyError(ValueError):
    """The classification cannot answer for a path."""


class UndeclaredPathError(CommitPolicyError):
    """A path under the census surface matches no declared row."""


class PathClass(BaseModel):
    """One declared path family and the commit policy it carries.

    ``must_exist`` is only meaningful for a literal pattern: a wildcard
    row describes a family that may legitimately be empty, so requiring
    it to exist would assert something the pattern does not say.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: Annotated[str, Field(min_length=1, max_length=120)]
    policy: CommitPolicy
    tier: StorageTier | None = None
    must_exist: bool = False
    note: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("pattern")
    @classmethod
    def _repo_relative(cls, value: str) -> str:
        """Reject a pattern that is not a plain repo-relative path.

        Args:
            value: The declared pattern.

        Returns:
            The pattern unchanged.

        Raises:
            ValueError: The pattern is absolute, walks upward, or names
                a directory instead of the files inside it.
        """
        if value.startswith("/") or value.endswith("/"):
            raise ValueError(f"pattern {value!r} must be repo-relative and name files, not a dir")
        if ".." in value.split("/"):
            raise ValueError(f"pattern {value!r} must not walk upward")
        return value

    @model_validator(mode="after")
    def _existence_needs_a_literal(self) -> Self:
        """Reject ``must_exist`` on a wildcard pattern.

        Returns:
            The validated row.

        Raises:
            ValueError: A wildcard row claims a specific file must exist.
        """
        if self.must_exist and "*" in self.pattern:
            raise ValueError(f"pattern {self.pattern!r} is a family, so it cannot be must_exist")
        return self

    @property
    def specificity(self) -> int:
        """Return the count of literal characters, which resolves overlaps."""
        return len(self.pattern.replace("*", ""))

    @property
    def probe(self) -> str:
        """Return one representative path of this family for the ignore probe."""
        segments = [
            PROBE_SEGMENT if segment == "**" else segment.replace("*", PROBE_SEGMENT)
            for segment in self.pattern.split("/")
        ]
        return "/".join(segments)

    def matches(self, path: str) -> bool:
        """Report whether *path* belongs to this family.

        Args:
            path: A repo-relative, forward-slash path.

        Returns:
            ``True`` when the pattern matches, with ``*`` confined to one
            segment and ``**`` spanning any number of them.
        """
        return PurePosixPath(path).full_match(self.pattern)


def _row(
    pattern: str,
    policy: CommitPolicy,
    note: str,
    *,
    tier: StorageTier | None = None,
    must_exist: bool = False,
) -> PathClass:
    """Build one classification row (positional to keep the table readable)."""
    return PathClass(pattern=pattern, policy=policy, tier=tier, must_exist=must_exist, note=note)


_YES: Final = CommitPolicy.COMMITTED
_NO: Final = CommitPolicy.NOT_COMMITTED

EA_PATH_CLASSES: Final[tuple[PathClass, ...]] = (
    _row(
        ".ea/state.json",
        _YES,
        "the compare-and-swap document of work in flight",
        tier=StorageTier.DOCUMENT,
        must_exist=True,
    ),
    _row(
        ".ea/config.yaml",
        _YES,
        "the layered config a clone needs to resolve anything",
        must_exist=True,
    ),
    _row(".ea/rules.yaml", _YES, "the authored rule corpus the projections render from"),
    _row(".ea/profile.yaml", _YES, "the project profile that selects the rule set"),
    _row(".ea/profiles/*.yaml", _YES, "per-topic profile overlays, authored by hand"),
    _row(".ea/bench/*.yaml", _YES, "benchmark thresholds a run is scored against"),
    _row(
        ".ea/store/event.jsonl",
        _NO,
        "raw agent output; unbounded and full of text nobody typed",
        tier=StorageTier.FIREHOSE,
    ),
    _row(
        ".ea/store/*.jsonl",
        _YES,
        "the typed epoch-1 stores that carry the evidence chain",
        tier=StorageTier.LEDGER,
    ),
    _row(
        ".ea/ledger/*.jsonl",
        _YES,
        "epoch-2 append-only history; a line is never rewritten",
        tier=StorageTier.LEDGER,
    ),
    _row(
        ".ea/indexes/**",
        _NO,
        "offset indexes that regenerate byte-identically from the ledgers",
        tier=StorageTier.DERIVED,
    ),
    _row(
        ".ea/rules/views/**",
        _NO,
        "per-runtime rule renders; the authored corpus is the source",
        tier=StorageTier.DERIVED,
    ),
    _row(
        ".ea/artifacts/rendered/**",
        _NO,
        "render cache for memory and research views; regenerable",
        tier=StorageTier.DERIVED,
    ),
    _row(".ea/artifacts/**", _YES, "durable audits, research, decisions and evidence"),
    _row(
        ".ea/local/**",
        _NO,
        "machine-local scratch; a clone starts empty by design",
        tier=StorageTier.LOCAL_STORE,
    ),
    _row(
        ".ea/telemetry.db",
        _NO,
        "measurement history that embeds this machine's absolute paths",
        tier=StorageTier.LOCAL_STORE,
    ),
    _row(".ea/locks/**", _NO, "lock directory carrying a live pid and hostname"),
    _row(".ea/worktrees/**", _NO, "per-wave worktrees; git owns their lifetime"),
    _row(".ea/**/*.lock", _NO, "sibling lockfiles carrying a pid heartbeat"),
    _row(".ea/state.json.bak.*", _NO, "migration backups; forward-fix is the recovery path"),
    _row(".ea/instrument-probe.json", _NO, "probe cache that embeds absolute host paths"),
    _row(".ea/**/.DS_Store", _NO, "finder metadata the OS writes into any directory"),
    _row(
        "AGENTS.md",
        _YES,
        "the root projection of the rule corpus; the committed agent contract",
        must_exist=True,
    ),
)


class CensusFindingKind(StrEnum):
    """The five ways a tree can disagree with the declaration."""

    UNDECLARED = "undeclared"
    TRACKED_BUT_NOT_COMMITTED = "tracked_but_not_committed"
    DECLARED_BUT_ABSENT = "declared_but_absent"
    COMMITTED_BUT_IGNORED = "committed_but_ignored"
    NOT_COMMITTED_BUT_UNIGNORED = "not_committed_but_unignored"


class CensusFinding(BaseModel):
    """One disagreement between the tree and the declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CensusFindingKind
    path: Annotated[str, Field(min_length=1, max_length=240)]
    reason: Annotated[str, Field(min_length=1, max_length=240)]

    def render(self) -> str:
        """Return a ``kind path: reason`` one-liner for the console."""
        return f"{self.kind.value} {self.path}: {self.reason}"


def classify_path(
    path: str,
    *,
    classes: Sequence[PathClass] = EA_PATH_CLASSES,
) -> PathClass:
    """Return the one row that governs *path*.

    Overlapping rows are resolved by specificity, so
    ``.ea/store/event.jsonl`` beats ``.ea/store/*.jsonl`` no matter which
    order they are declared in.

    Args:
        path: A repo-relative path; back-slashes are folded first.
        classes: The declaration to resolve against.

    Returns:
        The governing row.

    Raises:
        UndeclaredPathError: No row matches, so the path has no declared
            commit policy.
        CommitPolicyError: Two equally specific rows disagree on the
            policy, so the declaration is ambiguous for this path.
    """
    normalized = path.replace("\\", "/")
    matched = [row for row in classes if row.matches(normalized)]
    if not matched:
        raise UndeclaredPathError(
            f"{normalized!r} matches no declared row; declare it in EA_PATH_CLASSES"
        )
    best = max(row.specificity for row in matched)
    winners = [row for row in matched if row.specificity == best]
    policies = {row.policy for row in winners}
    if len(policies) > 1:
        patterns = ", ".join(sorted(row.pattern for row in winners))
        raise CommitPolicyError(f"{normalized!r} is claimed by disagreeing rows: {patterns}")
    return winners[0]


def probe_paths(*, classes: Sequence[PathClass] = EA_PATH_CLASSES) -> tuple[str, ...]:
    """Return one representative path per row, in table order.

    The caller hands these to ``git check-ignore --no-index`` so the
    census learns which rows the ignore rules match, without needing the
    described files to exist.

    Args:
        classes: The declaration to build probes for.

    Returns:
        The probe path of every row.
    """
    return tuple(row.probe for row in classes)


def _tracked_findings(
    tracked: Iterable[str],
    classes: Sequence[PathClass],
) -> list[CensusFinding]:
    """Return the findings that come from what git tracks."""
    findings: list[CensusFinding] = []
    for path in tracked:
        if not path.startswith(CENSUS_SURFACE_PREFIX):
            continue
        try:
            row = classify_path(path, classes=classes)
        except UndeclaredPathError:
            findings.append(
                CensusFinding(
                    kind=CensusFindingKind.UNDECLARED,
                    path=path,
                    reason="tracked under .ea/ but matches no declared row",
                )
            )
            continue
        if row.policy is CommitPolicy.NOT_COMMITTED:
            findings.append(
                CensusFinding(
                    kind=CensusFindingKind.TRACKED_BUT_NOT_COMMITTED,
                    path=path,
                    reason=f"declared not committed by {row.pattern!r} ({row.note})",
                )
            )
    return findings


def _declaration_findings(
    tracked: frozenset[str],
    ignored_probes: frozenset[str],
    classes: Sequence[PathClass],
) -> list[CensusFinding]:
    """Return the findings that come from the declaration's own promises."""
    findings: list[CensusFinding] = []
    for row in classes:
        if row.must_exist and row.pattern not in tracked:
            findings.append(
                CensusFinding(
                    kind=CensusFindingKind.DECLARED_BUT_ABSENT,
                    path=row.pattern,
                    reason="declared committed and required, but git does not track it",
                )
            )
        ignored = row.probe in ignored_probes
        if row.policy is CommitPolicy.COMMITTED and ignored:
            findings.append(
                CensusFinding(
                    kind=CensusFindingKind.COMMITTED_BUT_IGNORED,
                    path=row.pattern,
                    reason="declared committed but an ignore rule matches it",
                )
            )
        if row.policy is CommitPolicy.NOT_COMMITTED and not ignored:
            findings.append(
                CensusFinding(
                    kind=CensusFindingKind.NOT_COMMITTED_BUT_UNIGNORED,
                    path=row.pattern,
                    reason="declared not committed but no ignore rule matches it",
                )
            )
    return findings


def census_findings(
    *,
    tracked: Iterable[str],
    ignored_probes: Iterable[str],
    classes: Sequence[PathClass] = EA_PATH_CLASSES,
) -> tuple[CensusFinding, ...]:
    """Return every disagreement between the tree and the declaration.

    Args:
        tracked: Repo-relative paths git tracks (the whole ``git
            ls-files`` set is accepted; anything outside the census
            surface is skipped).
        ignored_probes: The subset of :func:`probe_paths` that the ignore
            rules match, as reported by ``git check-ignore --no-index``.
        classes: The declaration to check against.

    Returns:
        The findings, tracked-path ones first and then declaration ones,
        each group in input order.
    """
    tracked_set = frozenset(tracked)
    findings = _tracked_findings(sorted(tracked_set), classes)
    findings.extend(_declaration_findings(tracked_set, frozenset(ignored_probes), classes))
    return tuple(findings)


__all__ = [
    "CENSUS_SURFACE_PREFIX",
    "EA_PATH_CLASSES",
    "PROBE_SEGMENT",
    "CensusFinding",
    "CensusFindingKind",
    "CommitPolicy",
    "CommitPolicyError",
    "PathClass",
    "UndeclaredPathError",
    "census_findings",
    "classify_path",
    "probe_paths",
]
