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

The same holds one level up, for commits rather than paths. A delivered
product's default branch carries exactly three permanent commits --
bootstrap, verified delivery, observed publication -- each the only
parent of the next, and no commit per Run, per Task transition or per
receipt. One disposable review checkpoint freezes the reviewed tree: the
delivery commit cites it by provenance and never descends from it, so
discarding the checkpoint rewrites nothing permanent.
:func:`ancestry_findings` reports every way a history disagrees with that
policy, again over facts the caller collected from git.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from itertools import pairwise
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
        ".ea/retyped-triage.yaml",
        _YES,
        "where each re-typed rule over the threshold went; the tag preflight reads it",
    ),
    _row(
        ".ea/requirements.json",
        _YES,
        "the requirement catalog and its generated trace; CI rechecks it by value",
    ),
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
        ".ea/generations/selected.json",
        _YES,
        "the pointer naming the epoch-2 generation a clone reads from",
    ),
    _row(
        ".ea/generations/EPOCH2_ACTIVE.json",
        _YES,
        "the epoch marker; without it a clone would read the tree as epoch 1",
    ),
    _row(
        ".ea/generations/gen-*/state.json",
        _YES,
        "one generation's compare-and-swap document of work in flight",
        tier=StorageTier.DOCUMENT,
    ),
    _row(
        ".ea/generations/gen-*/ledger/*.jsonl",
        _YES,
        "one generation's append-only history; a line is never rewritten",
        tier=StorageTier.LEDGER,
    ),
    _row(
        ".ea/generations/gen-*/indexes/**",
        _NO,
        "one generation's offset indexes; they regenerate from its ledgers",
        tier=StorageTier.DERIVED,
    ),
    _row(
        ".ea/generations/gen-*/local/**",
        _NO,
        "one generation's Task and Run status projection; only release, delivery "
        "and decision facts are committed",
        tier=StorageTier.LOCAL_STORE,
    ),
    _row(
        ".ea/generations/.staging-*/**",
        _NO,
        "half-built generations an apply compares and then publishes or discards",
    ),
    _row(
        ".ea/generations/restore/**",
        _NO,
        "pre-cutover copies of the authority files, firehose and telemetry included; "
        "git history already holds the committed originals",
    ),
    _row(
        ".ea/generations/journal.jsonl",
        _NO,
        "how far one machine's cutover got; its free-text detail stays local",
    ),
    _row(
        ".ea/epoch2-disposable-canary.json",
        _NO,
        "names who declared this tree throwaway; a clone must declare that for itself",
    ),
    _row(
        ".ea/epoch2-opt-in.json",
        _YES,
        "the repository's opt-in into epoch 2; every clone shares it, and the backup it "
        "names is verified only on the machine that applies the cutover",
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
        ".ea/specs/**",
        _NO,
        "per-wave spec renders; the typed criteria in state are the record",
    ),
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


class PermanentCommitKind(StrEnum):
    """The three commits a delivered product's default branch keeps, in order."""

    BOOTSTRAP = "bootstrap"
    DELIVERY = "delivery"
    PUBLICATION = "publication"


#: The order the permanent commits descend in, each the only parent of the next.
PERMANENT_COMMIT_ORDER: Final[tuple[PermanentCommitKind, ...]] = tuple(PermanentCommitKind)

#: The scheme a delivery commit's provenance trailer addresses its manifest by.
DELIVERY_PROVENANCE_SCHEME: Final = "manifest://"

#: The trailer a delivery commit cites its disposable review checkpoint by.
REVIEW_CHECKPOINT_TRAILER_KEY: Final = "Eawf-Review-Checkpoint"

#: The subject of the commit that records an observed, or a partial, publication.
PUBLICATION_SUBJECT: Final = re.compile(r"chore: record (observed|partial) \S+ publication")


class HistoryCommit(BaseModel):
    """One commit as the ancestry check reads it.

    Attributes:
        sha: The commit's object name.
        parents: Its parents' object names, first parent first.
        subject: The first line of its message.
        provenance: Its ``Eawf-Provenance`` trailer, when it carries one.
        cites: The commits its provenance cites -- its
            ``Eawf-Review-Checkpoint`` trailers -- which is how a delivery
            names the review checkpoint it did not descend from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")]
    parents: tuple[Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")], ...] = ()
    subject: Annotated[str, Field(min_length=1, max_length=200)]
    provenance: str | None = None
    cites: tuple[str, ...] = ()


def classify_commit(commit: HistoryCommit) -> PermanentCommitKind | None:
    """Return which permanent commit *commit* is, or ``None`` for any other.

    Args:
        commit: The commit to classify.

    Returns:
        ``BOOTSTRAP`` for a root commit, ``PUBLICATION`` for a commit
        whose subject records a publication, ``DELIVERY`` for a commit
        whose provenance names a delivery manifest, and ``None`` for
        everything else -- status, event and receipt chatter included.
    """
    if not commit.parents:
        return PermanentCommitKind.BOOTSTRAP
    if PUBLICATION_SUBJECT.fullmatch(commit.subject):
        return PermanentCommitKind.PUBLICATION
    if commit.provenance is not None and commit.provenance.startswith(DELIVERY_PROVENANCE_SCHEME):
        return PermanentCommitKind.DELIVERY
    return None


class AncestryFindingKind(StrEnum):
    """The ways a default branch can disagree with the permanent-commit policy."""

    CHATTER_COMMIT = "chatter_commit"
    PERMANENT_ORDER = "permanent_order"
    BROKEN_ANCESTRY = "broken_ancestry"
    CHECKPOINT_IN_ANCESTRY = "checkpoint_in_ancestry"
    CHECKPOINT_UNCITED = "checkpoint_uncited"


class AncestryFinding(BaseModel):
    """One disagreement between a history and the permanent-commit policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AncestryFindingKind
    sha: Annotated[str, Field(min_length=1, max_length=64)]
    reason: Annotated[str, Field(min_length=1, max_length=240)]

    def render(self) -> str:
        """Return a ``kind sha: reason`` one-liner for the console."""
        return f"{self.kind.value} {self.sha}: {self.reason}"


def ancestry_findings(
    *,
    history: Sequence[HistoryCommit],
    checkpoint: HistoryCommit,
) -> tuple[AncestryFinding, ...]:
    """Return every way *history* disagrees with the permanent-commit policy.

    Args:
        history: The default branch's commits, oldest first, as its
            first-parent walk lists them.
        checkpoint: The one disposable review checkpoint.

    Returns:
        The findings in history order: a commit that is none of the three
        permanent kinds, permanent commits out of order or not three, a
        permanent commit whose only parent is not its predecessor, a
        checkpoint the branch descends from, and a delivery that does not
        cite the checkpoint.
    """
    findings: list[AncestryFinding] = []
    permanent: list[tuple[PermanentCommitKind, HistoryCommit]] = []
    for commit in history:
        kind = classify_commit(commit)
        if kind is None:
            findings.append(
                AncestryFinding(
                    kind=AncestryFindingKind.CHATTER_COMMIT,
                    sha=commit.sha,
                    reason=f"{commit.subject!r} is not one of the three permanent commits",
                )
            )
            continue
        permanent.append((kind, commit))
    kinds = tuple(kind for kind, _ in permanent)
    if kinds != PERMANENT_COMMIT_ORDER:
        findings.append(
            AncestryFinding(
                kind=AncestryFindingKind.PERMANENT_ORDER,
                sha=history[-1].sha if history else checkpoint.sha,
                reason=f"permanent commits are {[kind.value for kind in kinds]}, not "
                f"{[kind.value for kind in PERMANENT_COMMIT_ORDER]}",
            )
        )
    findings.extend(_parent_findings(permanent))
    findings.extend(_checkpoint_findings(history, permanent, checkpoint))
    return tuple(findings)


def _parent_findings(
    permanent: Sequence[tuple[PermanentCommitKind, HistoryCommit]],
) -> list[AncestryFinding]:
    """Return a finding for each permanent commit not parented only on its predecessor."""
    findings: list[AncestryFinding] = []
    for previous, (_, commit) in pairwise(permanent):
        expected = (previous[1].sha,)
        if commit.parents != expected:
            findings.append(
                AncestryFinding(
                    kind=AncestryFindingKind.BROKEN_ANCESTRY,
                    sha=commit.sha,
                    reason=f"parents {list(commit.parents)} are not exactly the "
                    f"{previous[0].value} commit {previous[1].sha}",
                )
            )
    return findings


def _checkpoint_findings(
    history: Sequence[HistoryCommit],
    permanent: Sequence[tuple[PermanentCommitKind, HistoryCommit]],
    checkpoint: HistoryCommit,
) -> list[AncestryFinding]:
    """Return the findings about the disposable review checkpoint."""
    findings: list[AncestryFinding] = []
    ancestry = {commit.sha for commit in history}
    ancestry.update(parent for commit in history for parent in commit.parents)
    if checkpoint.sha in ancestry:
        findings.append(
            AncestryFinding(
                kind=AncestryFindingKind.CHECKPOINT_IN_ANCESTRY,
                sha=checkpoint.sha,
                reason="the disposable review checkpoint is an ancestor of the default branch",
            )
        )
    deliveries = [commit for kind, commit in permanent if kind is PermanentCommitKind.DELIVERY]
    for delivery in deliveries:
        if checkpoint.sha not in delivery.cites:
            findings.append(
                AncestryFinding(
                    kind=AncestryFindingKind.CHECKPOINT_UNCITED,
                    sha=delivery.sha,
                    reason=f"the delivery's provenance does not cite checkpoint {checkpoint.sha}",
                )
            )
    return findings


__all__ = [
    "CENSUS_SURFACE_PREFIX",
    "DELIVERY_PROVENANCE_SCHEME",
    "EA_PATH_CLASSES",
    "PERMANENT_COMMIT_ORDER",
    "PROBE_SEGMENT",
    "PUBLICATION_SUBJECT",
    "REVIEW_CHECKPOINT_TRAILER_KEY",
    "AncestryFinding",
    "AncestryFindingKind",
    "CensusFinding",
    "CensusFindingKind",
    "CommitPolicy",
    "CommitPolicyError",
    "HistoryCommit",
    "PathClass",
    "PermanentCommitKind",
    "UndeclaredPathError",
    "ancestry_findings",
    "census_findings",
    "classify_commit",
    "classify_path",
    "probe_paths",
]
