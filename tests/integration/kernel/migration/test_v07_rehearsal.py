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
from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    generation_id_for,
    generation_ids,
    read_marker,
    read_selection,
)
from eawf.kernel.migration.epoch2.journal import CutoverStage, read_journal, require_chain_intact
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.store.compaction import read_document
from eawf.surfaces.cli.app import app

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
#: resolves one of them reds here rather than silently widening the set an
#: apply is allowed to wave through.
EXPECTED_UNRESOLVED = (
    "decisions/D01",
    "decisions/D02",
    "incidents/INC01",
    "project",
    "sandbox_policies/SP01",
)

#: What the pinned corpus leaves in the published generation. Pinned rather
#: than derived from the manifest, so an apply that builds a thinner tree
#: and a manifest that agrees with it reds here instead of agreeing with
#: itself.
EXPECTED_LEDGER_RECORDS = 511
EXPECTED_TARGET_ROWS = 515
EXPECTED_LEDGER_FILES = 9

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
