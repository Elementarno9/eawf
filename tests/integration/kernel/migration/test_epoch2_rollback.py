"""The rollback table, rehearsed against real crashes rather than exceptions.

Every case here starts from a tree an interrupted apply actually left
behind: a child interpreter runs the apply and is killed with ``os._exit``
at a named durable write, so nothing unwinds, no ``finally`` runs and no
buffered journal row is flushed on the way out. The crash points are
declared in ``tests/fixtures/migration/cutover-faults/crash-points.json``
and the suite is total over that file, so a durable write that carries no
crash point reds the coverage test rather than passing unnoticed.

Two properties get their own attention.

**The full-set restore is a set claim.** The target tree is seeded with
three of the five declared authority surfaces before any apply runs. The
restore is then asked to put back a surface that was edited, a surface that
was deleted, a surface nothing touched, and to remove a surface that did
not exist when the restore point was taken. Afterwards every one of the
five is compared against its pinned digest, so "restores all files with
matching digests" is checked over the whole declared set rather than over
the one file the test happened to move.

**The boundary is refused, not automated.** A rollback after the published
generation has accepted a mutation is an incident and an operator decision.
The test proves both halves of that: the verb refuses with
``rollback_boundary_crossed``, and the tree is byte-identical afterwards --
including the journal, which does not get a row saying the rollback
declined.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import (
    TARGET_AUTHORITY_LOCATORS,
    DisposableTarget,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationDualAuthorityError,
    MigrationRestoreIncompleteError,
    MigrationRestorePointMissingError,
    MigrationRollbackBoundaryCrossedError,
)
from eawf.kernel.migration.epoch2.generation import (
    discard_generation,
    generation_ids,
)
from eawf.kernel.migration.epoch2.journal import read_journal, require_chain_intact
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.recovery import (
    Epoch2RecoverRequest,
    RecoveryAction,
    TreeAuthority,
    assess_recovery,
    recover_cutover,
    require_single_authority,
)
from eawf.kernel.migration.epoch2.restore import (
    ABSENT_SURFACE,
    SurfaceSnapshot,
    read_restore_manifest,
    restore_full_set,
)
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    REAPPLIED_AT,
    RECOVERED_AT,
    SEEDED_SURFACES,
    CutoverTree,
    applied_tree,
    apply_once,
    content_digests,
    crash_points,
    crash_the_apply,
    declared_canary,
    plan_over,
    staged_corpus,
)

runner = CliRunner()

#: The crash point whose prescribed action is the full-set restore: the
#: pointer names a generation and the marker has not been written.
POST_SELECT_CRASH = "after_selection_replaced"


def point_named(crash_point_id: str) -> dict[str, Any]:
    """Return one declared crash point by id.

    Args:
        crash_point_id: The row's id.

    Returns:
        The row.

    Raises:
        KeyError: No row carries that id, which means a test names a crash
            point the fixture no longer declares.
    """
    for point in crash_points():
        if point["id"] == crash_point_id:
            return dict(point)
    raise KeyError(f"the crash-point table declares no {crash_point_id!r}")


def crashed_after_select(tmp_path: Path) -> CutoverTree:
    """Return a tree whose apply died between the pointer and the marker."""
    return crash_the_apply(root=tmp_path, point=point_named(POST_SELECT_CRASH))


def rollback_request(target_root: Path, *, manifest: Path | None = None) -> Epoch2RecoverRequest:
    """Return a rollback request against ``target_root``."""
    return Epoch2RecoverRequest(
        target_root=str(target_root),
        action=RecoveryAction.ROLLBACK,
        manifest_path=None if manifest is None else str(manifest),
    )


def settled_authority(crashed: CutoverTree, *, epoch: int) -> TreeAuthority:
    """Return the authority the tree settles on, re-applying when it must.

    A recovery that put the tree back to epoch 1 has left no generation, so
    the "exactly one authoritative generation" claim is checked where it is
    meaningful: after the operator re-applies the plan whose rollback they
    just ran.

    Args:
        crashed: The tree the crash left behind.
        epoch: The epoch the recovery reported.

    Returns:
        The single authority the tree reads from.
    """
    target = DisposableTarget.require(crashed.target_root)
    if epoch == 2:
        return require_single_authority(target)
    reapplied = apply_once(
        corpus=crashed.corpus, target_root=crashed.target_root, applied_at=REAPPLIED_AT
    )
    assert reapplied.applied is True
    assert reapplied.generation_id == crashed.generation_id
    return require_single_authority(target)


@pytest.mark.parametrize("point", crash_points(), ids=lambda point: str(point["id"]))
def test_crash_point_leaves_the_wreckage_the_table_declares(
    tmp_path: Path, point: dict[str, Any]
) -> None:
    """A crash at one durable write leaves exactly the state the table names.

    The declared boundary is read from the tree and the declared journal
    boundary from the journal, and the two differ on purpose at the points
    where the journal was flushed ahead of the write that died: a recovery
    that trusted the journal there would finish an activation nobody
    selected.
    """
    crashed = crash_the_apply(root=tmp_path, point=point)
    target = DisposableTarget.require(crashed.target_root)
    expect = point["expect"]

    assessment = assess_recovery(target)

    assert assessment.boundary.value == expect["boundary"]
    assert assessment.journal_boundary.value == expect["journal_boundary"]
    assert assessment.maintenance_held is expect["maintenance_held"]
    assert list(assessment.staging_directories) == expect["staging"]
    assert len(generation_ids(target)) == expect["generations"]
    assert (assessment.selected_generation_id is not None) is expect["selected"]
    assert (assessment.marked_generation_id is not None) is expect["marked"]
    assert assessment.refusal_code is None


@pytest.mark.parametrize("point", crash_points(), ids=lambda point: str(point["id"]))
def test_crash_point_recovers_to_one_authority(tmp_path: Path, point: dict[str, Any]) -> None:
    """Each recovery leaves one authority and a journal row naming the stage."""
    crashed = crash_the_apply(root=tmp_path, point=point)
    target = DisposableTarget.require(crashed.target_root)
    recovery = point["recovery"]

    result = recover_cutover(
        Epoch2RecoverRequest(
            target_root=str(target.root), action=RecoveryAction(recovery["action"])
        ),
        recovered_at=RECOVERED_AT,
    )

    assert result.outcome.value == recovery["outcome"]
    assert result.stage is not None
    assert result.stage.value == recovery["stage"]
    assert result.authority.epoch == recovery["epoch"]
    assert result.journal_rows >= 1
    assert not target.maintenance_path.exists()
    rows = read_journal(target.journal_path)
    require_chain_intact(rows)
    assert rows[-1].stage.value == recovery["stage"]

    authority = settled_authority(crashed, epoch=result.authority.epoch)
    assert authority.epoch == 2
    assert authority.generation_id == crashed.generation_id
    assert authority.generation_count == 1


def test_crash_point_table_covers_every_durable_write(tmp_path: Path) -> None:
    """Every artifact one clean apply creates is claimed by a crash point.

    The set is derived from a real apply rather than listed by hand, so a
    later wave that adds a durable write and no crash point for it reds
    here naming the artifact nobody injects at.
    """
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")
    before = set(content_digests(target_root))

    result = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    created = set(content_digests(target_root)) - before
    classified = {_artifact_key(path, generation_id=result.generation_id) for path in created}
    transient = {"maintenance_marker", "staging_first", "staging_second"}
    declared = {key for point in crash_points() for key in point["writes"]}
    assert classified | transient == declared


def _artifact_key(path: str, *, generation_id: str) -> str:
    """Return the artifact key one created path belongs to.

    Args:
        path: A target-relative POSIX path the apply created.
        generation_id: The generation the apply published.

    Returns:
        The key the crash-point table declares the path under.

    Raises:
        AssertionError: The path belongs to no declared artifact, which
            means the apply now writes something the table cannot name.
    """
    keyed = {
        "generations/journal.jsonl": "journal",
        "generations/selected.json": "selection",
        "generations/EPOCH2_ACTIVE.json": "marker",
        "generations/restore/restore-manifest.json": "restore_manifest",
    }
    if path in keyed:
        return keyed[path]
    if path.startswith("generations/restore/"):
        return "restore_copies"
    if path.startswith(f"generations/{generation_id}/"):
        return "generation"
    raise AssertionError(f"{path} is a durable artifact the crash-point table does not name")


def test_rollback_restores_every_surface_with_matching_digests(tmp_path: Path) -> None:
    """A staged failure puts the whole declared surface set back, byte for byte.

    One surface is edited, one is deleted, one is untouched and one that
    never existed is created before the rollback runs, so the claim being
    checked is over the set rather than over the file the test moved.
    """
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    pinned = read_restore_manifest(target.restore_manifest_path)
    (target.root / "state.json").write_text('{"tampered": true}\n', encoding="utf-8")
    (target.root / "config.yaml").unlink()
    (target.root / "telemetry.db").write_bytes(b"a fallback writer got in\n")

    result = recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert result.outcome.value == "surfaces_restored"
    assert sorted(result.restored_locators) == sorted([*SEEDED_SURFACES, "telemetry.db"])
    for surface in pinned.restorable_surfaces():
        assert _surface_digest(target.root / surface.locator) == surface.digest
    assert set(TARGET_AUTHORITY_LOCATORS) == {
        surface.locator for surface in pinned.restorable_surfaces()
    }
    assert not (target.root / "telemetry.db").exists()


def _surface_digest(path: Path) -> str:
    """Return a surface's digest, or the absent sentinel when it is gone."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ABSENT_SURFACE


def test_rollback_drops_the_generation_and_the_activation(tmp_path: Path) -> None:
    """The tree reads epoch 1 again, with nothing selected and nothing built."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)

    result = recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert result.authority.epoch == 1
    assert result.authority.generation_id is None
    assert result.authority.generation_count == 0
    assert generation_ids(target) == ()
    assert crashed.generation_id in result.discarded
    assert not target.selection_path.exists()
    assert not target.marker_path.exists()


def test_rollback_then_reapply_reproduces_the_idempotence_digest(tmp_path: Path) -> None:
    """Restoring and re-applying from one manifest writes the same placement.

    The restore point pins the idempotence digest of the cutover it was
    taken for, so the re-apply is checked against the run it replaced rather
    than against itself.
    """
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)

    rolled_back = recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)
    reapplied = apply_once(
        corpus=crashed.corpus, target_root=crashed.target_root, applied_at=REAPPLIED_AT
    )

    assert rolled_back.idempotence_digest == crashed.plan.manifest.idempotence_digest
    assert reapplied.applied is True
    assert reapplied.generation_id == crashed.generation_id
    assert reapplied.manifest_digest == rolled_back.manifest_digest
    replaced = read_restore_manifest(target.restore_manifest_path)
    assert replaced.idempotence_digest == rolled_back.idempotence_digest
    assert plan_over(crashed.corpus).manifest.idempotence_digest == replaced.idempotence_digest


def test_rollback_returns_the_crossed_boundary_after_a_native_mutation(tmp_path: Path) -> None:
    """The assessment reports the crossed boundary and the code that forbids it."""
    crashed = applied_tree(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    _mutate_the_generation(target, generation_id=crashed.generation_id)

    assessment = assess_recovery(target)

    assert assessment.boundary is RollbackBoundary.CROSSED
    assert assessment.refusal_code == "rollback_boundary_crossed"
    assert assessment.marked_generation_id == crashed.generation_id


def test_rollback_refuses_after_the_first_native_mutation(tmp_path: Path) -> None:
    """A post-mutation rollback refuses and writes nothing, journal included."""
    crashed = applied_tree(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    _mutate_the_generation(target, generation_id=crashed.generation_id)
    before = content_digests(target.root)

    with pytest.raises(MigrationRollbackBoundaryCrossedError) as caught:
        recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert caught.value.code == "rollback_boundary_crossed"
    assert "incident" in str(caught.value)
    assert content_digests(target.root) == before


def test_recover_after_the_first_native_mutation_has_nothing_to_do(tmp_path: Path) -> None:
    """The forward verb is not the refusing one: it reports and writes nothing."""
    crashed = applied_tree(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    _mutate_the_generation(target, generation_id=crashed.generation_id)
    before = content_digests(target.root)

    result = recover_cutover(
        Epoch2RecoverRequest(target_root=str(target.root), action=RecoveryAction.RECOVER),
        recovered_at=RECOVERED_AT,
    )

    assert result.outcome.value == "nothing_to_recover"
    assert result.boundary is RollbackBoundary.CROSSED
    assert result.journal_rows == 0
    assert result.stage is None
    assert content_digests(target.root) == before


def _mutate_the_generation(target: DisposableTarget, *, generation_id: str) -> None:
    """Accept one mutation natively against the published generation."""
    ledgers = sorted((target.generation_path(generation_id) / "ledger").iterdir())
    with ledgers[0].open("a", encoding="utf-8") as handle:
        handle.write('{"id": "REC-NATIVE", "kind": "accepted after activation"}\n')


def test_rollback_refuses_a_partial_restore_point_and_writes_nothing(tmp_path: Path) -> None:
    """A copy the manifest names but the tree does not hold stops the restore.

    The verification runs before the first byte, so the tree is left exactly
    as the interrupted cutover left it rather than half way between two
    corpora.
    """
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    target.restore_copy_path("state.json").unlink()
    before = content_digests(target.root)

    with pytest.raises(MigrationRestoreIncompleteError) as caught:
        recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert caught.value.code == "migration_restore_incomplete"
    assert "state.json" in str(caught.value)
    assert content_digests(target.root) == before


def test_rollback_refuses_a_restore_copy_that_no_longer_digests_to_its_pin(
    tmp_path: Path,
) -> None:
    """An edited copy is refused: a restore never writes bytes nobody pinned."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    target.restore_copy_path("config.yaml").write_text("epoch: 9\n", encoding="utf-8")
    before = content_digests(target.root)

    with pytest.raises(MigrationRestoreIncompleteError) as caught:
        recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert "config.yaml" in str(caught.value)
    assert content_digests(target.root) == before


def test_rollback_refuses_a_restore_point_from_another_cutover(tmp_path: Path) -> None:
    """A manifest for a different generation pins no bytes for this tree."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    foreign = tmp_path / "foreign-restore-manifest.json"
    payload = json.loads(target.restore_manifest_path.read_text(encoding="utf-8"))
    payload["generation_id"] = "gen-0000000000000000"
    foreign.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationRestorePointMissingError):
        recover_cutover(rollback_request(target.root, manifest=foreign), recovered_at=RECOVERED_AT)


def test_rollback_refuses_a_tree_with_no_restore_point(tmp_path: Path) -> None:
    """Past the select, a tree that pinned nothing cannot be put back."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    target.restore_manifest_path.unlink()

    with pytest.raises(MigrationRestorePointMissingError) as caught:
        recover_cutover(rollback_request(target.root), recovered_at=RECOVERED_AT)

    assert caught.value.code == "migration_restore_point_missing"


def test_restore_full_set_refuses_a_manifest_whose_digest_was_edited(tmp_path: Path) -> None:
    """A hand-edited restore manifest does not load, so it cannot be written back."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    payload = json.loads(target.restore_manifest_path.read_text(encoding="utf-8"))
    payload["surfaces"][0]["digest"] = "0" * 64
    target.restore_manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationRestorePointMissingError) as caught:
        read_restore_manifest(target.restore_manifest_path)

    assert "restore digest" in str(caught.value)


def test_restore_full_set_over_an_untouched_tree_is_the_whole_present_set(
    tmp_path: Path,
) -> None:
    """Boundary: nothing moved, so the restore rewrites exactly what was there."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    manifest = read_restore_manifest(target.restore_manifest_path)

    restored = restore_full_set(target=target, manifest=manifest)

    assert sorted(restored) == sorted(SEEDED_SURFACES)


def test_discard_generation_refuses_the_generation_the_tree_still_reads(
    tmp_path: Path,
) -> None:
    """The de-activate-before-delete order is enforced, not merely documented."""
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)

    with pytest.raises(MigrationDualAuthorityError) as caught:
        discard_generation(target, generation_id=crashed.generation_id)

    assert "clear the activation first" in str(caught.value)
    assert generation_ids(target) == (crashed.generation_id,)


def test_surface_snapshot_parse_rejects_an_entry_with_no_digest() -> None:
    """Error path: an entry carrying no ``@`` names no surface digest."""
    with pytest.raises(ValueError, match="carries no '@'"):
        SurfaceSnapshot.parse("state.json")


def test_surface_snapshot_parse_rejects_an_unknown_digest_algorithm() -> None:
    """Error path: a pin that is neither absent nor sha256 is not a pin."""
    with pytest.raises(ValueError, match="pins neither"):
        SurfaceSnapshot.parse("state.json@md5:0123")


def test_surface_snapshot_parse_reads_the_absent_sentinel() -> None:
    """Boundary: a surface that did not exist parses as absent, not as empty."""
    surface = SurfaceSnapshot.parse(f"telemetry.db@{ABSENT_SURFACE}")

    assert surface.was_present is False
    assert surface.restorable is True


def test_surface_snapshot_parse_marks_the_registry_unrestorable() -> None:
    """The registry lives outside the fenced tree, so no restore may write it."""
    surface = SurfaceSnapshot.parse(f"registry.json@sha256:{'a' * 64}")

    assert surface.restorable is False
    assert surface.was_present is True


def test_rollback_through_the_cli_refuses_the_crossed_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator surface reports the code, not prose, and changes nothing."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    crashed = applied_tree(tmp_path)
    target = DisposableTarget.require(crashed.target_root)
    _mutate_the_generation(target, generation_id=crashed.generation_id)
    before = content_digests(target.root)

    result = runner.invoke(
        app, ["migrate", "epoch2", "--rollback", "--target-root", str(target.root)]
    )

    assert result.exit_code != 0
    assert "rollback_boundary_crossed" in result.output
    assert content_digests(target.root) == before


def test_rollback_through_the_cli_restores_a_post_select_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator route drives the same restore the library does."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    crashed = crashed_after_select(tmp_path)
    target = DisposableTarget.require(crashed.target_root)

    result = runner.invoke(
        app,
        [
            "--json",
            "migrate",
            "epoch2",
            "--rollback",
            "--target-root",
            str(target.root),
            "--manifest",
            str(target.restore_manifest_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "surfaces_restored"
    assert payload["authority"]["epoch"] == 1
    assert generation_ids(target) == ()


def test_rollback_through_the_cli_requires_a_target_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no default tree to roll back, so the verb refuses without one."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    result = runner.invoke(app, ["migrate", "epoch2", "--rollback"])

    assert result.exit_code != 0
    assert "--rollback requires --target-root" in result.output
