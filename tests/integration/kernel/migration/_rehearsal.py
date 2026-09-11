"""The four-leg rehearsal driver and the record each fixture leaves behind.

One rehearsal is four legs over one corpus, and the four are not variants
of each other. **Dry run** seals a plan and proves it is reproducible.
**Apply** turns that plan into a published generation inside a tree that
declared itself disposable. **Idempotent rerun** re-applies the same
approved plan and proves it writes nothing. **Rollback rehearsal** takes
the tree back to epoch 1 and proves every authority surface came back.

A corpus the importer refuses never reaches the second leg, so a refusing
fixture rehearses the refusal instead: the plan refuses with a code, the
apply refuses with the same code, a second attempt refuses identically,
and the target tree is byte-identical to the tree before the attempt. The
claim there is not "it failed" but "it failed the same way twice and left
nothing behind", which is the property an operator retrying a refused
import depends on.

Every leg is recorded, and the record is what the goldens pin. Recording
the legs rather than asserting inside them is what lets one apply serve
every assertion: the expensive work happens once per fixture and the
per-leg tests read the result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.migration.epoch2.apply import Epoch2ApplyRequest, apply_cutover
from eawf.kernel.migration.epoch2.canary import CANARY_DECLARATION_FILENAME, DisposableTarget
from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT, generation_ids
from eawf.kernel.migration.epoch2.journal import read_journal
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.migration.epoch2.recovery import (
    Epoch2RecoverRequest,
    RecoveryAction,
    recover_cutover,
)
from eawf.kernel.migration.epoch2.scrub import ScrubFinding, scan_staged_tree

logger = logging.getLogger(__name__)


TESTS_ROOT: Final = Path(__file__).resolve().parents[3]
REPO_ROOT: Final = TESTS_ROOT.parent
FIXTURES: Final = TESTS_ROOT / "fixtures" / "migration"
GOLDEN_ROOT: Final = TESTS_ROOT / "golden" / "kernel" / "migration" / "rehearsal"
ALLOWLIST: Final = FIXTURES / "allowed_legacy_symbols.txt"
CANARY_DECLARATION: Final = FIXTURES / "cutover-faults" / "canary" / CANARY_DECLARATION_FILENAME

#: Where the built corpora are frozen on disk. The builder stays the
#: source of truth; the committed tree is its output, checked back
#: against the builder so a hand edit cannot survive.
BUILT_CORPUS_ROOT: Final = FIXTURES / "rehearsal"

#: The two corpora taken from real project history rather than built.
HISTORICAL_CORPUS_ROOT: Final = FIXTURES / "p30-i26-history"
LIVE_CORPUS_ROOT: Final = FIXTURES / "live-cutover"

#: The seal clocks the rehearsal runs under. Fixed, because the manifest
#: digest a golden pins must not move between two runs of one corpus.
SEALED_AT: Final = datetime(2026, 1, 1, tzinfo=UTC)
REAPPLIED_AT: Final = datetime(2026, 2, 2, tzinfo=UTC)
ROLLED_BACK_AT: Final = datetime(2026, 3, 3, tzinfo=UTC)

#: The principal the rehearsal seals every plan under.
SEALED_BY: Final = "v07-rehearsal"

#: The environment variable that rewrites the goldens instead of
#: comparing against them.
REGEN_ENV_VAR: Final = "EAWF_REGEN_GOLDEN"

#: The authority surfaces the rehearsal seeds into every target tree, so a
#: rollback has bytes to put back rather than only digests to compare.
SEEDED_CONFIG: Final = "epoch: 1\nseeded_by: v07-rehearsal\n"
SEEDED_LEDGER: Final = '{"id": "AUD-SEED", "kind": "audit"}\n'
SEEDED_DOCUMENT: Final = '{"schema_version": "1.19"}\n'


class RehearsalDisposition(StrEnum):
    """Whether a fixture is expected to import or to be refused."""

    IMPORTS = "imports"
    REFUSES = "refuses"


class GoldenKind(StrEnum):
    """How much of a rehearsal record a golden may pin.

    ``PINNED`` is a corpus whose bytes are frozen, so its digests and its
    counts are part of the contract. ``SHAPE`` is a corpus that moves --
    the live one -- where pinning a digest would record the day the
    golden was written rather than a property of the cutover.
    """

    PINNED = "pinned"
    SHAPE = "shape"


class RehearsalFixture(BaseModel):
    """One corpus the rehearsal runs, and what it is expected to do.

    Attributes:
        name: The fixture name, which is also its golden filename and its
            parametrised test id.
        summary: What the corpus exercises, in one line.
        disposition: Whether the importer takes it or refuses it.
        golden_kind: How much of the record the golden pins.
        refusal_code: The stable failure code a refusing fixture raises,
            or ``None`` for an importing one.
        refusal_names: Substrings the refusal message has to carry. A
            code alone tells an operator what kind of failure it was; the
            names tell them which row to open.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    disposition: RehearsalDisposition
    golden_kind: GoldenKind = GoldenKind.PINNED
    refusal_code: str | None = None
    refusal_names: tuple[str, ...] = ()


class DryRunLeg(BaseModel):
    """What sealing a plan over the corpus produced.

    Attributes:
        source_digest: The digest naming the pinned corpus revision.
        manifest_digest: The manifest's digest over its own content.
        approval_digest: What an operator approves and an apply is
            checked against.
        reproducible: Whether a second seal over the same bytes produced
            the same manifest digest.
        source_rows: How many rows the source census counted.
        target_rows: How many rows the import would write.
        unresolved_rows: Every row the cutover cannot place, by address.
        collections: One ``source/target/unresolved`` triple per source
            collection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_digest: str
    manifest_digest: str
    approval_digest: str
    reproducible: bool
    source_rows: int
    target_rows: int
    unresolved_rows: tuple[str, ...]
    collections: dict[str, list[int]]


class ApplyLeg(BaseModel):
    """What the apply published.

    Attributes:
        applied: Whether the apply wrote a generation.
        generation_id: The generation it published.
        journal_stages: The stages it journalled, in order.
        ledger_files: How many ledgers the generation holds.
        ledger_records: How many records those ledgers hold in total.
        residual_document_bytes: The size of the generation's own
            document, which is what stays hot after compaction.
        generation_bytes: The whole published generation's size.
        wall_clock_s: How long the apply took, rounded to milliseconds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    generation_id: str
    journal_stages: tuple[str, ...]
    ledger_files: int
    ledger_records: int
    residual_document_bytes: int
    generation_bytes: int
    wall_clock_s: float


class RerunLeg(BaseModel):
    """What re-applying the same approved plan did.

    Attributes:
        applied: Whether the rerun wrote anything. Always ``False`` for a
            plan whose work is already published.
        journal_rows: How many journal rows it appended.
        generation_id: The generation it resolved to, which must be the
            one the first apply published.
        tree_unchanged: Whether every content file in the target is
            byte-identical to before the rerun.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    journal_rows: int
    generation_id: str
    tree_unchanged: bool


class RollbackLeg(BaseModel):
    """What taking the tree back to epoch 1 did.

    Attributes:
        outcome: What the recovery verb actually did.
        epoch: The epoch the tree reads from afterwards.
        generation_count: How many generations remain on disk.
        restored_locators: The authority surfaces written back or removed.
        surfaces_match_restore_point: Whether every restored surface
            digests to what the restore point pinned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    epoch: int
    generation_count: int
    restored_locators: tuple[str, ...]
    surfaces_match_restore_point: bool


class RefusalLeg(BaseModel):
    """How a refused corpus refused, on each of its two attempts.

    Attributes:
        plan_code: The code the read-only plan refused with.
        apply_code: The code the apply refused with, which must be the
            same one: an apply that refuses differently would send an
            operator to a different defect than the plan named.
        retry_code: The code a second apply refused with.
        message_names: The substrings the plan refusal named, filtered to
            the ones the fixture declared, so the golden records what an
            operator would actually be able to act on.
        target_unchanged: Whether the target tree is byte-identical to
            the tree before the first attempt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_code: str
    apply_code: str
    retry_code: str
    message_names: tuple[str, ...]
    target_unchanged: bool


class RehearsalRecord(BaseModel):
    """Everything the four legs observed for one fixture.

    Attributes:
        fixture: The fixture name.
        disposition: Whether it imported or was refused.
        scrub_findings: How many concrete home-directory paths the staged
            corpus carries. Always zero for a committed fixture: a corpus
            that carried one could not be committed at all.
        source_bytes: The staged corpus's total size, which is the
            denominator the published tree's multiplier is measured
            against.
        dry_run: The dry-run leg, or ``None`` when the corpus was
            refused before a plan existed.
        apply: The apply leg, when one ran.
        rerun: The idempotent-rerun leg, when one ran.
        rollback: The rollback-rehearsal leg, when one ran.
        refusal: The refusal leg, for a refused corpus.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture: str
    disposition: RehearsalDisposition
    scrub_findings: int
    source_bytes: int
    dry_run: DryRunLeg | None = None
    apply: ApplyLeg | None = None
    rerun: RerunLeg | None = None
    rollback: RollbackLeg | None = None
    refusal: RefusalLeg | None = None

    def golden_payload(self, *, kind: GoldenKind) -> dict[str, Any]:
        """Return the part of the record a golden of ``kind`` pins.

        Args:
            kind: How much of the record may be pinned.

        Returns:
            The record as JSON. A ``PINNED`` golden keeps everything
            except the wall clock, which is a property of the machine
            rather than of the cutover. A ``SHAPE`` golden additionally
            drops every digest, count and byte size, because the corpus
            behind it moves: what stays is the sequence of legs, which is
            the part that must not move.
        """
        payload: dict[str, Any] = json.loads(self.model_dump_json(exclude_none=True))
        if self.apply is not None:
            payload["apply"].pop("wall_clock_s", None)
        if kind is GoldenKind.PINNED:
            return payload
        payload.pop("source_bytes", None)
        for section, volatile in _SHAPE_VOLATILE_KEYS.items():
            if section in payload:
                for key in volatile:
                    payload[section].pop(key, None)
        return payload


#: The fields a ``SHAPE`` golden drops, section by section. They are the
#: numbers and digests that move with the corpus rather than with the
#: cutover's behaviour.
_SHAPE_VOLATILE_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "dry_run": (
        "source_digest",
        "manifest_digest",
        "approval_digest",
        "source_rows",
        "target_rows",
        "unresolved_rows",
        "collections",
    ),
    "apply": (
        "generation_id",
        "ledger_files",
        "ledger_records",
        "residual_document_bytes",
        "generation_bytes",
    ),
    "rerun": ("generation_id",),
    "rollback": (),
}


def plan_request_for(corpus: Path) -> Epoch2PlanRequest:
    """Return the plan request the rehearsal seals ``corpus`` under."""
    from tests.integration.kernel.migration._corpus_shapes import (
        PROJECT_KEY,
        REPOSITORY_KEY,
        WORKSPACE_KEY,
    )

    return Epoch2PlanRequest(
        snapshot_root=str(corpus),
        allowlist_path=str(ALLOWLIST),
        workspace_key=WORKSPACE_KEY,
        project_key=PROJECT_KEY,
        repository_key=REPOSITORY_KEY,
        sealed_by=SEALED_BY,
    )


def apply_request_for(
    *, corpus: Path, target_root: Path, plan: MigrationPlan
) -> Epoch2ApplyRequest:
    """Return the apply request for one corpus, target and approved plan."""
    return Epoch2ApplyRequest(
        plan_request=plan_request_for(corpus),
        target_root=str(target_root),
        registry_path=str(corpus / "registry.json"),
        plan_digest=plan.approval_digest,
        accepted_unresolved_rows=tuple(row.address for row in plan.manifest.unresolved_rows),
    )


def declared_canary(root: Path) -> Path:
    """Create ``root``, declare it disposable, and seed its surfaces.

    Args:
        root: The target tree's root.

    Returns:
        The root, carrying the declaration plus three of the five
        declared authority surfaces. Seeding matters: a rollback with no
        bytes to put back proves nothing about restoring a set.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANARY_DECLARATION, root / CANARY_DECLARATION_FILENAME)
    (root / "state.json").write_text(SEEDED_DOCUMENT, encoding="utf-8")
    (root / "config.yaml").write_text(SEEDED_CONFIG, encoding="utf-8")
    (root / "store").mkdir(exist_ok=True)
    (root / "store" / "audit.jsonl").write_text(SEEDED_LEDGER, encoding="utf-8")
    return root


def content_digests(root: Path) -> dict[str, str]:
    """Return a digest per content file under ``root``.

    Args:
        root: The tree to walk.

    Returns:
        One entry per regular file that is not a lock holder record. Lock
        files carry a pid and a heartbeat rather than content, and every
        acquisition rewrites them.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def scrub_corpus(corpus: Path) -> tuple[ScrubFinding, ...]:
    """Return every concrete home-directory path a staged corpus carries.

    Args:
        corpus: The staged snapshot root.

    Returns:
        The findings over the document and every store ledger, which is
        the same scan the cutover runs before it writes.
    """
    return scan_staged_tree(
        corpus / "document.json",
        ledger_paths=sorted((corpus / "store").glob("*.jsonl")),
    )


def _directory_bytes(root: Path) -> int:
    """Return the total size of every regular file under ``root``."""
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _collection_triples(plan: MigrationPlan) -> dict[str, list[int]]:
    """Return the source, target and unresolved counts per collection.

    A document-metadata key holds no container to count, so its source
    count is reported as zero rather than as null: the triple is a count
    of rows, and a key that carries none carries none.
    """
    return {
        mapping.source_collection: [
            mapping.source_row_count or 0,
            mapping.target_row_count,
            mapping.unresolved_row_count,
        ]
        for mapping in plan.manifest.row_mappings
    }


def _dry_run(corpus: Path) -> tuple[MigrationPlan, DryRunLeg]:
    """Seal the plan twice and record whether the two agreed."""
    first = plan_cutover(plan_request_for(corpus), sealed_at=SEALED_AT)
    second = plan_cutover(plan_request_for(corpus), sealed_at=REAPPLIED_AT)
    return first, DryRunLeg(
        source_digest=first.manifest.source_digest,
        manifest_digest=first.manifest.manifest_digest,
        approval_digest=first.approval_digest,
        reproducible=second.manifest.manifest_digest == first.manifest.manifest_digest,
        source_rows=sum(mapping.source_row_count or 0 for mapping in first.manifest.row_mappings),
        target_rows=sum(mapping.target_row_count for mapping in first.manifest.row_mappings),
        unresolved_rows=tuple(sorted(row.address for row in first.manifest.unresolved_rows)),
        collections=_collection_triples(first),
    )


def _apply(*, corpus: Path, target_root: Path, plan: MigrationPlan) -> ApplyLeg:
    """Run the apply and record what it published."""
    started = time.perf_counter()
    result = apply_cutover(
        apply_request_for(corpus=corpus, target_root=target_root, plan=plan),
        applied_at=SEALED_AT,
    )
    elapsed = time.perf_counter() - started
    target = DisposableTarget.require(target_root)
    generation = target.generation_path(result.generation_id)
    ledgers = sorted((generation / "ledger").iterdir()) if (generation / "ledger").is_dir() else []
    return ApplyLeg(
        applied=result.applied,
        generation_id=result.generation_id,
        journal_stages=tuple(row.stage.value for row in read_journal(target.journal_path)),
        ledger_files=len(ledgers),
        ledger_records=sum(len(path.read_text("utf-8").splitlines()) for path in ledgers),
        residual_document_bytes=(generation / GENERATION_DOCUMENT).stat().st_size,
        generation_bytes=_directory_bytes(generation),
        wall_clock_s=round(elapsed, 3),
    )


def _rerun(*, corpus: Path, target_root: Path) -> RerunLeg:
    """Re-apply the approved plan and record that it wrote nothing."""
    before = content_digests(target_root)
    plan = plan_cutover(plan_request_for(corpus), sealed_at=REAPPLIED_AT)
    result = apply_cutover(
        apply_request_for(corpus=corpus, target_root=target_root, plan=plan),
        applied_at=REAPPLIED_AT,
    )
    return RerunLeg(
        applied=result.applied,
        journal_rows=result.journal_rows,
        generation_id=result.generation_id,
        tree_unchanged=content_digests(target_root) == before,
    )


def _rollback(target_root: Path) -> RollbackLeg:
    """Take the tree back to epoch 1 and record what came back."""
    from eawf.kernel.migration.epoch2.apply import ABSENT_SURFACE
    from eawf.kernel.migration.epoch2.restore import read_restore_manifest

    target = DisposableTarget.require(target_root)
    pinned = read_restore_manifest(target.restore_manifest_path)
    result = recover_cutover(
        Epoch2RecoverRequest(target_root=str(target_root), action=RecoveryAction.ROLLBACK),
        recovered_at=ROLLED_BACK_AT,
    )
    matched = all(
        _surface_digest(target_root / surface.locator, absent=ABSENT_SURFACE) == surface.digest
        for surface in pinned.restorable_surfaces()
    )
    return RollbackLeg(
        outcome=result.outcome.value,
        epoch=result.authority.epoch,
        generation_count=len(generation_ids(target)),
        restored_locators=tuple(sorted(result.restored_locators)),
        surfaces_match_restore_point=matched,
    )


def _surface_digest(path: Path, *, absent: str) -> str:
    """Return a surface's digest, or the absent sentinel when it is gone."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else absent


def _refusal(*, corpus: Path, target_root: Path, names: Sequence[str]) -> RefusalLeg:
    """Rehearse a refused corpus: refuse, refuse again, leave nothing.

    Args:
        corpus: The staged snapshot root.
        target_root: The fence-cleared target the apply would write into.
        names: The substrings the fixture declares the refusal must name.

    Returns:
        The refusal leg.

    Raises:
        AssertionError: When a leg that must refuse did not, which makes
            the negative fixture vacuous.
    """
    before = content_digests(target_root)
    plan_error = _refuse(lambda: plan_cutover(plan_request_for(corpus), sealed_at=SEALED_AT))
    apply_error = _refuse(lambda: _apply_without_a_plan(corpus=corpus, target_root=target_root))
    retry_error = _refuse(lambda: _apply_without_a_plan(corpus=corpus, target_root=target_root))
    message = str(plan_error)
    return RefusalLeg(
        plan_code=plan_error.code,
        apply_code=apply_error.code,
        retry_code=retry_error.code,
        message_names=tuple(name for name in names if name in message),
        target_unchanged=content_digests(target_root) == before,
    )


def _apply_without_a_plan(*, corpus: Path, target_root: Path) -> None:
    """Drive the apply over a corpus whose plan cannot be sealed.

    A refused corpus has no approval digest, because sealing is what
    refuses. The apply is therefore driven with a syntactically valid
    digest that names no plan: the apply has to re-plan before it can
    compare, so it refuses for the corpus reason rather than for the
    digest.

    Args:
        corpus: The staged snapshot root.
        target_root: The fence-cleared target tree.

    Raises:
        MigrationRuleError: Always, for whatever the corpus is refused
            for.
    """
    apply_cutover(
        Epoch2ApplyRequest(
            plan_request=plan_request_for(corpus),
            target_root=str(target_root),
            registry_path=str(corpus / "registry.json"),
            plan_digest="0" * 64,
        ),
        applied_at=SEALED_AT,
    )


def _refuse(call: Callable[[], object]) -> MigrationRuleError:
    """Run ``call`` and return the typed refusal it raised.

    Args:
        call: A zero-argument callable expected to refuse.

    Returns:
        The refusal.

    Raises:
        AssertionError: When the call did not refuse, or refused with
            something other than a typed importer failure.
    """
    try:
        call()
    except MigrationRuleError as error:
        return error
    raise AssertionError("the corpus was expected to refuse and did not")


def rehearse(*, fixture: RehearsalFixture, corpus: Path, root: Path) -> RehearsalRecord:
    """Run all four legs over one corpus and return the record.

    Args:
        fixture: What the corpus is and what it is expected to do.
        corpus: The staged snapshot root.
        root: A directory to build the target tree under.

    Returns:
        The record every per-leg assertion and the golden read from.

    Raises:
        AssertionError: When a refusing fixture did not refuse.
    """
    target_root = declared_canary(root / ".ea")
    findings = scrub_corpus(corpus)
    source_bytes = _directory_bytes(corpus)
    if fixture.disposition is RehearsalDisposition.REFUSES:
        return RehearsalRecord(
            fixture=fixture.name,
            disposition=fixture.disposition,
            scrub_findings=len(findings),
            source_bytes=source_bytes,
            refusal=_refusal(corpus=corpus, target_root=target_root, names=fixture.refusal_names),
        )
    plan, dry_run = _dry_run(corpus)
    apply_leg = _apply(corpus=corpus, target_root=target_root, plan=plan)
    rerun = _rerun(corpus=corpus, target_root=target_root)
    rollback = _rollback(target_root)
    return RehearsalRecord(
        fixture=fixture.name,
        disposition=fixture.disposition,
        scrub_findings=len(findings),
        source_bytes=source_bytes,
        dry_run=dry_run,
        apply=apply_leg,
        rerun=rerun,
        rollback=rollback,
    )


def golden_path(name: str) -> Path:
    """Return where the golden for fixture ``name`` lives."""
    return GOLDEN_ROOT / f"{name}.json"


def compare_or_regenerate(*, fixture: RehearsalFixture, record: RehearsalRecord) -> None:
    """Compare the record against its golden, or rewrite it on request.

    Args:
        fixture: The fixture the record belongs to.
        record: What the four legs observed.

    Raises:
        AssertionError: When the record and the golden disagree.
    """
    payload = record.golden_payload(kind=fixture.golden_kind)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path = golden_path(fixture.name)
    if os.environ.get(REGEN_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return
    assert path.is_file(), (
        f"{fixture.name} has no recorded manifest golden at "
        f"{path.relative_to(TESTS_ROOT).as_posix()}"
    )
    assert json.loads(path.read_text(encoding="utf-8")) == payload, (
        f"{fixture.name} no longer rehearses the way its golden records; "
        f"re-run with {REGEN_ENV_VAR}=1 once the change is intended"
    )


__all__ = [
    "ALLOWLIST",
    "BUILT_CORPUS_ROOT",
    "CANARY_DECLARATION",
    "FIXTURES",
    "GOLDEN_ROOT",
    "HISTORICAL_CORPUS_ROOT",
    "LIVE_CORPUS_ROOT",
    "REGEN_ENV_VAR",
    "REPO_ROOT",
    "SEALED_AT",
    "SEALED_BY",
    "TESTS_ROOT",
    "ApplyLeg",
    "DryRunLeg",
    "GoldenKind",
    "RefusalLeg",
    "RehearsalDisposition",
    "RehearsalFixture",
    "RehearsalRecord",
    "RerunLeg",
    "RollbackLeg",
    "apply_request_for",
    "compare_or_regenerate",
    "content_digests",
    "declared_canary",
    "golden_path",
    "plan_request_for",
    "rehearse",
    "scrub_corpus",
]
