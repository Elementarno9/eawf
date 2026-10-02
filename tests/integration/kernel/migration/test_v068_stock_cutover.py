"""A stock v0.6.8 tree cuts over without hand edits.

The two corpora under ``tests/fixtures/migration/v068-stock`` were staged
with ``eawf migrate epoch2 --stage-to`` from trees the released v0.6.8 CLI
wrote. ``fresh`` is ``eawf init --quick`` and nothing else: its document
omits every defaulted collection and it has no store ledger at all.
``used`` adds phases, an iter, waves, backlog, a decision, a memory note,
a second goal, a hypothesis and a closed executor session, so its
optional collections are written as JSON null and it holds only the
decision and memory ledgers.
"""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2 import cutover
from eawf.kernel.migration.epoch2.allowlist import DEFAULT_ALLOWLIST_PATH
from eawf.kernel.migration.epoch2.apply import CutoverResult, apply_cutover
from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import DropProofForm
from eawf.kernel.migration.epoch2.errors import (
    MigrationCollectionOmittedError,
    MigrationNotQuiescentError,
    MigrationRowsUnreconciledError,
)
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.plan_mode import plan_cutover
from eawf.kernel.migration.epoch2.rows import EPOCH1_OMITTED_DEFAULTS, RowFailureCode
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.backup import create_backup, snapshot_digest
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._corpus_shapes import DEFAULT_TRACK_KEY
from tests.integration.kernel.migration._cutover_harness import (
    ALLOWLIST,
    APPLIED_AT,
    BACKED_UP_AT,
    PROJECT_KEY,
    REPOSITORY_KEY,
    WORKSPACE_KEY,
    apply_request_for,
    plan_request_for,
    write_opt_in,
)

STOCK = Path(__file__).resolve().parents[3] / "fixtures" / "migration" / "v068-stock"
FRESH = STOCK / "fresh"
USED = STOCK / "used"

#: The optional slots a v0.6.8 tree writes as null until their first row.
USED_NULL_SLOTS = ("actuals", "audits", "incidents", "sandbox_policies", "worktrees")

runner = CliRunner()


@pytest.fixture
def backup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the backup service's user home at this test's tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EAWF_HOME", str(home))
    return home


def _rewrite(corpus: Path, edit: Callable[[dict[str, Any]], object]) -> None:
    path = corpus / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document, indent=1, sort_keys=True), encoding="utf-8")


def _corpus(fixture: Path, root: Path) -> Path:
    corpus = root / "staged"
    shutil.copytree(fixture, corpus)
    return corpus


def _opted_in_target(corpus: Path, root: Path, *, home: Path) -> Path:
    """Build the live ``.ea`` tree the corpus was staged from and opt it in."""
    target = root / ".ea"
    (target / "store").mkdir(parents=True)
    shutil.copyfile(corpus / "document.json", target / "state.json")
    shutil.copyfile(corpus / "config" / "base.yaml", target / "config.yaml")
    for ledger in sorted((corpus / "store").glob("*.jsonl")):
        shutil.copyfile(ledger, target / "store" / ledger.name)
    snapshot = create_backup(target / "state.json", home=home, when=BACKED_UP_AT)
    write_opt_in(target, backup_ts=snapshot.ts, backup_digest=snapshot_digest(snapshot))
    return target


@pytest.mark.parametrize("fixture", [FRESH, USED], ids=["fresh", "used"])
def test_v068_stock_tree_plans_and_applies_without_hand_edits(
    fixture: Path, tmp_path: Path, backup_home: Path
) -> None:
    corpus = _corpus(fixture, tmp_path)
    target = _opted_in_target(corpus, tmp_path, home=backup_home)

    plan = plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)
    result = apply_cutover(
        apply_request_for(corpus=corpus, target_root=target, plan=plan), applied_at=APPLIED_AT
    )

    assert result.applied is True


def test_v068_fresh_snapshot_reads_an_absent_ledger_as_empty() -> None:
    snapshot = SourceSnapshot.read(FRESH)

    assert snapshot.ledgers == {}
    assert snapshot.ledger("audit") == ()
    assert SourceCensus.build(snapshot).audits.ledger_rows == 0


def test_v068_fresh_census_reads_each_omitted_collection_as_its_default() -> None:
    raw = json.loads((FRESH / "document.json").read_text(encoding="utf-8"))
    census = SourceCensus.build(SourceSnapshot.read(FRESH))

    omitted = sorted(set(EPOCH1_OMITTED_DEFAULTS) - set(raw))
    assert len(omitted) == 23
    assert census.row_failures == ()
    assert census.collection("backlog").proof_form is DropProofForm.NULL_NOT_EMPTY
    assert census.collection("backlog").row_count is None
    assert census.collection("wave_integrations").proof_form is DropProofForm.ZERO_ROWS
    assert census.collection("wave_integrations").row_count == 0


def test_v068_used_census_accepts_each_null_optional_slot() -> None:
    document = json.loads((USED / "document.json").read_text(encoding="utf-8"))
    census = SourceCensus.build(SourceSnapshot.read(USED))

    assert all(document[slot] is None for slot in USED_NULL_SLOTS)
    assert census.row_failures == ()
    for slot in USED_NULL_SLOTS:
        assert census.collection(slot).proof_form is DropProofForm.NULL_NOT_EMPTY


def test_v068_census_still_refuses_an_omitted_collection_with_no_default(
    tmp_path: Path,
) -> None:
    corpus = _corpus(FRESH, tmp_path)
    _rewrite(corpus, lambda document: document.pop("phases"))

    with pytest.raises(MigrationCollectionOmittedError, match="phases"):
        SourceCensus.build(SourceSnapshot.read(corpus))


def test_v068_census_still_refuses_null_in_a_required_slot(tmp_path: Path) -> None:
    corpus = _corpus(USED, tmp_path)
    _rewrite(corpus, lambda document: document.update({"phases": None}))

    census = SourceCensus.build(SourceSnapshot.read(corpus))

    assert [failure.code for failure in census.row_failures] == [
        RowFailureCode.COLLECTION_NULL_FORBIDDEN
    ]


def test_v068_apply_refuses_a_live_session_and_names_the_close_verb(
    tmp_path: Path, backup_home: Path
) -> None:
    corpus = _corpus(USED, tmp_path)

    def reopen(document: dict[str, Any]) -> None:
        for session in document["agent_sessions"].values():
            session["status"] = "active"
            session["ended_at"] = None

    _rewrite(corpus, reopen)
    target = _opted_in_target(corpus, tmp_path, home=backup_home)
    plan = plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)

    with pytest.raises(MigrationNotQuiescentError) as caught:
        apply_cutover(
            apply_request_for(corpus=corpus, target_root=target, plan=plan),
            applied_at=APPLIED_AT,
        )

    assert "active_session=1" in str(caught.value)
    assert "eawf session close <session id>" in str(caught.value)


def test_v068_shipped_allowlist_is_the_pinned_one() -> None:
    assert DEFAULT_ALLOWLIST_PATH.read_bytes() == ALLOWLIST.read_bytes()


def test_cli_plan_without_allowlist_reads_the_shipped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(
        app,
        [
            "--json",
            "migrate",
            "epoch2",
            "--plan",
            "--snapshot-root",
            str(FRESH),
            "--workspace-key",
            WORKSPACE_KEY,
            "--project-key",
            PROJECT_KEY,
            "--repository-key",
            REPOSITORY_KEY,
            "--default-track-key",
            DEFAULT_TRACK_KEY,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    expected = plan_cutover(plan_request_for(FRESH), sealed_at=APPLIED_AT)
    assert payload["manifest_digest"] == expected.manifest.manifest_digest


def _apply_used(tmp_path: Path, home: Path) -> tuple[Path, CutoverResult]:
    corpus = _corpus(USED, tmp_path)
    target = _opted_in_target(corpus, tmp_path, home=home)
    plan = plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)
    assert plan.manifest.unresolved_rows == ()
    result = apply_cutover(
        apply_request_for(corpus=corpus, target_root=target, plan=plan), applied_at=APPLIED_AT
    )
    return target, result


def test_v068_goals_land_as_track_outcomes(tmp_path: Path, backup_home: Path) -> None:
    target, result = _apply_used(tmp_path, backup_home)
    state = DisposableTarget.require(target).generation_path(result.generation_id)

    outcomes = document_rows(
        read_document(state / GENERATION_DOCUMENT), Epoch2Collection.TRACK_OUTCOME
    )

    assert sorted(outcomes) == ["G01", "G02"]
    assert outcomes["G01"]["payload"]["payload"]["title"] == "Establish USED project intent"


def test_v068_hypothesis_lands_as_a_readable_legacy_row(tmp_path: Path, backup_home: Path) -> None:
    source = json.loads((USED / "document.json").read_text(encoding="utf-8"))["hypotheses"]
    target, result = _apply_used(tmp_path, backup_home)
    state = DisposableTarget.require(target).generation_path(result.generation_id)

    legacy = {
        line.record_key: line.payload
        for line in read_ledger_records(
            ledger_path(state / GENERATION_DOCUMENT, Epoch2Collection.LEGACY)
        )
    }

    assert legacy["legacy:hypotheses/H01-01"]["payload"] == source["H01-01"]


def _unreconciled(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[tuple[cutover.StagedRecord, ...]], tuple[cutover.StagedRecord, ...]],
) -> tuple[Path, str]:
    """Apply the used corpus with the write tampered, and return the refusal."""
    corpus = _corpus(USED, tmp_path)
    target = _opted_in_target(corpus, tmp_path, home=home)
    plan = plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)
    staged = cutover.staged_records
    monkeypatch.setattr(cutover, "staged_records", lambda corpus: tamper(staged(corpus)))

    with pytest.raises(MigrationRowsUnreconciledError) as caught:
        apply_cutover(
            apply_request_for(corpus=corpus, target_root=target, plan=plan),
            applied_at=APPLIED_AT,
        )

    assert caught.value.code == "migration_rows_unreconciled"
    return target, str(caught.value)


def test_v068_apply_refuses_a_generation_that_dropped_a_counted_row(
    tmp_path: Path, backup_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop_goal(records: tuple[cutover.StagedRecord, ...]) -> tuple[cutover.StagedRecord, ...]:
        return tuple(record for record in records if record.record_key != "G01")

    target, message = _unreconciled(tmp_path, backup_home, monkeypatch, drop_goal)

    assert "goals -> track_outcome counted 2, written 1 (missing: G01; unsourced: -)" in message
    assert not DisposableTarget.require(target).selection_path.exists()
    assert not DisposableTarget.require(target).marker_path.exists()


def test_v068_apply_refuses_a_generation_holding_an_uncounted_row(
    tmp_path: Path, backup_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def add_goal(records: tuple[cutover.StagedRecord, ...]) -> tuple[cutover.StagedRecord, ...]:
        goal = next(record for record in records if record.record_key == "G01")
        payload = copy.deepcopy(goal.payload)
        payload["origin"]["source_id"] = "G99"
        return (*records, goal.model_copy(update={"record_key": "G99", "payload": payload}))

    target, message = _unreconciled(tmp_path, backup_home, monkeypatch, add_goal)

    assert "goals -> track_outcome counted 2, written 3 (missing: -; unsourced: G99)" in message
    assert not DisposableTarget.require(target).selection_path.exists()
