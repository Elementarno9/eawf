"""Plan mode over the full-shape corpus: read-only, repeatable, and honest.

Three properties are asserted end to end. **Read-only**: a recursive
digest map of the staged corpus *and* of a canonical repository tree
beside it is identical before and after two plan runs, with no path added
and none removed. **Idempotent**: two runs over two different copies of
one corpus agree on every digest the manifest carries, which is what lets
an approval be tied to a corpus rather than to a moment. **Honest about
open questions**: the corpus's one goal, ``G01``, maps to a Track outcome
metric the source names no Track for, and it surfaces as a required
operator assignment rather than as an unresolved row -- the record
imports, one field waits for a person.

The same plan is driven three ways -- the library, the daemon method, and
``eawf migrate epoch2 --plan`` -- and all three must produce one approval
digest. A CLI that computed a different plan than the daemon would verify
would make the approval token meaningless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.lifecycle import MILESTONE_TRACK_FIELD
from eawf.kernel.migration.epoch2.manifest import (
    OUTCOME_TRACK_FIELD,
    RollbackBoundary,
    SealState,
)
from eawf.kernel.migration.epoch2.plan_mode import (
    EPOCH2_PLAN_METHOD,
    PLAN_STEPS,
    Epoch2PlanRequest,
    MigrationPlan,
    PlanStep,
    plan_cutover,
    plan_envelope,
)
from eawf.kernel.migration.epoch2.validation import CorpusIdentity
from eawf.kernel.store.tiers import tier_for
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, dispatch
from eawf.surfaces.cli.app import app

# Imported for its registration side effect: the method is only reachable
# through ``dispatch`` once its module has been loaded, exactly as the
# daemon server loads it at boot.
import eawf.runtime.daemon.methods.migration  # noqa: F401  isort:skip

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"

CUTOVER_IDENTITY = CorpusIdentity(
    workspace_key="WSP-DEFAULT", project_key="PRJ-DEMO", repository_key="REP-DEMO"
)
SEALED_AT = datetime(2026, 1, 1, tzinfo=UTC)
SEALED_BY = "plan-mode-test"

runner = CliRunner()


def _tree_digests(root: Path) -> dict[str, str]:
    """Return a digest per file under ``root``, keyed by relative POSIX path.

    Args:
        root: The directory to walk.

    Returns:
        One entry per regular file. A path that appears or disappears
        changes the mapping's key set, so the comparison catches a
        created file as well as an edited one.
    """
    return {
        str(path.relative_to(root).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def staged_repository(tmp_path: Path) -> Path:
    """A copy of the corpus beside a canonical repository tree.

    Plan mode must leave both alone: the corpus because the plan's digest
    is taken over it, and the repository because a read-only verb that
    writes canonical state is a write wearing a read's name.
    """
    root = tmp_path / "repo"
    shutil.copytree(FULL_SNAPSHOT, root / "staged")
    canonical = root / ".ea"
    (canonical / "store").mkdir(parents=True)
    (canonical / "state.json").write_text(
        json.dumps({"schema_version": "1.19", "scope_kind": "project"}) + "\n"
    )
    (canonical / "store" / "audit.jsonl").write_text('{"id": "A001"}\n')
    (root / "registry.json").write_text(json.dumps({"repos": {}}) + "\n")
    return root


def _plan(snapshot_root: Path) -> MigrationPlan:
    """Build one plan over ``snapshot_root`` under the shared test seal."""
    return plan_cutover(
        Epoch2PlanRequest(
            snapshot_root=str(snapshot_root),
            allowlist_path=str(ALLOWLIST),
            workspace_key=CUTOVER_IDENTITY.workspace_key,
            project_key=CUTOVER_IDENTITY.project_key,
            repository_key=CUTOVER_IDENTITY.repository_key,
            sealed_by=SEALED_BY,
        ),
        sealed_at=SEALED_AT,
    )


@pytest.fixture(scope="module")
def full_plan() -> MigrationPlan:
    """One plan over the pinned full-shape corpus, built once."""
    return _plan(FULL_SNAPSHOT)


def test_plan_mode_writes_nothing(staged_repository: Path) -> None:
    """Two plan runs leave every byte of corpus and repository untouched."""
    before = _tree_digests(staged_repository)
    _plan(staged_repository / "staged")
    _plan(staged_repository / "staged")
    after = _tree_digests(staged_repository)
    assert after == before
    assert set(after) == {
        "staged/document.json",
        "staged/registry.json",
        "staged/telemetry.json",
        "staged/config/base.yaml",
        "staged/config/overlay.yaml",
        "staged/store/audit.jsonl",
        "staged/store/decision.jsonl",
        ".ea/state.json",
        ".ea/store/audit.jsonl",
        "registry.json",
    }


def test_two_runs_emit_byte_identical_manifest_digests(tmp_path: Path) -> None:
    """One corpus at two roots yields one manifest digest, byte for byte."""
    first_root = tmp_path / "one" / "snapshot"
    second_root = tmp_path / "two" / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, first_root)
    shutil.copytree(FULL_SNAPSHOT, second_root)

    first = _plan(first_root)
    second = _plan(second_root)

    assert first.manifest.manifest_digest == second.manifest.manifest_digest
    assert first.manifest.idempotence_digest == second.manifest.idempotence_digest
    assert first.manifest.source_digest == second.manifest.source_digest
    assert first.approval_digest == second.approval_digest
    assert first.manifest.model_dump_json() == second.manifest.model_dump_json()


def test_manifest_digest_is_independent_of_the_seal(tmp_path: Path) -> None:
    """Sealing at a different time moves the seal digest and nothing else."""
    root = tmp_path / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, root)
    request = Epoch2PlanRequest(
        snapshot_root=str(root),
        allowlist_path=str(ALLOWLIST),
        workspace_key=CUTOVER_IDENTITY.workspace_key,
        project_key=CUTOVER_IDENTITY.project_key,
        repository_key=CUTOVER_IDENTITY.repository_key,
        sealed_by=SEALED_BY,
    )
    early = plan_cutover(request, sealed_at=SEALED_AT)
    late = plan_cutover(request, sealed_at=datetime(2027, 9, 9, tzinfo=UTC))

    assert early.manifest.manifest_digest == late.manifest.manifest_digest
    assert early.manifest.seal_digest != late.manifest.seal_digest
    assert early.approval_digest == late.approval_digest


def test_goal_lands_under_required_operator_assignments(full_plan: MigrationPlan) -> None:
    """``G01`` needs a Track nobody recorded; that is an assignment, not a gap."""
    manifest = full_plan.manifest
    assignment = next(row for row in manifest.track_assignments if row.address == "goals/G01")
    assert assignment.target_field == OUTCOME_TRACK_FIELD
    assert assignment.target_collection == "track_outcome"
    assert assignment.candidates == ()
    assert "goals/G01" not in {row.address for row in manifest.unresolved_rows}
    assert manifest.row_mapping("goals").operator_assignment_count == 1
    assert manifest.row_mapping("goals").unresolved_row_count == 0
    assert manifest.row_mapping("goals").target_row_count == 1


def test_every_milestone_track_is_an_assignment_not_a_gap(full_plan: MigrationPlan) -> None:
    """A corpus with no tracks leaves every Milestone's owner to an operator."""
    phases = full_plan.manifest.assignments_for("phases")
    assert [row.address for row in phases] == ["phases/P01", "phases/P02", "phases/P03"]
    assert {row.target_field for row in phases} == {MILESTONE_TRACK_FIELD}
    assert "phases" not in {row.source_collection for row in full_plan.manifest.unresolved_rows}


def test_plan_runs_the_seven_declared_steps_in_order(full_plan: MigrationPlan) -> None:
    """The sequence is the contract, and the last step is the double validation."""
    assert tuple(row.step for row in full_plan.steps) == PLAN_STEPS
    assert len(PLAN_STEPS) == 7
    assert full_plan.steps[-1].step is PlanStep.VALIDATE_SECOND
    assert "byte-identical" in full_plan.step(PlanStep.VALIDATE_SECOND).summary


def test_double_validation_records_two_agreeing_passes(full_plan: MigrationPlan) -> None:
    """Both passes are recorded, and they agree on digest and byte length."""
    first, second = full_plan.manifest.validation_results
    assert (first.pass_index, second.pass_index) == (1, 2)
    assert first.report_digest == second.report_digest
    assert first.payload_byte_length == second.payload_byte_length


def test_plan_mode_manifest_is_sealed_but_writes_nothing(full_plan: MigrationPlan) -> None:
    """A plan is a closed record of an open question, not a started cutover."""
    assert full_plan.manifest.seal_state is SealState.SEALED
    assert full_plan.manifest.rollback_boundary is RollbackBoundary.PLAN_ONLY
    assert full_plan.manifest.backup is None


def test_store_mappings_agree_with_the_compiled_tier_table(
    full_plan: MigrationPlan,
) -> None:
    """Every receiving collection lands at the tier the table declares."""
    mappings = full_plan.manifest.store_mappings
    assert mappings
    assert all(row.tier is tier_for(row.collection) for row in mappings)
    assert sum(row.row_count for row in mappings) == full_plan.manifest.target_census.total_rows


def test_row_mappings_are_total_over_the_censused_source(
    full_plan: MigrationPlan,
) -> None:
    """Every censused collection has a mapping, and the counts agree."""
    manifest = full_plan.manifest
    censused = {row.source_collection for row in manifest.source_census.collections}
    assert {row.source_collection for row in manifest.row_mappings} == censused
    for mapping in manifest.row_mappings:
        row = manifest.source_census.collection(mapping.source_collection)
        assert mapping.source_row_count == row.row_count
        assert mapping.proof_form is row.proof_form


def _dispatch(params: Mapping[str, Any]) -> dict[str, Any]:
    """Call the plan RPC the way the daemon server does."""
    ctx = MethodContext(started_at="now", pid=1, protocol_version="test", version="test")
    return asyncio.run(dispatch(EPOCH2_PLAN_METHOD, ctx, dict(params)))


def _request_params(snapshot_root: Path) -> dict[str, Any]:
    """Return the wire params for a plan over ``snapshot_root``."""
    return {
        "snapshot_root": str(snapshot_root),
        "allowlist_path": str(ALLOWLIST),
        "workspace_key": CUTOVER_IDENTITY.workspace_key,
        "project_key": CUTOVER_IDENTITY.project_key,
        "repository_key": CUTOVER_IDENTITY.repository_key,
        "sealed_by": SEALED_BY,
    }


def test_plan_rpc_agrees_with_the_library(full_plan: MigrationPlan) -> None:
    """The daemon and the library compute one approval digest."""
    result = _dispatch(_request_params(FULL_SNAPSHOT))
    assert result["status"] == "ok"
    assert result["approval_digest"] == full_plan.approval_digest
    assert result["manifest_digest"] == full_plan.manifest.manifest_digest
    assert result["applicable"] is False
    assert result["required_operator_assignment_count"] == 4
    assert [row["step"] for row in result["steps"]] == [step.value for step in PLAN_STEPS]


def test_plan_rpc_rejects_an_unknown_param() -> None:
    """The wire contract forbids a field the handler would ignore."""
    params = _request_params(FULL_SNAPSHOT) | {"force": True}
    with pytest.raises(DaemonValidationError, match="validation_failed"):
        _dispatch(params)


def test_plan_rpc_rejects_a_missing_addressing_slot() -> None:
    """Epoch 1 recorded no workspace, so omitting one is a refusal."""
    params = _request_params(FULL_SNAPSHOT)
    del params["workspace_key"]
    with pytest.raises(DaemonValidationError, match="workspace_key"):
        _dispatch(params)


def test_plan_rpc_reports_a_rule_refusal_by_code(tmp_path: Path) -> None:
    """A corpus the importer cannot read is refused with its stable code."""
    root = tmp_path / "snapshot"
    shutil.copytree(FIXTURES / "epoch1-malformed" / "snapshot", root)
    with pytest.raises(DaemonValidationError, match="migration_row_validation"):
        _dispatch(_request_params(root))


def test_plan_envelope_carries_the_whole_plan(full_plan: MigrationPlan) -> None:
    """The summary is a convenience; the plan itself is never dropped."""
    envelope = plan_envelope(full_plan)
    assert envelope["plan"]["approval_digest"] == full_plan.approval_digest
    assert envelope["unresolved_row_count"] == len(full_plan.manifest.unresolved_rows)
    assert envelope["target_rows"] == full_plan.manifest.target_census.total_rows


def test_cli_plan_verb_agrees_with_the_library(
    full_plan: MigrationPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eawf migrate epoch2 --plan`` emits the same digests as the library."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(
        app,
        [
            "--json",
            "migrate",
            "epoch2",
            "--plan",
            "--snapshot-root",
            str(FULL_SNAPSHOT),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            CUTOVER_IDENTITY.workspace_key,
            "--project-key",
            CUTOVER_IDENTITY.project_key,
            "--repository-key",
            CUTOVER_IDENTITY.repository_key,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["manifest_digest"] == full_plan.manifest.manifest_digest
    assert payload["idempotence_digest"] == full_plan.manifest.idempotence_digest
    assert payload["applicable"] is False


def test_cli_plan_verb_requires_the_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no default mode, so a bare invocation refuses rather than runs."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(
        app,
        [
            "migrate",
            "epoch2",
            "--snapshot-root",
            str(FULL_SNAPSHOT),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            CUTOVER_IDENTITY.workspace_key,
            "--project-key",
            CUTOVER_IDENTITY.project_key,
            "--repository-key",
            CUTOVER_IDENTITY.repository_key,
        ],
    )
    assert result.exit_code != 0
    assert "pass exactly one of --plan, --apply, --export" in result.output


def test_cli_plan_verb_refuses_a_missing_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging root with no document is unreadable, not empty."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(
        app,
        [
            "migrate",
            "epoch2",
            "--plan",
            "--snapshot-root",
            str(tmp_path / "absent"),
            "--allowlist",
            str(ALLOWLIST),
            "--workspace-key",
            CUTOVER_IDENTITY.workspace_key,
            "--project-key",
            CUTOVER_IDENTITY.project_key,
            "--repository-key",
            CUTOVER_IDENTITY.repository_key,
        ],
    )
    assert result.exit_code != 0
    assert "migration_source_unreadable" in result.output


def test_chain_verbs_are_untouched_by_the_nested_sub_app() -> None:
    """``eawf migrate status`` still answers about the state-schema chain."""
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "epoch2" in result.output
    assert "status" in result.output
