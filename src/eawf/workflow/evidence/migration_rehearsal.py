"""Read back the committed cutover rehearsal, corpus by corpus and leg by leg.

The epoch-2 cutover is rehearsed over a declared set of corpora, and each
rehearsal leaves a committed record of what its legs observed. This
module is the *consumer* of those records: it is what lets a release
readiness sweep say "the migration is proven" as a fact about evidence
on disk rather than as a claim about a suite somebody ran once.

Three properties make the reading falsifiable.

**The corpus set is declared here.** ``REHEARSED_CORPORA`` names every
corpus the signal is computed over and what each is expected to do.
Reading whatever records happen to be present would make the signal pass
by deleting a record, which is the opposite of evidence.

**The stage list comes from the production enum.** The stages one clean
apply journals are derived from
:class:`~eawf.kernel.migration.epoch2.journal.CutoverStage`, not copied
into a literal here, so adding a stage to the cutover reds the signal
until the corpora are rehearsed again against it.

**An absent record is absent, not clean.** A missing corpus, a missing
leg or an unreadable record resolves to
:attr:`RehearsalGap.MISSING_RECORD` and friends rather than to silence.
A leg that ran and disagrees with the contract is a separate finding,
because the two call for different repairs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import ConfigDict, ValidationError

from eawf.kernel.migration.epoch2.journal import CutoverStage
from eawf.kernel.spec.common import _StrictModel

logger = logging.getLogger(__name__)

#: Where the rehearsal records are committed, relative to the repo root.
REHEARSAL_EVIDENCE_DIR: Final[tuple[str, ...]] = (
    "tests",
    "golden",
    "kernel",
    "migration",
    "rehearsal",
)

#: The stages a recovery adds to a journal. A journal carrying one is a
#: journal whose apply did not finish, so they are excluded when deriving
#: what a clean apply looks like.
RECOVERY_STAGES: Final[frozenset[CutoverStage]] = frozenset(
    {
        CutoverStage.ROLLBACK_DISCARDED,
        CutoverStage.RESTORE_VERIFIED,
        CutoverStage.SURFACES_RESTORED,
        CutoverStage.ACTIVATION_COMPLETED,
    }
)

#: The stages one clean apply journals, in order, derived from the
#: production enum rather than restated.
APPLY_JOURNAL_STAGES: Final[tuple[str, ...]] = tuple(
    stage.value for stage in CutoverStage if stage not in RECOVERY_STAGES
)

#: The outcome a rollback rehearsal has to report.
ROLLBACK_OUTCOME: Final[str] = CutoverStage.SURFACES_RESTORED.value

#: The epoch a rolled-back tree reads from.
ROLLED_BACK_EPOCH: Final[int] = 1


class RehearsalDisposition(StrEnum):
    """Whether a corpus is expected to import or to be refused.

    Values:
        IMPORTS: The importer takes it, so it rehearses four legs.
        REFUSES: The importer refuses it, so it rehearses the refusal --
            which is four claims of its own: the plan refuses with a
            code, the apply refuses with the same code, a retry refuses
            identically, and the target tree is left untouched.
    """

    IMPORTS = "imports"
    REFUSES = "refuses"


class RehearsalLeg(StrEnum):
    """The four legs one rehearsal runs.

    Values:
        DRY_RUN: Seal a plan and prove it reproducible.
        APPLY: Publish a generation into a disposable tree.
        RERUN: Re-apply the approved plan and write nothing.
        ROLLBACK: Take the tree back to epoch 1 and restore every
            authority surface.
    """

    DRY_RUN = "dry_run"
    APPLY = "apply"
    RERUN = "rerun"
    ROLLBACK = "rollback"


class RehearsalGap(StrEnum):
    """Why the rehearsal evidence does not prove the migration.

    The split is between evidence that is *absent* and evidence that
    *disagrees*, because they are different repairs: the first needs the
    rehearsal run, the second needs the cutover fixed.

    Values:
        MISSING_EVIDENCE_DIR: No rehearsal records were committed at all.
        MISSING_RECORD: A declared corpus has no record.
        UNREADABLE_RECORD: A record is present but is not a rehearsal
            record.
        DISPOSITION_MISMATCH: A record reports the opposite disposition
            to the one declared for its corpus.
        MISSING_LEG: An expected leg is absent from a record.
        LEG_FAILED: A leg ran and its claim does not hold.
        UNSCRUBBED_CORPUS: A staged corpus carried a concrete home path.
    """

    MISSING_EVIDENCE_DIR = "missing_evidence_dir"
    MISSING_RECORD = "missing_record"
    UNREADABLE_RECORD = "unreadable_record"
    DISPOSITION_MISMATCH = "disposition_mismatch"
    MISSING_LEG = "missing_leg"
    LEG_FAILED = "leg_failed"
    UNSCRUBBED_CORPUS = "unscrubbed_corpus"


#: The gaps that mean evidence was never produced, as opposed to
#: evidence that was produced and disagrees. A sweep reports the first
#: group as unproven and the second as failing.
ABSENCE_GAPS: Final[frozenset[RehearsalGap]] = frozenset(
    {
        RehearsalGap.MISSING_EVIDENCE_DIR,
        RehearsalGap.MISSING_RECORD,
        RehearsalGap.UNREADABLE_RECORD,
        RehearsalGap.MISSING_LEG,
    }
)

#: Every corpus the migration signal is computed over, and what each is
#: expected to do. Eight structural shapes, this project's own frozen
#: history, and the live corpus at cutover scale.
REHEARSED_CORPORA: Final[Mapping[str, RehearsalDisposition]] = {
    "empty-repository": RehearsalDisposition.IMPORTS,
    "minimal-active": RehearsalDisposition.IMPORTS,
    "all-terminal": RehearsalDisposition.IMPORTS,
    "multi-root-workspace": RehearsalDisposition.IMPORTS,
    "interrupted-close": RehearsalDisposition.IMPORTS,
    "open-attention": RehearsalDisposition.IMPORTS,
    "corrupt-reference": RehearsalDisposition.REFUSES,
    "unknown-extension-field": RehearsalDisposition.REFUSES,
    "p30-i26-history": RehearsalDisposition.IMPORTS,
    "largest-supported-state": RehearsalDisposition.IMPORTS,
}


class DryRunEvidence(_StrictModel):
    """What sealing a plan over one corpus recorded.

    Every field but :attr:`reproducible` is optional because the record
    of a corpus that moves -- the live one -- deliberately drops its
    digests and counts: pinning those would record the day the record
    was written rather than a property of the cutover.

    Attributes:
        reproducible: Whether a second seal over the same bytes produced
            the same manifest digest.
        source_digest: Digest naming the pinned corpus revision.
        manifest_digest: The manifest's digest over its own content.
        approval_digest: What an operator approves.
        source_rows: How many rows the source census counted.
        target_rows: How many rows the import would write.
        unresolved_rows: Every row the cutover cannot place, by address.
        collections: One ``source/target/unresolved`` triple per source
            collection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reproducible: bool
    source_digest: str | None = None
    manifest_digest: str | None = None
    approval_digest: str | None = None
    source_rows: int | None = None
    target_rows: int | None = None
    unresolved_rows: tuple[str, ...] = ()
    collections: Mapping[str, tuple[int, ...]] = {}


class ApplyEvidence(_StrictModel):
    """What the apply leg published.

    Attributes:
        applied: Whether the apply wrote a generation.
        journal_stages: The stages it journalled, in order.
        generation_id: The generation it published.
        ledger_files: How many ledgers the generation holds.
        ledger_records: How many records those ledgers hold.
        residual_document_bytes: The generation's own document size.
        generation_bytes: The whole published generation's size.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    journal_stages: tuple[str, ...]
    generation_id: str | None = None
    ledger_files: int | None = None
    ledger_records: int | None = None
    residual_document_bytes: int | None = None
    generation_bytes: int | None = None


class RerunEvidence(_StrictModel):
    """What re-applying the same approved plan did.

    Attributes:
        applied: Whether the rerun wrote anything.
        journal_rows: How many journal rows it appended.
        tree_unchanged: Whether the target is byte-identical to before.
        generation_id: The generation it resolved to.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    journal_rows: int
    tree_unchanged: bool
    generation_id: str | None = None


class RollbackEvidence(_StrictModel):
    """What taking the tree back to epoch 1 did.

    Attributes:
        outcome: What the recovery verb actually did.
        epoch: The epoch the tree reads from afterwards.
        generation_count: How many generations remain on disk.
        restored_locators: The authority surfaces written back.
        surfaces_match_restore_point: Whether every restored surface
            digests to what the restore point pinned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    epoch: int
    generation_count: int
    restored_locators: tuple[str, ...]
    surfaces_match_restore_point: bool


class RefusalEvidence(_StrictModel):
    """How a refused corpus refused, on each of its attempts.

    Attributes:
        plan_code: The code the read-only plan refused with.
        apply_code: The code the apply refused with.
        retry_code: The code a second apply refused with.
        message_names: The substrings the refusal named.
        target_unchanged: Whether the target tree is byte-identical to
            the tree before the first attempt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_code: str
    apply_code: str
    retry_code: str
    message_names: tuple[str, ...]
    target_unchanged: bool


class RehearsalEvidence(_StrictModel):
    """One corpus's committed rehearsal record.

    Attributes:
        fixture: The corpus name.
        disposition: Whether it imported or was refused.
        scrub_findings: How many concrete home-directory paths the
            staged corpus carried.
        source_bytes: The staged corpus's total size.
        dry_run: The dry-run leg, when one ran.
        apply: The apply leg, when one ran.
        rerun: The idempotent-rerun leg, when one ran.
        rollback: The rollback-rehearsal leg, when one ran.
        refusal: The refusal leg, for a refused corpus.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture: str
    disposition: RehearsalDisposition
    scrub_findings: int
    source_bytes: int | None = None
    dry_run: DryRunEvidence | None = None
    apply: ApplyEvidence | None = None
    rerun: RerunEvidence | None = None
    rollback: RollbackEvidence | None = None
    refusal: RefusalEvidence | None = None


class RehearsalFinding(_StrictModel):
    """One reason the rehearsal evidence falls short.

    Attributes:
        corpus: The corpus the finding is about, or ``""`` when the
            finding is about the evidence set as a whole.
        gap: Which gap fired.
        detail: One line naming what is missing or wrong.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus: str
    gap: RehearsalGap
    detail: str

    @property
    def is_absence(self) -> bool:
        """Return whether this finding is a missing producer rather than a failure."""
        return self.gap in ABSENCE_GAPS


def evidence_dir(repo_root: Path) -> Path:
    """Return the rehearsal evidence directory of the checkout at *repo_root*.

    Args:
        repo_root: Checkout the records were committed in.

    Returns:
        The directory the per-corpus records live in.
    """
    return repo_root.joinpath(*REHEARSAL_EVIDENCE_DIR)


def _read_record(path: Path) -> RehearsalEvidence:
    """Return the rehearsal record at *path*.

    Args:
        path: The record file.

    Returns:
        The validated record.

    Raises:
        ValueError: When the file is not valid JSON, or does not
            validate as a rehearsal record. A record that cannot be read
            is not an absent one, so it must not read as clean.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"rehearsal record {path.name!r} is not valid JSON: {exc}") from exc
    try:
        return RehearsalEvidence.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"rehearsal record {path.name!r} does not validate: "
            f"{exc.error_count()} error(s); first: {exc.errors()[0]['msg']}"
        ) from exc


def _importing_findings(record: RehearsalEvidence) -> list[RehearsalFinding]:
    """Return every gap in one importing corpus's four legs.

    Args:
        record: The corpus's record.

    Returns:
        One finding per absent or disagreeing leg, in leg order.
    """
    findings: list[RehearsalFinding] = []
    corpus = record.fixture
    present = {
        RehearsalLeg.DRY_RUN: record.dry_run,
        RehearsalLeg.APPLY: record.apply,
        RehearsalLeg.RERUN: record.rerun,
        RehearsalLeg.ROLLBACK: record.rollback,
    }
    for leg, evidence in present.items():
        if evidence is None:
            findings.append(
                RehearsalFinding(
                    corpus=corpus,
                    gap=RehearsalGap.MISSING_LEG,
                    detail=f"the {leg.value!r} leg was never rehearsed",
                )
            )
    if record.dry_run is not None and not record.dry_run.reproducible:
        findings.append(
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.LEG_FAILED,
                detail="two seals over the same bytes produced different manifest digests",
            )
        )
    if record.apply is not None:
        findings.extend(_apply_findings(corpus, record.apply))
    if record.rerun is not None:
        findings.extend(_rerun_findings(corpus, record.rerun))
    if record.rollback is not None:
        findings.extend(_rollback_findings(corpus, record.rollback))
    return findings


def _apply_findings(corpus: str, leg: ApplyEvidence) -> list[RehearsalFinding]:
    """Return the gaps in one apply leg.

    Args:
        corpus: The corpus the leg ran over.
        leg: The apply evidence.

    Returns:
        Findings for an apply that wrote nothing or journalled a
        different transaction than the cutover declares.
    """
    findings: list[RehearsalFinding] = []
    if not leg.applied:
        findings.append(
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.LEG_FAILED,
                detail="the apply published no generation",
            )
        )
    if leg.journal_stages != APPLY_JOURNAL_STAGES:
        findings.append(
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.LEG_FAILED,
                detail=(
                    f"the apply journalled {list(leg.journal_stages)} rather than the "
                    f"{len(APPLY_JOURNAL_STAGES)} stages one clean cutover records"
                ),
            )
        )
    return findings


def _rerun_findings(corpus: str, leg: RerunEvidence) -> list[RehearsalFinding]:
    """Return the gaps in one idempotent-rerun leg.

    Args:
        corpus: The corpus the leg ran over.
        leg: The rerun evidence.

    Returns:
        Findings for a rerun that wrote anything at all.
    """
    if not leg.applied and leg.journal_rows == 0 and leg.tree_unchanged:
        return []
    return [
        RehearsalFinding(
            corpus=corpus,
            gap=RehearsalGap.LEG_FAILED,
            detail=(
                f"re-applying the approved plan was not inert "
                f"(applied={leg.applied}, journal_rows={leg.journal_rows}, "
                f"tree_unchanged={leg.tree_unchanged})"
            ),
        )
    ]


def _rollback_findings(corpus: str, leg: RollbackEvidence) -> list[RehearsalFinding]:
    """Return the gaps in one rollback-rehearsal leg.

    Args:
        corpus: The corpus the leg ran over.
        leg: The rollback evidence.

    Returns:
        Findings for a rollback that left the tree on epoch 2, left a
        generation behind, restored nothing, or restored bytes that do
        not match the restore point.
    """
    failures: list[str] = []
    if leg.outcome != ROLLBACK_OUTCOME:
        failures.append(f"outcome={leg.outcome!r} rather than {ROLLBACK_OUTCOME!r}")
    if leg.epoch != ROLLED_BACK_EPOCH:
        failures.append(f"the tree reads from epoch {leg.epoch}")
    if leg.generation_count != 0:
        failures.append(f"{leg.generation_count} generation(s) remain on disk")
    if not leg.restored_locators:
        failures.append("no authority surface was restored")
    if not leg.surfaces_match_restore_point:
        failures.append("a restored surface does not digest to the restore point")
    if not failures:
        return []
    return [
        RehearsalFinding(
            corpus=corpus,
            gap=RehearsalGap.LEG_FAILED,
            detail=f"the rollback did not return the tree to epoch 1: {'; '.join(failures)}",
        )
    ]


def _refusing_findings(record: RehearsalEvidence) -> list[RehearsalFinding]:
    """Return every gap in one refusing corpus's rehearsed refusal.

    Args:
        record: The corpus's record.

    Returns:
        A missing-leg finding when the refusal was never rehearsed, and
        a failure finding when the two attempts refused differently, the
        refusal named nothing actionable, or the target was touched.
    """
    corpus = record.fixture
    leg = record.refusal
    if leg is None:
        return [
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.MISSING_LEG,
                detail="the refusal was never rehearsed",
            )
        ]
    failures: list[str] = []
    codes = {leg.plan_code, leg.apply_code, leg.retry_code}
    if len(codes) != 1:
        failures.append(f"the three attempts refused with {sorted(codes)} rather than one code")
    if not leg.message_names:
        failures.append("the refusal named no row an operator could open")
    if not leg.target_unchanged:
        failures.append("the refused attempt left bytes behind in the target")
    if not failures:
        return []
    return [
        RehearsalFinding(
            corpus=corpus,
            gap=RehearsalGap.LEG_FAILED,
            detail=f"the refusal is not reproducible and inert: {'; '.join(failures)}",
        )
    ]


def _corpus_findings(
    corpus: str,
    declared: RehearsalDisposition,
    path: Path,
) -> list[RehearsalFinding]:
    """Return every gap in one declared corpus's record.

    Args:
        corpus: The declared corpus name.
        declared: What the corpus is expected to do.
        path: Where its record should be.

    Returns:
        The findings, empty when the corpus is fully rehearsed.
    """
    if not path.is_file():
        return [
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.MISSING_RECORD,
                detail=f"no rehearsal record was committed at {path.name}",
            )
        ]
    try:
        record = _read_record(path)
    except ValueError as exc:
        return [
            RehearsalFinding(corpus=corpus, gap=RehearsalGap.UNREADABLE_RECORD, detail=str(exc))
        ]
    if record.disposition is not declared:
        return [
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.DISPOSITION_MISMATCH,
                detail=(
                    f"the record reports {record.disposition.value!r} but the corpus is "
                    f"declared {declared.value!r}"
                ),
            )
        ]
    findings: list[RehearsalFinding] = []
    if record.scrub_findings:
        findings.append(
            RehearsalFinding(
                corpus=corpus,
                gap=RehearsalGap.UNSCRUBBED_CORPUS,
                detail=f"the staged corpus carried {record.scrub_findings} concrete home path(s)",
            )
        )
    if declared is RehearsalDisposition.IMPORTS:
        findings.extend(_importing_findings(record))
    else:
        findings.extend(_refusing_findings(record))
    return findings


def rehearsal_findings(
    repo_root: Path,
    *,
    corpora: Mapping[str, RehearsalDisposition] = REHEARSED_CORPORA,
) -> tuple[RehearsalFinding, ...]:
    """Return every gap between the declared corpus set and the committed records.

    Args:
        repo_root: Checkout the rehearsal records were committed in.
        corpora: The corpus set to require, defaulting to
            :data:`REHEARSED_CORPORA`.

    Returns:
        The findings in declaration order; empty exactly when every
        declared corpus was rehearsed and every leg holds.

    Raises:
        TypeError: When *repo_root* is not a :class:`~pathlib.Path`.
    """
    if not isinstance(repo_root, Path):
        raise TypeError(f"repo_root must be Path; got {type(repo_root).__name__}")
    root = evidence_dir(repo_root)
    if not root.is_dir():
        return (
            RehearsalFinding(
                corpus="",
                gap=RehearsalGap.MISSING_EVIDENCE_DIR,
                detail=(
                    f"no cutover rehearsal records are committed under "
                    f"{'/'.join(REHEARSAL_EVIDENCE_DIR)}"
                ),
            ),
        )
    findings: list[RehearsalFinding] = []
    for corpus, declared in corpora.items():
        findings.extend(_corpus_findings(corpus, declared, root / f"{corpus}.json"))
    logger.info(
        f"rehearsal_findings corpora={len(corpora)} findings={len(findings)} "
        f"absences={sum(1 for finding in findings if finding.is_absence)}"
    )
    return tuple(findings)


def rehearsal_evidence_refs(
    corpora: Mapping[str, RehearsalDisposition] = REHEARSED_CORPORA,
) -> tuple[str, ...]:
    """Return one evidence reference per declared corpus.

    Args:
        corpora: The corpus set, defaulting to :data:`REHEARSED_CORPORA`.

    Returns:
        A ``rehearsal:<corpus>:<disposition>`` reference per corpus, in
        declaration order.
    """
    return tuple(f"rehearsal:{corpus}:{declared.value}" for corpus, declared in corpora.items())


def summarise(findings: Sequence[RehearsalFinding]) -> str:
    """Return a one-line operator-facing summary of *findings*.

    Args:
        findings: The gaps to summarise.

    Returns:
        A semicolon-joined ``<corpus>: <gap> -- <detail>`` list; empty
        when there is nothing to report.
    """
    return "; ".join(
        f"{finding.corpus or 'evidence set'}: {finding.gap.value} -- {finding.detail}"
        for finding in findings
    )


__all__ = [
    "ABSENCE_GAPS",
    "APPLY_JOURNAL_STAGES",
    "RECOVERY_STAGES",
    "REHEARSAL_EVIDENCE_DIR",
    "REHEARSED_CORPORA",
    "ApplyEvidence",
    "DryRunEvidence",
    "RefusalEvidence",
    "RehearsalDisposition",
    "RehearsalEvidence",
    "RehearsalFinding",
    "RehearsalGap",
    "RehearsalLeg",
    "RerunEvidence",
    "RollbackEvidence",
    "evidence_dir",
    "rehearsal_evidence_refs",
    "rehearsal_findings",
    "summarise",
]
