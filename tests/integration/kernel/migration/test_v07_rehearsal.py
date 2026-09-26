"""The whole apply, rehearsed end to end over the full-shape corpus.

What is asserted here is the transaction, not its pieces. One apply runs
over a fresh copy of ``epoch1-full`` into a tree that has declared itself
disposable, and the claims are that it pinned every authority surface
before writing, re-censused the corpus under the authority locks against
the digest the operator approved, built a generation twice and compared
the two byte for byte, read the published one back through the public
readers, and then selected it with the epoch marker written last.

Two properties get their own attention because they are the ones a
careless implementation loses.

**The marker is last.** A generation that is selected but not marked is a
tree that still reads as epoch 1, which is the state a crash in the
window must leave behind. The ordering is proved by making the marker
write fail and observing that the selection survived and the marker did
not -- not by reading timestamps, which prove only that two writes
happened in some order.

**A second apply writes nothing.** The generation is named by the
manifest digest, so a re-run of an approved plan recognises its own work
and declines. Every file in the tree is compared before and after, and
the only paths allowed to move are the lock holder records -- which carry
a pid and a heartbeat rather than content, and which the commit policy
already declares uncommitted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.apply import (
    ABSENT_SURFACE,
    CutoverResult,
    Epoch2ApplyRequest,
    apply_cutover,
    apply_envelope,
    authority_snapshot,
)
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    TARGET_AUTHORITY_LOCATORS,
    DisposableTarget,
)
from eawf.kernel.migration.epoch2.dispositions import Disposition
from eawf.kernel.migration.epoch2.errors import (
    MigrationSourceUnreadableError,
    MigrationStagingRefusedError,
)
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    generation_id_for,
    generation_ids,
    read_marker,
    read_selection,
)
from eawf.kernel.migration.epoch2.journal import CutoverStage, read_journal, require_chain_intact
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.migration.epoch2.restore import read_restore_manifest
from eawf.kernel.migration.epoch2.scrub import scan_text
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot, repository_revision
from eawf.kernel.store.commit_policy import EA_PATH_CLASSES, CommitPolicy
from eawf.kernel.store.compaction import read_document
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._corpus_builders import CorpusPlan
from tests.integration.kernel.migration._corpus_shapes import (
    DEFAULT_TRACK_KEY,
    INTERRUPTED_CLOSE_SESSION,
    MISSING_ITER_ID,
    STRUCTURAL_CORPORA,
    UNKNOWN_EXTENSION_FIELD,
)
from tests.integration.kernel.migration._historical_freeze import (
    BACKFILLED_COLLECTIONS,
    PHASE_BATCH_POINTER,
    SLICE_ITER_ID,
    SLICE_PHASE_ID,
    SOURCE_REVISION,
    row_counts,
)
from tests.integration.kernel.migration._live_corpus import (
    LIVE_PROJECT_KEY,
    LIVE_REPOSITORY_KEY,
    LIVE_STORE_LOCATOR,
    LIVE_TRACK_KEY,
    LIVE_WORKSPACE_KEY,
    PIN_FILENAME,
    CorpusMagnitude,
    LiveCorpusPin,
    clone_at_revision,
    committed_sources,
    is_committed,
    magnitude_for,
    quiesce_clone,
    register_live_workspace,
    stage_live_corpus,
    write_opt_in,
)
from tests.integration.kernel.migration._rehearsal import (
    HISTORICAL_CORPUS_ROOT,
    LIVE_CORPUS_ROOT,
    REPO_ROOT,
    RehearsalFixture,
    RehearsalRecord,
    compare_or_regenerate,
    rehearse,
    scrub_corpus,
)
from tests.integration.kernel.migration._rehearsal import (
    SEALED_AT as REHEARSAL_SEALED_AT,
)
from tests.integration.kernel.migration._rehearsal import (
    plan_request_for as rehearsal_plan_request,
)
from tests.integration.kernel.migration._rehearsal_set import (
    IMPORTING_FIXTURES,
    LARGEST_FIXTURE,
    REFUSING_FIXTURES,
    REHEARSAL_FIXTURE_INDEX,
    REHEARSAL_FIXTURES,
    committed_snapshot,
    stage_corpus,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"
FAULTS = FIXTURES / "cutover-faults"
CANARY_DECLARATION = FAULTS / "canary" / CANARY_DECLARATION_FILENAME
REGISTRY = FAULTS / "registry" / "registry.json"

WORKSPACE_KEY = "WSP-DEFAULT"
PROJECT_KEY = "PRJ-DEMO"
REPOSITORY_KEY = "REP-DEMO"
SEALED_BY = "rehearsal-test"
APPLIED_AT = datetime(2026, 1, 1, tzinfo=UTC)
REAPPLIED_AT = datetime(2026, 2, 2, tzinfo=UTC)

runner = CliRunner()

#: The rows the pinned corpus cannot place, pinned so a converter that
#: stops resolving one of them reds here rather than silently widening the
#: set an apply is allowed to wave through. Every collection of the pinned
#: corpus converts, so the waiver is empty.
EXPECTED_UNRESOLVED: tuple[str, ...] = ()

#: What the pinned corpus leaves in the published generation. Pinned rather
#: than derived from the manifest, so an apply that builds a thinner tree
#: and a manifest that agrees with it reds here instead of agreeing with
#: itself.
EXPECTED_LEDGER_RECORDS = 514
EXPECTED_TARGET_ROWS = 520
EXPECTED_LEDGER_FILES = 11

#: The stages one clean apply records, in order.
EXPECTED_STAGES = (
    CutoverStage.FENCE_CLEARED,
    CutoverStage.WORKSPACE_RESOLVED,
    CutoverStage.AUTHORITY_LOCKED,
    CutoverStage.QUIESCENCE_PROVED,
    CutoverStage.RECENSUS_MATCHED,
    CutoverStage.MAINTENANCE_ENTERED,
    CutoverStage.SNAPSHOT_TAKEN,
    CutoverStage.GENERATION_BUILT,
    CutoverStage.READ_SMOKE_PASSED,
    CutoverStage.GENERATION_SELECTED,
    CutoverStage.MARKER_WRITTEN,
    CutoverStage.MAINTENANCE_EXITED,
)


def plan_over(corpus: Path) -> MigrationPlan:
    """Return one sealed plan over ``corpus`` under the shared test seal."""
    return plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)


def plan_request_for(corpus: Path) -> Epoch2PlanRequest:
    """Return the plan request the apply carries for ``corpus``."""
    return Epoch2PlanRequest(
        snapshot_root=str(corpus),
        allowlist_path=str(ALLOWLIST),
        workspace_key=WORKSPACE_KEY,
        project_key=PROJECT_KEY,
        repository_key=REPOSITORY_KEY,
        sealed_by=SEALED_BY,
        default_track_key=DEFAULT_TRACK_KEY,
    )


def declared_canary(root: Path) -> Path:
    """Create ``root`` and declare it a disposable canary."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANARY_DECLARATION, root / CANARY_DECLARATION_FILENAME)
    return root


def staged_corpus(root: Path) -> Path:
    """Return a fresh copy of the pinned corpus under ``root``."""
    corpus = root / "staged"
    shutil.copytree(FULL_SNAPSHOT, corpus)
    return corpus


def apply_request_for(
    *, corpus: Path, target_root: Path, plan: MigrationPlan, accepted: tuple[str, ...]
) -> Epoch2ApplyRequest:
    """Return the apply request for one corpus, target and approved plan."""
    return Epoch2ApplyRequest(
        plan_request=plan_request_for(corpus),
        target_root=str(target_root),
        registry_path=str(REGISTRY),
        plan_digest=plan.approval_digest,
        accepted_unresolved_rows=accepted,
    )


def content_digests(root: Path) -> dict[str, str]:
    """Return a digest per content file under ``root``.

    Args:
        root: The tree to walk.

    Returns:
        One entry per regular file that is not a lock holder record,
        keyed by relative POSIX path. Lock files are excluded because
        they carry a pid and a heartbeat rather than content: every
        acquisition rewrites them, and the commit policy already declares
        them uncommitted.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, CutoverResult]:
    """One apply over a fresh corpus copy, run once for the module.

    Returns:
        ``(corpus, target_root, result)`` for the completed apply.
    """
    root = tmp_path_factory.mktemp("apply_rehearsal")
    corpus = staged_corpus(root)
    target_root = declared_canary(root / ".ea")
    plan = plan_over(corpus)
    result = apply_cutover(
        apply_request_for(
            corpus=corpus,
            target_root=target_root,
            plan=plan,
            accepted=tuple(row.address for row in plan.manifest.unresolved_rows),
        ),
        applied_at=APPLIED_AT,
    )
    return corpus, target_root, result


def test_apply_accounts_for_every_row_the_corpus_cannot_place(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The waiver the apply ran under names exactly the pinned open rows."""
    _corpus, _target_root, result = applied

    assert tuple(sorted(result.accepted_unresolved_rows)) == EXPECTED_UNRESOLVED


def test_apply_builds_a_generation_named_by_its_manifest_digest(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The generation is addressed by the manifest, not by a clock."""
    _corpus, target_root, result = applied
    target = DisposableTarget.require(target_root)

    assert result.applied is True
    assert result.generation_id == generation_id_for(result.manifest_digest)
    assert generation_ids(target) == (result.generation_id,)
    assert result.generation_count == 1


def test_apply_selects_the_generation_and_marks_the_epoch(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """Both halves of the select name the one generation that was built."""
    _corpus, target_root, result = applied
    target = DisposableTarget.require(target_root)

    selection = read_selection(target)
    marker = read_marker(target)
    assert selection is not None
    assert marker is not None
    assert selection.generation_id == result.generation_id
    assert selection.manifest_digest == result.manifest_digest
    assert selection.approval_digest == result.approval_digest
    assert marker.epoch == 2
    assert marker.generation_id == result.generation_id
    assert result.rollback_boundary is RollbackBoundary.MARKER_WRITTEN


def test_apply_leaves_a_generation_that_reads_back_as_epoch_two(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The published generation is a complete tree, not a staging leftover."""
    _corpus, target_root, result = applied
    target = DisposableTarget.require(target_root)
    state_path = target.generation_path(result.generation_id) / GENERATION_DOCUMENT

    document = read_document(state_path)
    assert document["schema_version"] == "2"
    assert (state_path.parent / "ledger").is_dir()
    assert (state_path.parent / "indexes").is_dir()


def test_apply_publishes_the_whole_corpus_into_the_generation_ledgers(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The published tree holds the whole import, not a truncated slice."""
    _corpus, target_root, result = applied
    target = DisposableTarget.require(target_root)
    generation = target.generation_path(result.generation_id)

    ledgers = sorted((generation / "ledger").iterdir())
    assert len(ledgers) == EXPECTED_LEDGER_FILES
    assert sum(len(path.read_text("utf-8").splitlines()) for path in ledgers) == (
        EXPECTED_LEDGER_RECORDS
    )
    assert result.target_rows == EXPECTED_TARGET_ROWS
    assert len(sorted((generation / "indexes").iterdir())) == EXPECTED_LEDGER_FILES


def test_apply_journals_every_stage_in_order_and_the_chain_verifies(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The journal is the ordered, tamper-evident record of the transaction."""
    _corpus, target_root, result = applied
    target = DisposableTarget.require(target_root)

    rows = read_journal(target.journal_path)
    require_chain_intact(rows)
    assert tuple(row.stage for row in rows) == EXPECTED_STAGES
    assert result.journal_rows == len(EXPECTED_STAGES)


def test_apply_journals_the_select_before_the_marker(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The journal runs ahead of the tree, so the marker row comes last."""
    _corpus, target_root, _result = applied
    target = DisposableTarget.require(target_root)

    stages = [row.stage for row in read_journal(target.journal_path)]
    assert stages.index(CutoverStage.GENERATION_SELECTED) < stages.index(
        CutoverStage.MARKER_WRITTEN
    )
    boundaries = {row.stage: row.boundary for row in read_journal(target.journal_path)}
    assert boundaries[CutoverStage.GENERATION_SELECTED] is RollbackBoundary.GENERATION_SELECTED
    assert boundaries[CutoverStage.MARKER_WRITTEN] is RollbackBoundary.MARKER_WRITTEN


def test_apply_closes_the_maintenance_window_it_opened(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """The window is transient: a finished apply leaves no closed tree."""
    _corpus, target_root, _result = applied
    target = DisposableTarget.require(target_root)

    assert not target.maintenance_path.exists()


def test_apply_pins_a_digest_for_every_authority_surface(tmp_path: Path) -> None:
    """The restore point covers every declared surface, absent ones included."""
    target = DisposableTarget.require(declared_canary(tmp_path / ".ea"))
    (target.root / "state.json").write_text("{}\n", encoding="utf-8")

    backup = authority_snapshot(target, registry_path=REGISTRY, taken_at=APPLIED_AT)

    locators = [entry.split("@", 1)[0] for entry in backup.surfaces]
    assert locators == sorted([*TARGET_AUTHORITY_LOCATORS, "registry.json"])
    assert (
        sum(entry.endswith(ABSENT_SURFACE) for entry in backup.surfaces)
        == len(TARGET_AUTHORITY_LOCATORS) - 1
    )
    assert any(entry.startswith("state.json@sha256:") for entry in backup.surfaces)


def test_apply_a_second_time_writes_nothing_and_keeps_the_manifest_digest(
    applied: tuple[Path, Path, CutoverResult],
) -> None:
    """Re-running an approved plan recognises its own work and declines."""
    corpus, target_root, first = applied
    before = content_digests(target_root)

    plan = plan_over(corpus)
    second = apply_cutover(
        apply_request_for(
            corpus=corpus,
            target_root=target_root,
            plan=plan,
            accepted=tuple(row.address for row in plan.manifest.unresolved_rows),
        ),
        applied_at=REAPPLIED_AT,
    )

    assert second.applied is False
    assert second.journal_rows == 0
    assert second.manifest_digest == first.manifest_digest
    assert second.generation_id == first.generation_id
    assert content_digests(target_root) == before


def test_apply_envelope_reports_the_idempotent_run_as_already_selected() -> None:
    """The wire envelope distinguishes a fresh apply from a declined one."""
    result = CutoverResult(
        applied=False,
        generation_id="gen-0123456789abcdef",
        manifest_digest="a" * 64,
        approval_digest="b" * 64,
        rollback_boundary=RollbackBoundary.MARKER_WRITTEN,
        journal_rows=0,
        target_rows=7,
        generation_count=1,
        accepted_unresolved_rows=(),
    )

    envelope = apply_envelope(result)

    assert envelope["status"] == "already-selected"
    assert envelope["journal_rows"] == 0
    assert envelope["generation_id"] == "gen-0123456789abcdef"


def test_apply_writes_the_marker_after_the_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed marker write leaves a selected generation and no marker.

    This is the ordering proof. If the marker were written first, or the
    two writes were folded into one step, a marker failure would leave no
    selection behind -- and the crash window the whole design rests on
    would not exist.
    """
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")
    plan = plan_over(corpus)

    def _refuse(**_kwargs: object) -> None:
        raise OSError("the marker write failed")

    monkeypatch.setattr("eawf.kernel.migration.epoch2.apply.write_marker", _refuse)

    with pytest.raises(OSError, match="the marker write failed"):
        apply_cutover(
            apply_request_for(
                corpus=corpus,
                target_root=target_root,
                plan=plan,
                accepted=tuple(row.address for row in plan.manifest.unresolved_rows),
            ),
            applied_at=APPLIED_AT,
        )

    target = DisposableTarget.require(target_root)
    selection = read_selection(target)
    assert selection is not None
    assert read_marker(target) is None
    assert generation_ids(target) == (selection.generation_id,)
    assert not target.maintenance_path.exists()

    monkeypatch.undo()
    resumed = apply_cutover(
        apply_request_for(
            corpus=corpus,
            target_root=target_root,
            plan=plan,
            accepted=tuple(row.address for row in plan.manifest.unresolved_rows),
        ),
        applied_at=REAPPLIED_AT,
    )

    assert resumed.applied is True
    assert resumed.generation_id == selection.generation_id
    assert resumed.generation_count == 1
    marker = read_marker(target)
    assert marker is not None
    assert marker.manifest_digest == selection.manifest_digest


def test_apply_through_the_cli_selects_a_generation(tmp_path: Path) -> None:
    """The operator surface drives the same transaction as the library.

    A CLI that computed a different plan than the daemon verifies, or that
    wrote a generation the library would not have, would make the approval
    digest meaningless -- so the operator route is asserted end to end
    rather than trusted to be a thin wrapper.
    """
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")
    plan = plan_over(corpus)
    accepted: list[str] = []
    for row in plan.manifest.unresolved_rows:
        accepted += ["--accept-unresolved", row.address]

    result = runner.invoke(
        app,
        [
            "--json",
            "migrate",
            "epoch2",
            "--apply",
            "--snapshot-root",
            str(corpus),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            WORKSPACE_KEY,
            "--project-key",
            PROJECT_KEY,
            "--repository-key",
            REPOSITORY_KEY,
            "--default-track-key",
            DEFAULT_TRACK_KEY,
            "--target-root",
            str(target_root),
            "--registry-path",
            str(REGISTRY),
            "--plan-digest",
            plan.approval_digest,
            *accepted,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "applied"
    assert payload["approval_digest"] == plan.approval_digest
    target = DisposableTarget.require(target_root)
    selection = read_selection(target)
    assert selection is not None
    assert selection.generation_id == payload["generation_id"]
    assert read_marker(target) is not None


def test_apply_through_the_cli_refuses_without_a_plan_digest(tmp_path: Path) -> None:
    """There is no default approval, so the apply cannot run unapproved."""
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    result = runner.invoke(
        app,
        [
            "migrate",
            "epoch2",
            "--apply",
            "--snapshot-root",
            str(corpus),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            WORKSPACE_KEY,
            "--project-key",
            PROJECT_KEY,
            "--repository-key",
            REPOSITORY_KEY,
            "--target-root",
            str(target_root),
            "--registry-path",
            str(REGISTRY),
        ],
    )

    assert result.exit_code != 0
    assert "--apply requires --plan-digest" in result.output
    assert not (target_root / "generations").exists()


def test_apply_through_the_cli_refuses_two_modes_at_once(tmp_path: Path) -> None:
    """A run is never both a read and a write, so the pair refuses."""
    corpus = staged_corpus(tmp_path)

    result = runner.invoke(
        app,
        ["migrate", "epoch2", "--plan", "--apply", "--snapshot-root", str(corpus)],
    )

    assert result.exit_code != 0
    assert "got 2" in result.output


# ---------------------------------------------------------------------------
# The ten-fixture rehearsal.
#
# Everything above rehearses one corpus in depth. What follows rehearses the
# whole required set in breadth: ten corpora, four legs each, one recorded
# manifest per corpus. The legs are separate tests over a cached record rather
# than one test that asserts everything, so a failure names the fixture *and*
# the leg -- "apply[p30-i26-history]" is a defect report; "the rehearsal
# failed" is not.
# ---------------------------------------------------------------------------


#: The ten fixtures the migration signal is required to compute over. Pinned
#: as a literal rather than derived from the registry, so deleting a fixture
#: reds here instead of silently shrinking the set every other test iterates.
REQUIRED_FIXTURE_NAMES = (
    "empty-repository",
    "minimal-active",
    "all-terminal",
    "interrupted-close",
    "open-attention",
    "multi-root-workspace",
    "corrupt-reference",
    "unknown-extension-field",
    "p30-i26-history",
    "largest-supported-state",
)

#: The stages a clean apply journals, in order. The same tuple the deep
#: rehearsal above pins, reused so one corpus and ten corpora cannot disagree
#: about what a complete transaction looks like.
EXPECTED_STAGE_VALUES = tuple(stage.value for stage in EXPECTED_STAGES)


@pytest.fixture(scope="module")
def rehearsed(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[RehearsalFixture], RehearsalRecord]:
    """Return a cached, lazy runner for one fixture's four legs.

    Lazy because the two large corpora cost seconds and a ``-k`` selector
    that names neither should not pay for them. Cached because the four
    legs are one transaction: running them once and asserting over the
    record is what keeps ten fixtures inside a quick gate's budget.
    """
    cache: dict[str, RehearsalRecord] = {}

    def run(fixture: RehearsalFixture) -> RehearsalRecord:
        if fixture.name not in cache:
            root = tmp_path_factory.mktemp(fixture.name.replace("-", "_"))
            corpus = stage_corpus(fixture=fixture, root=root, repo_root=REPO_ROOT)
            cache[fixture.name] = rehearse(fixture=fixture, corpus=corpus, root=root)
        return cache[fixture.name]

    return run


def test_the_rehearsal_covers_exactly_the_required_fixture_set() -> None:
    """The set is the claim: a missing corpus is a hole in the signal."""
    assert tuple(fixture.name for fixture in REHEARSAL_FIXTURES) == REQUIRED_FIXTURE_NAMES
    assert len(IMPORTING_FIXTURES) + len(REFUSING_FIXTURES) == len(REQUIRED_FIXTURE_NAMES)


@pytest.mark.parametrize("fixture", REHEARSAL_FIXTURES, ids=lambda row: row.name)
def test_staged_corpus_carries_no_concrete_home_directory_path(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """Every corpus passes the cutover's own scrub gate before it is used.

    The gate runs here rather than only at commit time because a
    committed fixture is invisible to the repository's leak lints the
    moment it stops being decodable text, and because the live corpus is
    never committed at all.
    """
    assert rehearsed(fixture).scrub_findings == 0


@pytest.mark.parametrize("fixture", IMPORTING_FIXTURES, ids=lambda row: row.name)
def test_dry_run_seals_a_plan_that_two_reads_agree_on(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """Leg one: the plan is addressed by the corpus, not by the clock."""
    dry_run = rehearsed(fixture).dry_run
    assert dry_run is not None
    assert dry_run.reproducible is True
    assert dry_run.manifest_digest != dry_run.approval_digest
    assert dry_run.target_rows >= 0


@pytest.mark.parametrize("fixture", IMPORTING_FIXTURES, ids=lambda row: row.name)
def test_apply_publishes_one_generation_and_journals_every_stage(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """Leg two: the corpus lands as a complete, selected, marked generation."""
    record = rehearsed(fixture)
    assert record.apply is not None
    assert record.apply.applied is True
    assert record.apply.journal_stages == EXPECTED_STAGE_VALUES
    assert record.apply.generation_id.startswith("gen-")


@pytest.mark.parametrize("fixture", IMPORTING_FIXTURES, ids=lambda row: row.name)
def test_idempotent_rerun_recognises_its_own_work_and_writes_nothing(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """Leg three: a re-run of an approved plan declines, byte for byte."""
    record = rehearsed(fixture)
    assert record.apply is not None
    assert record.rerun is not None
    assert record.rerun.applied is False
    assert record.rerun.journal_rows == 0
    assert record.rerun.generation_id == record.apply.generation_id
    assert record.rerun.tree_unchanged is True


@pytest.mark.parametrize("fixture", IMPORTING_FIXTURES, ids=lambda row: row.name)
def test_rollback_rehearsal_puts_the_tree_back_at_epoch_one(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """Leg four: the activation is reversible until a native write lands."""
    rollback = rehearsed(fixture).rollback
    assert rollback is not None
    assert rollback.outcome == "surfaces_restored"
    assert rollback.epoch == 1
    assert rollback.generation_count == 0
    assert rollback.surfaces_match_restore_point is True


@pytest.mark.parametrize("fixture", REHEARSAL_FIXTURES, ids=lambda row: row.name)
def test_rehearsal_matches_the_manifest_recorded_as_its_golden(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """The four legs are recorded, so a silent change of behaviour reds."""
    compare_or_regenerate(fixture=fixture, record=rehearsed(fixture))


# ---------------------------------------------------------------------------
# Negative fixtures.
#
# A corpus the importer refuses has no four legs, so it rehearses the refusal
# instead: refuse, refuse the same way again, and leave the target byte-
# identical. The third negative is different in kind -- an interrupted close
# is not refused, it is *imported*, and what has to be true is that the
# attempt arrives as a record that grants nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", REFUSING_FIXTURES, ids=lambda row: row.name)
def test_negative_fixture_refuses_with_its_declared_code(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """The plan, the apply and the retry all refuse with one stable code."""
    refusal = rehearsed(fixture).refusal
    assert refusal is not None
    assert refusal.plan_code == fixture.refusal_code
    assert refusal.apply_code == fixture.refusal_code
    assert refusal.retry_code == fixture.refusal_code


@pytest.mark.parametrize("fixture", REFUSING_FIXTURES, ids=lambda row: row.name)
def test_negative_fixture_names_what_an_operator_has_to_open(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """A code says what kind of defect; the names say which row holds it."""
    refusal = rehearsed(fixture).refusal
    assert refusal is not None
    assert refusal.message_names == fixture.refusal_names


@pytest.mark.parametrize("fixture", REFUSING_FIXTURES, ids=lambda row: row.name)
def test_negative_fixture_leaves_the_target_tree_untouched(
    fixture: RehearsalFixture, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """A refused import is a no-op, so a retry starts from where it started."""
    refusal = rehearsed(fixture).refusal
    assert refusal is not None
    assert refusal.target_unchanged is True


def test_negative_corrupt_reference_names_the_reference_that_resolves_to_nothing(
    rehearsed: Callable[[RehearsalFixture], RehearsalRecord],
) -> None:
    """The refusal locates the dangling edge as ``row.field -> referent``.

    The reference is unresolved rather than mis-typed: the source really
    did record it, and the importer will not invent the record it names,
    so the whole import stops instead of writing a Task whose batch does
    not exist.
    """
    fixture = REHEARSAL_FIXTURE_INDEX["corrupt-reference"]
    refusal = rehearsed(fixture).refusal
    assert refusal is not None
    assert refusal.plan_code == "migration_fabrication_detected"
    assert refusal.message_names == ("waves/P01-I01-W02.batch_ref", MISSING_ITER_ID)


def test_negative_unknown_extension_field_names_the_undeclared_field(
    rehearsed: Callable[[RehearsalFixture], RehearsalRecord],
) -> None:
    """Strict validation reds on the key itself, before a row is read."""
    fixture = REHEARSAL_FIXTURE_INDEX["unknown-extension-field"]
    refusal = rehearsed(fixture).refusal
    assert refusal is not None
    assert refusal.plan_code == "migration_collection_unknown"
    assert refusal.message_names == (UNKNOWN_EXTENSION_FIELD,)


def test_negative_interrupted_close_imports_the_attempt_as_a_legacy_record(
    tmp_path: Path,
) -> None:
    """The close that never finished arrives as a record that grants nothing.

    Two halves. The session that was closing the wave imports as an
    immutable legacy envelope: the row is preserved whole and mints no
    live epoch-2 record, which is the only honest import of a claim whose
    outcome the source never recorded. The ``close_attempts`` row itself
    reaches the cutover's explicit-drop arm, so it is accounted for
    rather than silently skipped -- a skipped row is one the target
    census could never reconcile against the source.
    """
    fixture = REHEARSAL_FIXTURE_INDEX["interrupted-close"]
    corpus = stage_corpus(fixture=fixture, root=tmp_path, repo_root=REPO_ROOT)
    snapshot = SourceSnapshot.read(corpus)
    import_plan = CorpusImportPlan.build(snapshot=snapshot, allowlist_path=ALLOWLIST)

    envelopes = {
        (row.source_collection, row.source_id): row for row in import_plan.envelopes.envelopes
    }
    attempt = envelopes[("agent_sessions", INTERRUPTED_CLOSE_SESSION)]
    assert attempt.minted_records == ()
    assert attempt.alias == f"legacy:agent_sessions/{INTERRUPTED_CLOSE_SESSION}"
    assert attempt.payload["status"] == "open"

    plan = plan_cutover(rehearsal_plan_request(corpus), sealed_at=REHEARSAL_SEALED_AT)
    dropped = next(
        mapping
        for mapping in plan.manifest.row_mappings
        if mapping.source_collection == "close_attempts"
    )
    assert dropped.disposition is Disposition.EXPLICIT_DROP
    assert dropped.source_row_count == 1
    assert dropped.target_row_count == 0
    assert dropped.unresolved_row_count == 0


# ---------------------------------------------------------------------------
# The largest supported state.
# ---------------------------------------------------------------------------


#: The macOS home anchor, split so this file carries no substring the
#: scrub scanner's own pattern can match.
MACOS_HOME_ANCHOR = "/Users" + "/"

#: One valid ledger row, so a built corpus has something to scan.
SEEDED_AUDIT_ROW = '{"id": "AUD-1", "kind": "audit"}\n'


@pytest.fixture(scope="module")
def largest_pin() -> LiveCorpusPin:
    """Return the committed contract the live corpus has to keep satisfying."""
    return LiveCorpusPin.load(LIVE_CORPUS_ROOT / PIN_FILENAME)


def test_largest_supported_state_is_the_live_corpus_at_the_declared_magnitude(
    largest_pin: LiveCorpusPin, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """The magnitude is asserted over the corpus that was actually imported.

    Asserting an order of magnitude rather than a row count is what lets
    the live corpus grow without loosening the claim: a measurement taken
    over four thousand rows still backs a statement about thousands, and
    one taken over four hundred does not.

    The magnitude is the one assertion here that a growing corpus
    eventually reds, and that is deliberate. Crossing ten thousand rows
    means the rehearsal now backs a *stronger* claim than the one
    declared, and re-declaring it is the point at which someone
    re-derives the ceilings over the bigger population instead of
    inheriting numbers measured over a smaller one.
    """
    record = rehearsed(LARGEST_FIXTURE)
    assert record.dry_run is not None
    observed = magnitude_for(record.dry_run.source_rows)
    assert observed is largest_pin.declared_magnitude, (
        f"the live corpus censuses {record.dry_run.source_rows} rows, which is magnitude "
        f"{observed.value}, not the {largest_pin.declared_magnitude.value} the pin declares; "
        f"re-measure the corpus and re-declare declared_magnitude in {PIN_FILENAME}"
    )
    assert largest_pin.declared_magnitude is CorpusMagnitude.THOUSANDS, (
        f"the rehearsal's published claim is a thousands-row cutover; raising "
        f"declared_magnitude in {PIN_FILENAME} means raising it here too"
    )


def test_largest_supported_state_stays_under_the_recorded_multiplier_ceiling(
    largest_pin: LiveCorpusPin, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """The published tree may grow against its source, but only so far.

    The multiplier is the whole generation over the whole staged corpus.
    An importer change that widened every ledger row, or that stopped
    compacting, would show up here as a number the recorded ceiling does
    not admit -- before the flag day rather than during it.

    What is asserted is the ceiling, not the pin's own observation. The
    corpus behind the ratio is live: matching a recorded number, to any
    tolerance, would be a gate that reds on the calendar rather than on a
    defect, and widening the tolerance until it stopped would leave a
    gate that reds on nothing at all.
    """
    record = rehearsed(LARGEST_FIXTURE)
    assert record.apply is not None
    multiplier = record.apply.generation_bytes / record.source_bytes

    assert multiplier <= largest_pin.multiplier_ceiling, (
        f"the published generation is {multiplier:.3f}x the {record.source_bytes}-byte corpus "
        f"it was built from, over the {largest_pin.multiplier_ceiling}x ceiling; either the "
        f"importer stopped compacting or the ceiling has to be re-derived from a fresh "
        f"measurement in {PIN_FILENAME}"
    )


def test_largest_supported_state_applies_inside_its_declared_budget(
    largest_pin: LiveCorpusPin, rehearsed: Callable[[RehearsalFixture], RehearsalRecord]
) -> None:
    """One apply finishes in time and leaves a residual document a reader can hold.

    Both numbers are budgets rather than observations. The apply has to
    fit inside the wall clock an operator is asked to hold a maintenance
    window open for, and the document left hot after the terminal records
    move to their ledgers has to stay small enough that a reader does not
    pay for history it is not looking at.
    """
    record = rehearsed(LARGEST_FIXTURE)
    assert record.apply is not None
    assert record.apply.applied is True
    assert record.apply.wall_clock_s <= largest_pin.apply_timeout_budget_s, (
        f"the apply took {record.apply.wall_clock_s}s, over the "
        f"{largest_pin.apply_timeout_budget_s}s budget the pin records"
    )
    assert record.apply.residual_document_bytes < largest_pin.residual_document_ceiling_bytes, (
        f"the residual document is {record.apply.residual_document_bytes} bytes, over the "
        f"{largest_pin.residual_document_ceiling_bytes}-byte ceiling; compaction is no longer "
        f"moving terminal records out of the document"
    )
    assert record.apply.ledger_records > 0


def test_largest_supported_state_pin_records_what_was_observed(
    largest_pin: LiveCorpusPin,
) -> None:
    """The pin's ceilings sit above the numbers it says were measured.

    A ceiling below its own observation is a gate that was green when it
    was written and can never be green again, which is the specific way a
    budget stops meaning anything.
    """
    assert largest_pin.observed_multiplier < largest_pin.multiplier_ceiling
    assert largest_pin.observed_apply_s < largest_pin.apply_timeout_budget_s
    assert largest_pin.observed_residual_bytes < largest_pin.residual_document_ceiling_bytes
    assert magnitude_for(largest_pin.observed_rows) is largest_pin.declared_magnitude


def test_the_live_corpus_stages_only_paths_the_commit_policy_carries() -> None:
    """The staged surface is the committed one, decided by the policy table.

    A migration operates on the state a clone can reproduce. Deriving
    that set from
    :data:`~eawf.kernel.store.commit_policy.EA_PATH_CLASSES` rather than
    from an exclusion list kept alongside it is what stops the fixture
    and the policy from disagreeing about which files those are.
    """
    top, revision = repository_revision(REPO_ROOT)
    sources = committed_sources(top, revision=revision)
    uncommitted = [source.locator for source in sources if not is_committed(source.locator)]
    tracked = _git(top, "ls-tree", "--name-only", revision, f"{LIVE_STORE_LOCATOR}/").split()

    assert sources, "the live corpus staged nothing, so the rehearsal would be vacuous"
    assert uncommitted == [], f"staged paths no clone carries: {uncommitted}"
    assert [source.locator for source in sources[:2]] == [".ea/state.json", ".ea/config.yaml"]
    assert {source.locator for source in sources[2:]} == {
        locator for locator in tracked if locator.endswith(".jsonl") and is_committed(locator)
    }


def _git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _commit_tree(repo: Path) -> None:
    """Make ``repo`` a git repository whose HEAD holds every file in it."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=rehearsal",
        "-c",
        "user.email=rehearsal@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "corpus",
    )


def _built_repo(root: Path) -> Path:
    """Return a committed repository carrying a minimal ``.ea`` corpus."""
    repo = root / "repo"
    (repo / ".ea" / "store").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text('{"schema_version": "1.19"}\n', encoding="utf-8")
    (repo / ".ea" / "config.yaml").write_text("epoch: 1\n", encoding="utf-8")
    (repo / ".ea" / "store" / "audit.jsonl").write_text(SEEDED_AUDIT_ROW, encoding="utf-8")
    _commit_tree(repo)
    return repo


def _seed_uncommitted_families(repo_root: Path, *, leak: str) -> tuple[str, ...]:
    """Write one representative file per uncommitted ``.ea/`` family.

    The families come from the commit policy's own probe paths, so a row
    added to the table is seeded here without anyone editing this test.

    Args:
        repo_root: The directory holding the synthetic ``.ea`` tree.
        leak: A concrete home path to write into the firehose, so the
            staged tree has something real to have carried.

    Returns:
        The repo-relative paths written, in table order.
    """
    written: list[str] = []
    for row in EA_PATH_CLASSES:
        if row.policy is not CommitPolicy.NOT_COMMITTED or not row.probe.startswith(".ea/"):
            continue
        path = repo_root / row.probe
        path.parent.mkdir(parents=True, exist_ok=True)
        leaks = row.probe.endswith("event.jsonl")
        body = json.dumps({"id": "EV-1", "text": leak}) + "\n" if leaks else "seed\n"
        path.write_text(body, encoding="utf-8")
        written.append(row.probe)
    return tuple(written)


def test_the_staged_tree_drops_every_family_the_commit_policy_excludes(tmp_path: Path) -> None:
    """The staging keeps the committed surface and nothing else.

    Driven over a built tree rather than the repository's own, because
    every excluded family is gitignored: a checkout that has never run an
    agent carries no firehose, no telemetry database and no locks, so a
    gate that only reds on a developer's machine is not a gate. The tree
    here carries all of them, the firehose carries a concrete home path,
    and the staged result has to hold the committed five and scan clean.
    """
    ea_root = tmp_path / "repo" / ".ea"
    leak = f"{MACOS_HOME_ANCHOR}devuser/Workspace/eawf"
    (ea_root / "store").mkdir(parents=True)
    (ea_root / "state.json").write_text('{"schema_version": "1.19"}\n', encoding="utf-8")
    (ea_root / "config.yaml").write_text("epoch: 1\n", encoding="utf-8")
    (ea_root / "store" / "audit.jsonl").write_text(SEEDED_AUDIT_ROW, encoding="utf-8")
    excluded = _seed_uncommitted_families(ea_root.parent, leak=leak)
    _commit_tree(ea_root.parent)
    # An uncommitted edit is exactly what the staging must not see.
    (ea_root / "state.json").write_text('{"schema_version": "9.99"}\n', encoding="utf-8")

    staged = stage_live_corpus(repo_root=ea_root.parent, destination=tmp_path / "staged")
    names = sorted(
        path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()
    )

    assert scan_text(locator=f"{LIVE_STORE_LOCATOR}/event.jsonl", text=leak), (
        "the seeded firehose carries no reportable home path, so the scan proves nothing"
    )
    assert f"{LIVE_STORE_LOCATOR}/event.jsonl" in excluded
    assert ".ea/telemetry.db" in excluded
    assert names == [
        "config/base.yaml",
        "document.json",
        "registry.json",
        "store/audit.jsonl",
        "telemetry.json",
    ]
    assert scrub_corpus(staged) == ()
    assert (staged / "document.json").read_text(encoding="utf-8") == (
        '{"schema_version": "1.19"}\n'
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Return every file under ``root`` keyed by its root-relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stage_to(repo: Path, destination: Path) -> Result:
    """Invoke ``eawf migrate epoch2 --stage-to --json`` over ``repo``."""
    return runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "migrate",
            "epoch2",
            "--stage-to",
            str(destination),
            "--workspace-key",
            WORKSPACE_KEY,
            "--project-key",
            PROJECT_KEY,
        ],
    )


def test_stage_to_matches_stage_live_corpus_byte_for_byte(tmp_path: Path) -> None:
    """The operator verb and the rehearsal stage one snapshot, byte for byte.

    Driven over this repository's own committed corpus, which is the tree
    the operator stages at the flag day.
    """
    helper = stage_live_corpus(repo_root=REPO_ROOT, destination=tmp_path / "helper")
    result = _stage_to(REPO_ROOT, tmp_path / "verb")

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["revision"] == repository_revision(REPO_ROOT)[1]
    assert envelope["sources"][:2] == [".ea/state.json", ".ea/config.yaml"]
    verb = _tree_bytes(tmp_path / "verb")
    assert verb == _tree_bytes(helper)
    assert {"document.json", "registry.json", "telemetry.json", "config/base.yaml"} <= set(verb)


def test_stage_to_stages_a_corpus_with_no_ledgers(tmp_path: Path) -> None:
    """A committed store with no ledger is staged as an empty store, not refused."""
    repo = _built_repo(tmp_path)
    (repo / ".ea" / "store" / "audit.jsonl").unlink()
    (repo / ".ea" / "store" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=rehearsal",
        "-c",
        "user.email=rehearsal@example.invalid",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "drop",
    )

    result = _stage_to(repo, tmp_path / "staged")

    assert result.exit_code == 0, result.output
    assert sorted(_tree_bytes(tmp_path / "staged")) == [
        "config/base.yaml",
        "document.json",
        "registry.json",
        "telemetry.json",
    ]
    assert (tmp_path / "staged" / "store").is_dir()


@pytest.mark.parametrize(
    "option",
    ["--workspace-key", "--project-key"],
)
def test_stage_to_refuses_a_malformed_key_as_a_typed_usage_error(
    option: str, tmp_path: Path
) -> None:
    """A malformed addressing key exits 2 with a typed envelope and writes nothing."""
    repo = _built_repo(tmp_path)
    args = {"--workspace-key": WORKSPACE_KEY, "--project-key": PROJECT_KEY, option: "wsp bad"}
    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "migrate",
            "epoch2",
            "--stage-to",
            str(tmp_path / "staged"),
            *(part for pair in args.items() for part in pair),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    envelope = json.loads(result.output)
    assert envelope["exit_name"] == "VALIDATION_ERROR"
    assert "symbol_key_invalid" in envelope["message"]
    assert not (tmp_path / "staged").exists()


def test_plan_refuses_a_malformed_workspace_key_as_a_typed_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed key is refused at the CLI boundary, not deep in the importer."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    corpus = tmp_path / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, corpus)
    result = runner.invoke(
        app,
        [
            "migrate",
            "epoch2",
            "--plan",
            "--snapshot-root",
            str(corpus),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            "wsp-default",
            "--project-key",
            PROJECT_KEY,
            "--repository-key",
            REPOSITORY_KEY,
        ],
    )

    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "--workspace-key: symbol_key_invalid" in result.output


def test_stage_to_refuses_a_destination_inside_the_live_tree(tmp_path: Path) -> None:
    """The staging never writes under the ``.ea`` tree it reads."""
    repo = _built_repo(tmp_path)
    before = _tree_bytes(repo / ".ea")

    result = _stage_to(repo, repo / ".ea" / "staged")

    assert result.exit_code == 2, result.output
    assert "migration_staging_refused" in result.output
    assert _tree_bytes(repo / ".ea") == before


def test_stage_committed_corpus_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    """A leftover file would be pinned as corpus, so the staging refuses it."""
    repo = _built_repo(tmp_path)
    destination = tmp_path / "staged"
    destination.mkdir()
    (destination / "stray.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(MigrationStagingRefusedError, match="already holds files"):
        stage_live_corpus(repo_root=repo, destination=destination)


def test_stage_committed_corpus_refuses_a_tree_outside_git(tmp_path: Path) -> None:
    """Without a revision there is no committed corpus to stage."""
    (tmp_path / "loose" / ".ea").mkdir(parents=True)

    with pytest.raises(MigrationSourceUnreadableError, match="rev-parse"):
        stage_live_corpus(repo_root=tmp_path / "loose", destination=tmp_path / "staged")


def test_stage_committed_corpus_refuses_a_revision_without_a_document(tmp_path: Path) -> None:
    """A revision that tracks no ``.ea/state.json`` is a broken corpus, not an empty one."""
    repo = _built_repo(tmp_path)
    _git(repo, "rm", "-q", ".ea/state.json")
    _git(
        repo,
        "-c",
        "user.name=rehearsal",
        "-c",
        "user.email=rehearsal@example.invalid",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "drop document",
    )

    with pytest.raises(MigrationSourceUnreadableError, match="not tracked"):
        stage_live_corpus(repo_root=repo, destination=tmp_path / "staged")


# ---------------------------------------------------------------------------
# Fixture provenance.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("corpus", STRUCTURAL_CORPORA, ids=lambda row: row.name)
def test_built_corpus_is_exactly_what_its_typed_builder_emits(
    corpus: CorpusPlan, tmp_path: Path
) -> None:
    """The builder is the source of truth; the committed tree is its output.

    Regenerating into a temporary directory and comparing bytes is what
    makes that true rather than aspirational: a hand edit to the
    committed JSON reds here, and so does a builder change nobody
    re-emitted.
    """
    regenerated = corpus.write(tmp_path / "snapshot")
    committed = committed_snapshot(corpus.name)

    assert content_digests(regenerated) == content_digests(committed)


def test_frozen_historical_corpus_agrees_with_its_recorded_provenance() -> None:
    """The frozen slice says what it dropped, and the tree agrees with it."""
    provenance = json.loads(
        (HISTORICAL_CORPUS_ROOT / "provenance.json").read_text(encoding="utf-8")
    )
    document = json.loads(
        (HISTORICAL_CORPUS_ROOT / "snapshot" / "document.json").read_text(encoding="utf-8")
    )

    assert provenance["source_revision"] == SOURCE_REVISION
    assert provenance["scrub_findings"] == 0
    assert provenance["row_counts_frozen"] == row_counts(document)
    assert provenance["slice"] == {"phase_id": SLICE_PHASE_ID, "iter_id": SLICE_ITER_ID}
    for collection in BACKFILLED_COLLECTIONS:
        assert document[collection] == {}
    assert PHASE_BATCH_POINTER not in document["phases"][SLICE_PHASE_ID]
    assert set(document["iters"]) == {SLICE_ITER_ID}
    assert set(document["iters"][SLICE_ITER_ID]["wave_ids"]) == set(document["waves"])


# ---------------------------------------------------------------------------
# The pinned clone.
#
# Everything above imports this repository's corpus into a scratch target.
# What follows cuts the repository itself over -- a clone of it, pinned at
# one revision -- through the operator verbs the live cut runs, in the order
# the runbook runs them: back up, opt in, stage, plan, apply, apply again,
# roll back. The target is the clone's own ``.ea``, so the fence, the
# quiescence probe and the restore point all meet the tree they will meet on
# the day, not a seeded stand-in.
# ---------------------------------------------------------------------------

#: The allowlist the live cut plans under, read from the clone so it is
#: pinned at the same revision as the corpus.
LIVE_ALLOWLIST_LOCATOR = "tests/fixtures/migration/allowed_legacy_symbols.txt"

#: The collection whose conversion is declared but not implemented, and the
#: one row the negative corpus adds to it.
UNCONVERTED_COLLECTION = "hypotheses"
UNCONVERTED_ROW = "H01-01"


@dataclass(frozen=True)
class CloneRehearsal:
    """What one pinned-clone rehearsal recorded, leg by leg.

    Attributes:
        home: The user-scope home the backup was written under.
        revision: The commit the clone is pinned at.
        clone: The clone's repository root.
        staged: The snapshot root ``--stage-to`` assembled.
        registry: The registry the apply resolved the workspace in.
        stage: The staging envelope.
        plan: The plan envelope.
        apply: The first apply's envelope.
        rerun: The second apply's envelope.
        tree_before_rerun: A digest per content file before the second apply.
        tree_after_rerun: The same digests after it.
        restore_point: The digest the restore point pinned per restorable
            surface.
        rollback: The rollback envelope.
        boundary_after_rollback: The boundary report once rolled back.
        surfaces_after_rollback: The digest per restorable surface once
            rolled back, ``absent`` for one that is not on disk.
    """

    home: Path
    revision: str
    clone: Path
    staged: Path
    registry: Path
    stage: dict[str, Any]
    plan: dict[str, Any]
    apply: dict[str, Any]
    rerun: dict[str, Any]
    tree_before_rerun: dict[str, str]
    tree_after_rerun: dict[str, str]
    restore_point: dict[str, str]
    rollback: dict[str, Any]
    boundary_after_rollback: dict[str, Any]
    surfaces_after_rollback: dict[str, str]

    @property
    def ea_root(self) -> Path:
        """The clone's ``.ea`` tree, which is the cutover's target."""
        return self.clone / ".ea"


def _eawf(*argv: str) -> dict[str, Any]:
    """Run one ``eawf --json`` command and return its envelope."""
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.output)
    return payload


def _corpus_argv(*, staged: Path, allowlist: Path) -> list[str]:
    """Return the corpus and addressing options the live cut plans with."""
    return [
        "--snapshot-root",
        str(staged),
        "--allowlist",
        str(allowlist),
        "--workspace-key",
        LIVE_WORKSPACE_KEY,
        "--project-key",
        LIVE_PROJECT_KEY,
        "--repository-key",
        LIVE_REPOSITORY_KEY,
        "--default-track-key",
        LIVE_TRACK_KEY,
    ]


def _apply_argv(*, corpus: list[str], ea_root: Path, registry: Path, digest: str) -> list[str]:
    """Return the full ``migrate epoch2 --apply`` argv for one approved plan."""
    return [
        "migrate",
        "epoch2",
        "--apply",
        *corpus,
        "--target-root",
        str(ea_root),
        "--registry-path",
        str(registry),
        "--plan-digest",
        digest,
    ]


def _surface_digests(ea_root: Path, locators: tuple[str, ...]) -> dict[str, str]:
    """Return the digest per surface, ``absent`` for one not on disk."""
    return {
        locator: hashlib.sha256((ea_root / locator).read_bytes()).hexdigest()
        if (ea_root / locator).is_file()
        else ABSENT_SURFACE
        for locator in locators
    }


@pytest.fixture(scope="module")
def clone_rehearsal(tmp_path_factory: pytest.TempPathFactory) -> CloneRehearsal:
    """Cut a pinned clone of this repository over, once for the module.

    Returns:
        The record of every leg. The legs are one transaction -- a re-run
        and a rollback only mean something after the apply they follow --
        so they run once and the tests assert over the record.
    """
    root = tmp_path_factory.mktemp("pinned_clone")
    clone = root / "eawf-rehearsal"
    home = root / "home"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("EAWF_HOME", str(home))
        patch.setenv("EAWF_DAEMONLESS", "1")
        revision = clone_at_revision(repo_root=REPO_ROOT, destination=clone)
        ea_root = clone / ".ea"
        quiesce_clone(clone, booted_at=datetime.now(UTC))
        backup = _eawf("-w", str(clone), "backup", "create", "--note", "pre epoch-2 cutover")
        write_opt_in(ea_root, backup_ts=backup["ts"], backup_digest=backup["digest"])
        registry = register_live_workspace(root / "registry.json")
        staged = root / "staged"
        stage = _eawf(
            "-w",
            str(clone),
            "migrate",
            "epoch2",
            "--stage-to",
            str(staged),
            "--workspace-key",
            LIVE_WORKSPACE_KEY,
            "--project-key",
            LIVE_PROJECT_KEY,
        )
        corpus = _corpus_argv(staged=staged, allowlist=clone / LIVE_ALLOWLIST_LOCATOR)
        plan = _eawf("migrate", "epoch2", "--plan", *corpus)
        apply_argv = _apply_argv(
            corpus=corpus, ea_root=ea_root, registry=registry, digest=plan["approval_digest"]
        )
        applied = _eawf(*apply_argv)
        before = content_digests(ea_root)
        rerun = _eawf(*apply_argv)
        after = content_digests(ea_root)
        manifest = read_restore_manifest(DisposableTarget.require(ea_root).restore_manifest_path)
        restorable = tuple(surface.locator for surface in manifest.surfaces if surface.restorable)
        rollback = _eawf("migrate", "epoch2", "--rollback", "--target-root", str(ea_root))
        boundary = _eawf("migrate", "epoch2", "--rollback-boundary", "--target-root", str(ea_root))
    return CloneRehearsal(
        home=home,
        revision=revision,
        clone=clone,
        staged=staged,
        registry=registry,
        stage=stage,
        plan=plan,
        apply=applied,
        rerun=rerun,
        tree_before_rerun=before,
        tree_after_rerun=after,
        restore_point={
            surface.locator: surface.digest for surface in manifest.surfaces if surface.restorable
        },
        rollback=rollback,
        boundary_after_rollback=boundary,
        surfaces_after_rollback=_surface_digests(ea_root, restorable),
    )


def test_pinned_clone_rehearsal_stages_the_revision_it_pinned(
    clone_rehearsal: CloneRehearsal,
) -> None:
    """The corpus is the clone's committed tree at the one pinned commit."""
    head = _git(clone_rehearsal.clone, "rev-parse", "HEAD").strip()

    assert head == clone_rehearsal.revision
    assert clone_rehearsal.stage["revision"] == clone_rehearsal.revision
    assert clone_rehearsal.stage["sources"][:2] == [".ea/state.json", ".ea/config.yaml"]


def test_pinned_clone_rehearsal_plans_an_applicable_cutover(
    clone_rehearsal: CloneRehearsal,
) -> None:
    """Every row of the real corpus has a target, so the plan needs no waiver."""
    plan = clone_rehearsal.plan

    assert plan["applicable"] is True
    assert plan["unresolved_row_count"] == 0
    assert plan["required_operator_assignment_count"] == 0
    assert plan["target_rows"] > 0
    assert plan["manifest_digest"] != plan["approval_digest"]


def test_pinned_clone_rehearsal_apply_selects_the_planned_manifest(
    clone_rehearsal: CloneRehearsal,
) -> None:
    """The apply builds exactly the generation the approved plan named."""
    plan, applied = clone_rehearsal.plan, clone_rehearsal.apply
    target = DisposableTarget.require(clone_rehearsal.ea_root)

    assert applied["status"] == "applied"
    assert applied["manifest_digest"] == plan["manifest_digest"]
    assert applied["approval_digest"] == plan["approval_digest"]
    assert applied["generation_id"] == generation_id_for(plan["manifest_digest"])
    assert applied["journal_rows"] == len(EXPECTED_STAGE_VALUES)
    assert applied["rollback_boundary"] == RollbackBoundary.MARKER_WRITTEN.value
    assert applied["accepted_unresolved_rows"] == []
    assert applied["target_rows"] == plan["target_rows"]
    require_chain_intact(read_journal(target.journal_path))


def test_pinned_clone_rehearsal_second_apply_writes_nothing_and_keeps_the_manifest(
    clone_rehearsal: CloneRehearsal,
) -> None:
    """A re-run of the approved plan recognises its own work, byte for byte."""
    applied, rerun = clone_rehearsal.apply, clone_rehearsal.rerun

    assert rerun["status"] == "already-selected"
    assert rerun["applied"] is False
    assert rerun["journal_rows"] == 0
    assert rerun["manifest_digest"] == applied["manifest_digest"]
    assert rerun["generation_id"] == applied["generation_id"]
    assert rerun["generation_count"] == 1
    assert clone_rehearsal.tree_after_rerun == clone_rehearsal.tree_before_rerun


def test_pinned_clone_rehearsal_rollback_restores_the_restore_point(
    clone_rehearsal: CloneRehearsal,
) -> None:
    """Before any native write, the whole activation is reversible."""
    rollback = clone_rehearsal.rollback
    boundary = clone_rehearsal.boundary_after_rollback

    assert rollback["outcome"] == "surfaces_restored"
    assert rollback["authority"] == {"epoch": 1, "generation_count": 0, "generation_id": None}
    assert rollback["manifest_digest"] == clone_rehearsal.apply["manifest_digest"]
    assert clone_rehearsal.surfaces_after_rollback == clone_rehearsal.restore_point
    assert boundary["boundary"] == RollbackBoundary.PLAN_ONLY.value
    assert boundary["generation_id"] is None
    assert boundary["declaration"] == "opt_in"


def test_pinned_clone_rehearsal_refuses_a_corpus_with_one_unconverted_row(
    clone_rehearsal: CloneRehearsal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row with no epoch-2 target turns the plan inapplicable and the apply away.

    The row is added to the clone's own staged corpus, so the only
    difference from the applicable plan above is that one row.
    """
    monkeypatch.setenv("EAWF_HOME", str(clone_rehearsal.home))
    corpus = tmp_path / "staged"
    shutil.copytree(clone_rehearsal.staged, corpus)
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document[UNCONVERTED_COLLECTION] = {UNCONVERTED_ROW: {"id": UNCONVERTED_ROW}}
    document_path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    argv = _corpus_argv(staged=corpus, allowlist=clone_rehearsal.clone / LIVE_ALLOWLIST_LOCATOR)

    plan = _eawf("migrate", "epoch2", "--plan", *argv)
    before = content_digests(clone_rehearsal.ea_root)
    refused = runner.invoke(
        app,
        [
            "--json",
            *_apply_argv(
                corpus=argv,
                ea_root=clone_rehearsal.ea_root,
                registry=clone_rehearsal.registry,
                digest=plan["approval_digest"],
            ),
        ],
    )

    assert plan["applicable"] is False
    assert plan["unresolved_row_count"] == 1
    assert refused.exit_code == 2, refused.output
    assert "migration_plan_not_applicable" in refused.output
    assert UNCONVERTED_ROW in refused.output
    assert content_digests(clone_rehearsal.ea_root) == before
