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
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.cutover import stage_cutover
from eawf.kernel.migration.epoch2.errors import (
    MigrationPlanNotApplicableError,
    MigrationTrackUndeclaredError,
)
from eawf.kernel.migration.epoch2.lifecycle import MILESTONE_TRACK_FIELD, DeferralReason
from eawf.kernel.migration.epoch2.manifest import (
    OUTCOME_TRACK_FIELD,
    MigrationManifest,
    OperatorAssignment,
    RollbackBoundary,
    SealState,
    UnresolvedReason,
)
from eawf.kernel.migration.epoch2.native_records import (
    NativeRecordCollection,
    NativeRecordImportPlan,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.plan_mode import (
    DEFAULT_TRACK_FLAG,
    EPOCH2_PLAN_METHOD,
    PLAN_STEPS,
    Epoch2PlanRequest,
    MigrationPlan,
    PlanStep,
    declare_default_track,
    plan_cutover,
    plan_envelope,
    require_tracks_declared,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.migration.epoch2.validation import CorpusIdentity
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, tier_for
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


# ---------------------------------------------------------------------------
# Natively keyed records: decisions, incidents, sandbox policies, project.
# ---------------------------------------------------------------------------

#: What the full-shape corpus holds in each natively keyed collection.
NATIVE_ROWS = {"decisions": 2, "incidents": 1, "sandbox_policies": 1, "project": 1}


def test_plan_over_the_full_corpus_leaves_no_row_without_a_converter(
    full_plan: MigrationPlan,
) -> None:
    """Every natively keyed row has a target, so nothing waits on a waiver."""
    manifest = full_plan.manifest

    assert not [
        row for row in manifest.unresolved_rows if row.reason is UnresolvedReason.NO_CONVERTER
    ]
    assert manifest.unresolved_rows == ()
    for collection, rows in NATIVE_ROWS.items():
        mapping = manifest.row_mapping(collection)
        assert mapping.unresolved_row_count == 0, collection
        assert mapping.target_row_count == rows, collection
        assert manifest.target_census.by_collection[mapping.target_collection] == rows


def test_plan_names_every_row_a_missing_converter_leaves_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection whose converter is gone reds as unresolved, row by row."""
    original = NativeRecordImportPlan.build

    def _without_decisions(**kwargs: Any) -> NativeRecordImportPlan:
        built = original(**kwargs)
        return NativeRecordImportPlan(
            records=tuple(
                row
                for row in built.records
                if row.source_collection is not NativeRecordCollection.DECISIONS
            )
        )

    monkeypatch.setattr(
        "eawf.kernel.migration.epoch2.plan.NativeRecordImportPlan.build", _without_decisions
    )
    manifest = _plan(FULL_SNAPSHOT).manifest

    assert [(row.address, row.reason) for row in manifest.unresolved_rows] == [
        ("decisions/D01", UnresolvedReason.NO_CONVERTER),
        ("decisions/D02", UnresolvedReason.NO_CONVERTER),
    ]
    assert manifest.row_mapping("decisions").target_row_count == 0


def test_every_decision_converts_under_its_d_id_urn() -> None:
    """The converted Decision is cited by the same id and URN as before."""
    snapshot = SourceSnapshot.read(FULL_SNAPSHOT)
    corpus = CorpusImportPlan.build(snapshot=snapshot, allowlist_path=ALLOWLIST)
    source = snapshot.document["decisions"]
    decisions = [
        row
        for row in corpus.native.records
        if row.source_collection is NativeRecordCollection.DECISIONS
    ]

    assert [row.record_key for row in decisions] == sorted(source)
    for row in decisions:
        assert row.urn == build_urn("decision", owner="DEMO", id=row.record_key)
        assert row.payload == source[row.record_key]
        assert row.target is Epoch2Collection.DECISION


def test_staged_cutover_writes_each_native_record_under_its_source_key(tmp_path: Path) -> None:
    """Decisions and incidents reach their ledgers; project and policy stay in the document.

    The ledger line keeps the D-id as its key and the source row verbatim,
    which carries the status and supersession a decision citation is
    resolved by.
    """
    root = tmp_path / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, root)
    state_path = tmp_path / "generation" / "state.json"
    stage_cutover(
        plan=_plan(root),
        snapshot_root=root,
        allowlist_path=ALLOWLIST,
        state_path=state_path,
        recorded_at=SEALED_AT,
    )
    source = json.loads((root / "document.json").read_text(encoding="utf-8"))

    decisions = read_ledger_records(ledger_path(state_path, Epoch2Collection.DECISION))
    assert [record.record_key for record in decisions] == ["D01", "D02"]
    for record in decisions:
        assert record.payload["urn"] == f"urn:eawf:v1:decision:DEMO/{record.record_key}"
        assert record.payload["payload"] == source["decisions"][record.record_key]
    incidents = read_ledger_records(ledger_path(state_path, Epoch2Collection.INCIDENT))
    assert [record.record_key for record in incidents] == ["INC01"]

    document = json.loads(state_path.read_text(encoding="utf-8"))
    project = document[Epoch2Collection.PROJECT.value]["DEMO"]
    assert project["payload"]["payload"] == source["project"]
    assert set(document[Epoch2Collection.SANDBOX_POLICY.value]) == {"SP01"}


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


DECLARED_TRACK = "TRK-EAWF-CORE"


def _declared_request(snapshot_root: Path, *, track_key: str | None) -> Epoch2PlanRequest:
    """Return a plan request over ``snapshot_root`` declaring ``track_key``."""
    return Epoch2PlanRequest(
        snapshot_root=str(snapshot_root),
        allowlist_path=str(ALLOWLIST),
        workspace_key=CUTOVER_IDENTITY.workspace_key,
        project_key=CUTOVER_IDENTITY.project_key,
        repository_key=CUTOVER_IDENTITY.repository_key,
        sealed_by=SEALED_BY,
        default_track_key=track_key,
    )


@pytest.fixture(scope="module")
def declared_plan() -> MigrationPlan:
    """One plan over the full-shape corpus with the default Track declared."""
    return plan_cutover(
        _declared_request(FULL_SNAPSHOT, track_key=DECLARED_TRACK), sealed_at=SEALED_AT
    )


def _history_corpus(root: Path, *, phase_count: int) -> Path:
    """Copy the full-shape corpus and grow its phases to ``phase_count``.

    The added phases are closed and name no Track, the shape every phase
    of a real epoch-1 history has, so together with the one goal the
    corpus asks ``phase_count + 1`` Track questions.
    """
    shutil.copytree(FULL_SNAPSHOT, root)
    document_path = root / "document.json"
    document = json.loads(document_path.read_text())
    template = document["phases"]["P01"]
    for number in range(len(document["phases"]) + 1, phase_count + 1):
        phase_id = f"P{number:02d}"
        document["phases"][phase_id] = template | {
            "id": phase_id,
            "scope_id": phase_id,
            "title": f"Deliver {phase_id}",
        }
    document_path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return root


def test_plan_cutover_counts_every_track_question_without_a_declaration(tmp_path: Path) -> None:
    """38 phases and one goal ask 39 Track questions; nothing answers them."""
    corpus = _history_corpus(tmp_path / "snapshot", phase_count=38)
    envelope = plan_envelope(
        plan_cutover(_declared_request(corpus, track_key=None), sealed_at=SEALED_AT)
    )
    assert envelope["required_operator_assignment_count"] == 39
    assert envelope["declared_track_assignment_count"] == 0
    assert envelope["applicable"] is False


def test_plan_cutover_binds_every_track_question_to_the_declared_track(tmp_path: Path) -> None:
    """One declaration answers all 39 questions, recorded row by row, inferring nothing."""
    corpus = _history_corpus(tmp_path / "snapshot", phase_count=38)
    plan = plan_cutover(_declared_request(corpus, track_key=DECLARED_TRACK), sealed_at=SEALED_AT)
    envelope = plan_envelope(plan)
    manifest = plan.manifest
    assert envelope["required_operator_assignment_count"] == 0
    assert envelope["declared_track_assignment_count"] == 39
    assert {row.track_key for row in manifest.declared_track_assignments} == {DECLARED_TRACK}
    assert all(row.fabrication_findings == () for row in manifest.validation_results)
    assert all(mapping.operator_assignment_count == 0 for mapping in manifest.row_mappings)


def test_plan_cutover_records_the_declaration_in_the_manifest(
    full_plan: MigrationPlan, declared_plan: MigrationPlan
) -> None:
    """Each declared row answers exactly the question the undeclared plan asked."""
    asked = {
        (row.address, row.target_field, row.reason) for row in full_plan.manifest.track_assignments
    }
    answered = {
        (row.address, row.target_field, row.reason)
        for row in declared_plan.manifest.declared_track_assignments
    }
    assert answered == asked
    assert len(answered) == 4
    assert declared_plan.manifest.track_assignments == ()
    assert "4 declared Track assignments" in declared_plan.step(PlanStep.COLLECT_EVIDENCE).summary


def test_plan_cutover_declaration_moves_the_approval_digest(
    full_plan: MigrationPlan, declared_plan: MigrationPlan
) -> None:
    """Approving an undeclared plan does not approve a declared one, or the reverse."""
    assert declared_plan.manifest.source_digest == full_plan.manifest.source_digest
    assert declared_plan.manifest.manifest_digest != full_plan.manifest.manifest_digest
    assert declared_plan.approval_digest != full_plan.approval_digest


def test_plan_cutover_declaration_writes_no_track_onto_a_staged_record(
    declared_plan: MigrationPlan,
) -> None:
    """The declaration is recorded, never inferred: validation stays clean and reference-free."""
    for row in declared_plan.manifest.validation_results:
        assert row.fabrication_findings == ()
        assert row.dangling_references == ()


def test_declared_manifest_round_trips_through_its_own_digest(declared_plan: MigrationPlan) -> None:
    """The content digest covers the declaration, so a tampered Track fails to load."""
    dumped = declared_plan.manifest.model_dump(mode="json")
    assert MigrationManifest.model_validate(dumped) == declared_plan.manifest
    dumped["declared_track_assignments"][0]["track_key"] = "TRK-OTHER"
    with pytest.raises(ValidationError, match="does not cover its content"):
        MigrationManifest.model_validate(dumped)


def test_require_tracks_declared_refuses_naming_the_flag(full_plan: MigrationPlan) -> None:
    """A plan that still asks a Track question refuses with the flag that answers it."""
    with pytest.raises(MigrationTrackUndeclaredError) as excinfo:
        require_tracks_declared(full_plan.manifest)
    assert excinfo.value.code == "migration_track_undeclared"
    assert isinstance(excinfo.value, MigrationPlanNotApplicableError)
    assert DEFAULT_TRACK_FLAG in str(excinfo.value)
    assert DEFAULT_TRACK_FLAG == "--default-track-key"
    assert "4 imported records" in str(excinfo.value)
    assert "and 1 more" in str(excinfo.value)


def test_require_tracks_declared_passes_a_declared_plan(declared_plan: MigrationPlan) -> None:
    """Nothing left to ask is nothing to refuse."""
    require_tracks_declared(declared_plan.manifest)


def test_require_applicable_refuses_an_undeclared_track_once_rows_resolve(
    full_plan: MigrationPlan,
) -> None:
    """With every row placed, the open Track question is what refuses the apply."""
    resolved = full_plan.model_copy(
        update={"manifest": full_plan.manifest.model_copy(update={"unresolved_rows": ()})}
    )
    with pytest.raises(MigrationTrackUndeclaredError, match="--default-track-key"):
        resolved.require_applicable()


def _assignment(address: str) -> OperatorAssignment:
    return OperatorAssignment(
        address=address,
        source_collection="phases",
        target_collection="milestone",
        target_field=MILESTONE_TRACK_FIELD,
        reason=DeferralReason.SOURCE_HAS_NO_FIELD,
    )


def test_declare_default_track_without_a_declaration_changes_nothing() -> None:
    required = (_assignment("phases/P01"),)
    assert declare_default_track(required, track_key=None) == (required, ())


def test_declare_default_track_over_no_questions_declares_nothing() -> None:
    assert declare_default_track((), track_key=DECLARED_TRACK) == ((), ())
    assert declare_default_track((), track_key=None) == ((), ())


def test_declare_default_track_answers_a_single_question() -> None:
    required, declared = declare_default_track(
        (_assignment("phases/P01"),), track_key=DECLARED_TRACK
    )
    assert required == ()
    assert [(row.address, row.track_key) for row in declared] == [("phases/P01", DECLARED_TRACK)]


def test_declare_default_track_answers_every_question_in_order() -> None:
    addresses = [f"phases/P{number:02d}" for number in range(1, 40)]
    _, declared = declare_default_track(
        tuple(_assignment(address) for address in addresses), track_key=DECLARED_TRACK
    )
    assert [row.address for row in declared] == addresses


@pytest.mark.parametrize("bad_key", ["eawf-core", "TRK-", "TRK-x", "", "TRK-" + "A" * 33])
def test_declare_default_track_rejects_a_non_canonical_key(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        declare_default_track((_assignment("phases/P01"),), track_key=bad_key)


def test_epoch2_plan_request_rejects_a_non_canonical_track_key() -> None:
    """The slug an operator thinks of is not a Track key; the request says so."""
    with pytest.raises(ValidationError, match="default_track_key"):
        _declared_request(FULL_SNAPSHOT, track_key="eawf-core")


def test_epoch2_plan_request_rejects_a_non_string_track_key() -> None:
    with pytest.raises(ValidationError, match="default_track_key"):
        Epoch2PlanRequest.model_validate(_request_params(FULL_SNAPSHOT) | {"default_track_key": 7})


def test_plan_rpc_carries_the_declared_track(declared_plan: MigrationPlan) -> None:
    """The daemon computes the same declared plan as the library."""
    result = _dispatch(_request_params(FULL_SNAPSHOT) | {"default_track_key": DECLARED_TRACK})
    assert result["approval_digest"] == declared_plan.approval_digest
    assert result["required_operator_assignment_count"] == 0
    assert result["declared_track_assignment_count"] == 4


def _cli_plan_args(*extra: str) -> list[str]:
    return [
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
        *extra,
    ]


def test_cli_plan_verb_records_the_declared_track(
    declared_plan: MigrationPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--default-track-key`` reaches the plan and moves every question to declared."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(app, _cli_plan_args("--default-track-key", DECLARED_TRACK))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["manifest_digest"] == declared_plan.manifest.manifest_digest
    assert payload["required_operator_assignment_count"] == 0
    assert payload["declared_track_assignment_count"] == 4


def test_cli_plan_verb_rejects_a_slug_track_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slug is refused at the CLI boundary, not silently canonicalised."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(app, _cli_plan_args("--default-track-key", "eawf-core"))
    assert result.exit_code != 0
    assert "default_track_key" in result.output
