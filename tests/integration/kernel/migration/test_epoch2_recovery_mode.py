"""Recovery mode: finishing what an interrupted apply started, or undoing it.

The window this suite lives in is the one between the selection pointer and
the epoch marker. A tree there has a complete new generation and still
reads, correctly, as epoch 1, so the decision is genuinely open: the
generation can be activated, or the tree can be put back. Recovery makes
that decision by reading the generation, not by trusting the journal --
which at this boundary has already recorded the select it was flushed ahead
of.

**Without a dual write** is the sharp part of the claim. Completing an
activation is one write: the marker. The generation is not rebuilt, not
re-staged and not re-published, so the recovery cannot leave a second copy
of a tree that is already on disk. The proof is a byte-level comparison of
the whole target tree before and after -- the only paths allowed to move
are the marker, the journal and the maintenance marker the crash left open.

The fence applies here exactly as it does to the apply. A recovery writes
the same authority surfaces, so it clears the same gate and holds the same
locks, and a tree that has not declared itself disposable is refused before
anything is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    MARKER_FILENAME,
    DisposableTarget,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationDualAuthorityError,
    MigrationJournalBrokenError,
    MigrationNotQuiescentError,
    MigrationTargetNotDisposableError,
)
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    STAGING_PREFIX,
    generation_digest,
    generation_ids,
    read_marker,
    read_selection,
)
from eawf.kernel.migration.epoch2.journal import CutoverStage, read_journal, require_chain_intact
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.recovery import (
    Epoch2RecoverRequest,
    RecoveryAction,
    RecoveryOutcome,
    RecoveryResult,
    TreeAuthority,
    assess_recovery,
    recover_cutover,
    recovery_envelope,
    require_single_authority,
)
from eawf.kernel.migration.epoch2.restore import read_restore_manifest
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._cutover_harness import (
    RECOVERED_AT,
    SEEDED_SURFACES,
    CutoverTree,
    applied_tree,
    content_digests,
    crash_points,
    crash_the_apply,
)

runner = CliRunner()

#: The crash point this suite is about: the pointer is written and the
#: marker is not, which is the one window where both actions are open.
POST_SELECT_CRASH = "after_selection_replaced"

#: The crash point that leaves a complete activation and an open window.
POST_MARKER_CRASH = "after_marker_written"

#: A crash before anything is built, where a recovery can only discard.
PRE_BUILD_CRASH = "after_restore_manifest"


def point_named(crash_point_id: str) -> dict[str, Any]:
    """Return one declared crash point by id.

    Args:
        crash_point_id: The row's id.

    Returns:
        The row.

    Raises:
        KeyError: No row carries that id.
    """
    for point in crash_points():
        if point["id"] == crash_point_id:
            return dict(point)
    raise KeyError(f"the crash-point table declares no {crash_point_id!r}")


def crashed_at(tmp_path: Path, crash_point_id: str) -> CutoverTree:
    """Return a tree whose apply died at the named crash point."""
    return crash_the_apply(root=tmp_path, point=point_named(crash_point_id))


def recover(target_root: Path) -> RecoveryResult:
    """Run the forward recovery verb against ``target_root``."""
    return recover_cutover(
        Epoch2RecoverRequest(target_root=str(target_root), action=RecoveryAction.RECOVER),
        recovered_at=RECOVERED_AT,
    )


def test_recover_completes_the_activation_of_a_selected_generation(tmp_path: Path) -> None:
    """The window between the pointer and the marker is finished, not undone."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    result = recover(target.root)

    assert result.outcome is RecoveryOutcome.ACTIVATION_COMPLETED
    assert result.boundary is RollbackBoundary.GENERATION_SELECTED
    assert result.authority == TreeAuthority(
        epoch=2, generation_id=crashed.generation_id, generation_count=1
    )
    marker = read_marker(target)
    assert marker is not None
    assert marker.generation_id == crashed.generation_id
    assert marker.written_at == RECOVERED_AT


def test_recover_pins_the_published_generation_in_the_marker_it_writes(
    tmp_path: Path,
) -> None:
    """The completed activation pins the same baseline a clean apply would.

    Without that, the tree would be marked epoch 2 with nothing to compare a
    later mutation against, and the crossed boundary would be undetectable
    on exactly the trees that had to be recovered.
    """
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    recover(target.root)

    marker = read_marker(target)
    selection = read_selection(target)
    assert marker is not None
    assert selection is not None
    assert marker.generation_digest == generation_digest(
        target, generation_id=crashed.generation_id
    )
    assert marker.manifest_digest == selection.manifest_digest


def test_recover_reconciles_the_selected_generation_without_a_dual_write(
    tmp_path: Path,
) -> None:
    """Completing an activation writes the marker and nothing else.

    Every other path in the tree is compared byte for byte. A recovery that
    rebuilt or republished the generation would move one of them, which is
    the dual write this claim forbids.
    """
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    before = content_digests(target.root)

    recover(target.root)

    after = content_digests(target.root)
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {path for path in set(before) & set(after) if before[path] != after[path]}
    assert added == {f"generations/{MARKER_FILENAME}"}
    assert removed == {"local/epoch2-maintenance.json"}
    assert changed == {"generations/journal.jsonl"}
    assert generation_ids(target) == (crashed.generation_id,)


def test_recover_journals_the_re_read_before_it_completes_the_activation(
    tmp_path: Path,
) -> None:
    """The journal names the stage, and names the read that justified it."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    result = recover(target.root)

    rows = read_journal(target.journal_path)
    require_chain_intact(rows)
    stages = [row.stage for row in rows]
    assert stages[-2:] == [CutoverStage.READ_SMOKE_PASSED, CutoverStage.ACTIVATION_COMPLETED]
    assert rows[-1].boundary is RollbackBoundary.MARKER_WRITTEN
    assert "re-read" in rows[-2].detail
    assert result.journal_rows == 2


def test_recover_restores_the_full_set_when_the_generation_does_not_read_back(
    tmp_path: Path,
) -> None:
    """A generation that cannot be read is not activated: the tree goes back.

    This is the other arm of "completes activation or restores the full
    set", and it is the arm that matters -- activating a generation the
    public readers reject would make the marker a claim nothing supports.
    """
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    document = target.generation_path(crashed.generation_id) / GENERATION_DOCUMENT
    document.write_text("{ this is not json", encoding="utf-8")
    pinned = read_restore_manifest(target.restore_manifest_path)

    result = recover(target.root)

    assert result.outcome is RecoveryOutcome.SURFACES_RESTORED
    assert sorted(result.restored_locators) == sorted(SEEDED_SURFACES)
    assert result.authority.epoch == 1
    assert generation_ids(target) == ()
    assert read_marker(target) is None
    for surface in pinned.restorable_surfaces():
        path = target.root / surface.locator
        assert path.is_file() is surface.was_present


def test_recover_after_the_marker_closes_the_window_the_crash_left_open(
    tmp_path: Path,
) -> None:
    """A complete activation needs no repair, but an open window does."""
    crashed = crashed_at(tmp_path, POST_MARKER_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    assert target.maintenance_path.exists()

    result = recover(target.root)

    assert result.outcome is RecoveryOutcome.WINDOW_CLOSED
    assert result.boundary is RollbackBoundary.MARKER_WRITTEN
    assert result.authority.generation_id == crashed.generation_id
    assert not target.maintenance_path.exists()
    assert read_journal(target.journal_path)[-1].stage is CutoverStage.MAINTENANCE_EXITED


def test_recover_on_a_finished_cutover_writes_nothing(tmp_path: Path) -> None:
    """Boundary: a tree with nothing to recover is left byte-identical."""
    finished = applied_tree(tmp_path)
    target = DisposableTarget.require(finished.target_root)
    before = content_digests(target.root)

    result = recover(target.root)

    assert result.outcome is RecoveryOutcome.NOTHING_TO_RECOVER
    assert result.journal_rows == 0
    assert result.stage is None
    assert content_digests(target.root) == before


def test_recover_before_the_select_discards_what_the_crash_built(tmp_path: Path) -> None:
    """Boundary: with no activation to complete, both verbs discard.

    The forward verb does not invent a generation to activate out of a
    restore point and a journal, because there is nothing selected to
    activate.
    """
    crashed = crashed_at(tmp_path, PRE_BUILD_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    result = recover(target.root)

    assert result.outcome is RecoveryOutcome.STAGING_DISCARDED
    assert result.boundary is RollbackBoundary.PLAN_ONLY
    assert result.authority.epoch == 1
    assert result.stage is CutoverStage.ROLLBACK_DISCARDED
    assert "local/epoch2-maintenance.json" in result.discarded


def test_recover_refuses_a_tree_that_has_not_declared_itself_disposable(
    tmp_path: Path,
) -> None:
    """Error path: the recovery writes what the apply writes, so it is fenced too."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    (crashed.target_root / CANARY_DECLARATION_FILENAME).unlink()

    with pytest.raises(MigrationTargetNotDisposableError) as caught:
        recover(crashed.target_root)

    assert caught.value.code == "migration_target_not_disposable"


def test_recover_refuses_a_tree_a_lease_still_holds(tmp_path: Path) -> None:
    """Error path: a recovery rewrites authority surfaces, so it never races."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    locks = crashed.target_root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "worktrees.lock").write_text("{}\n", encoding="utf-8")

    with pytest.raises(MigrationNotQuiescentError) as caught:
        recover(crashed.target_root)

    assert caught.value.code == "migration_not_quiescent"


def test_recover_refuses_a_tree_that_names_two_authorities(tmp_path: Path) -> None:
    """Error path: a marker with no pointer is a tree nobody can reconcile."""
    crashed = crashed_at(tmp_path, POST_MARKER_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    target.selection_path.unlink()

    with pytest.raises(MigrationDualAuthorityError) as caught:
        recover(target.root)

    assert caught.value.code == "migration_dual_authority"
    assert "no selection pointer" in str(caught.value)
    assert crashed.generation_id in str(caught.value)


def test_recover_refuses_a_journal_whose_chain_was_edited(tmp_path: Path) -> None:
    """Error path: a journal that was edited cannot say how far the apply got."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    rows = target.journal_path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(rows[0])
    edited["detail"] = "a detail nobody wrote"
    target.journal_path.write_text(
        "\n".join([json.dumps(edited, sort_keys=True), *rows[1:]]) + "\n", encoding="utf-8"
    )

    with pytest.raises(MigrationJournalBrokenError):
        recover(target.root)


def test_require_single_authority_refuses_an_orphan_generation(tmp_path: Path) -> None:
    """Error path: a generation nothing selects is the next apply's surprise."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)
    target.selection_path.unlink()

    with pytest.raises(MigrationDualAuthorityError) as caught:
        require_single_authority(target)

    assert "nothing selects" in str(caught.value)


def test_require_single_authority_refuses_a_surviving_staging_directory(
    tmp_path: Path,
) -> None:
    """Error path: a staging tree left behind is a build nobody published."""
    finished = applied_tree(tmp_path)
    target = DisposableTarget.require(finished.target_root)
    (target.generations_dir / f"{STAGING_PREFIX}-a").mkdir()

    with pytest.raises(MigrationDualAuthorityError) as caught:
        require_single_authority(target)

    assert f"{STAGING_PREFIX}-a" in str(caught.value)


def test_assess_recovery_reports_the_journal_running_ahead_of_the_tree(
    tmp_path: Path,
) -> None:
    """The journal claims the select; the tree has the pointer to prove it."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    assessment = assess_recovery(target)

    assert assessment.boundary is RollbackBoundary.GENERATION_SELECTED
    assert assessment.journal_boundary is RollbackBoundary.MARKER_WRITTEN
    assert assessment.stage is CutoverStage.MARKER_WRITTEN
    assert assessment.selected_generation_id == crashed.generation_id
    assert assessment.marked_generation_id is None


def test_recovery_envelope_reports_the_outcome_as_the_status(tmp_path: Path) -> None:
    """The wire envelope leads with what was done, not with a boolean."""
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)

    envelope = recovery_envelope(recover(crashed.target_root))

    assert envelope["status"] == "activation_completed"
    assert envelope["action"] == "recover"
    assert envelope["authority"]["epoch"] == 2
    assert envelope["boundary"] == "generation_selected"


def test_recover_through_the_cli_completes_the_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator route drives the same reconciliation the library does."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    crashed = crashed_at(tmp_path, POST_SELECT_CRASH)
    target = DisposableTarget.require(crashed.target_root)

    result = runner.invoke(
        app, ["--json", "migrate", "epoch2", "--recover", "--target-root", str(target.root)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "activation_completed"
    assert payload["authority"]["generation_id"] == crashed.generation_id
    assert read_marker(target) is not None


def test_recover_through_the_cli_refuses_two_recovery_modes_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: a run is never both a repair forward and a repair back."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    result = runner.invoke(app, ["migrate", "epoch2", "--recover", "--rollback"])

    assert result.exit_code != 0
    assert "got 2" in result.output
