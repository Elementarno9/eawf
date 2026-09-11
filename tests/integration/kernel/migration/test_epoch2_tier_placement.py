"""The staged write puts every record in its tier, over the real corpus.

Three claims are asserted end to end against the ``epoch1-full`` fixture.

**Tier placement.** Every record that will never change again lands in an
append-only ledger, and the document keeps only work in flight -- so no
terminal Task survives in the document, and the manifest reports the
per-tier counts and the residual document size that prove it.

**A disposable index.** Deleting the whole derived index directory and
regenerating from the staged ledgers reproduces the previous bytes exactly,
which is what makes the tier safe to leave out of version control.

**A gate with teeth.** A staged document seeded with one terminal row
fails the cutover, naming the row, rather than being rewritten on every
mutation for the rest of the tree's life.

The 1.6 MB bound on the residual document is the cutover's own ceiling,
not a measurement of this fixture: the fixture's document is far smaller.
What the bound pins is that the staged document is sized by the work in
flight rather than by the corpus. It is not a claim that the repository
shrank -- the history moved into ledgers that are themselves committed, and
``ledger_byte_length`` records exactly where it went.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.cutover import (
    COMPACTING_STATUSES,
    HISTORY_RECORD_STATUS,
    ROW_PAYLOAD_FIELD,
    ROW_STATUS_FIELD,
    StagedRecord,
    document_residency_findings,
    require_document_holds_only_work_in_flight,
    stage_cutover,
    staged_records,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationTerminalInDocumentError,
    MigrationTierUndeclaredError,
)
from eawf.kernel.migration.epoch2.manifest import MigrationManifest, RollbackBoundary
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.store.index import regenerate_indexes
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import index_dir, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"

WORKSPACE_KEY = "WSP-DEFAULT"
PROJECT_KEY = "PRJ-DEMO"
REPOSITORY_KEY = "REP-DEMO"
SEALED_BY = "tier-placement-test"
RECORDED_AT = datetime(2026, 1, 1, tzinfo=UTC)

#: The ceiling the cutover holds the residual document to. The epoch-1
#: document it replaces was 5.9 MB because every finished record stayed in
#: the one file each mutation rewrote.
DOCUMENT_BYTE_CEILING = 1_600_000

#: What the pinned corpus stages, per tier. Pinned rather than derived so a
#: rule change that moves a record between tiers reds here instead of
#: silently agreeing with whatever the writer did.
EXPECTED_LEDGER_RECORDS = 511
EXPECTED_DOCUMENT_RECORDS = 3


def _staged_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(corpus_root, state_path)`` for a fresh copy of the fixture."""
    corpus = tmp_path / "staged"
    shutil.copytree(FULL_SNAPSHOT, corpus)
    return corpus, tmp_path / ".ea" / "state.json"


def _plan(corpus: Path) -> MigrationPlan:
    """Build one sealed plan over ``corpus`` under the shared test seal."""
    return plan_cutover(
        Epoch2PlanRequest(
            snapshot_root=str(corpus),
            allowlist_path=str(ALLOWLIST),
            workspace_key=WORKSPACE_KEY,
            project_key=PROJECT_KEY,
            repository_key=REPOSITORY_KEY,
            sealed_by=SEALED_BY,
        ),
        sealed_at=RECORDED_AT,
    )


def _stage(tmp_path: Path) -> tuple[Path, MigrationManifest]:
    """Run one staged cutover over a fresh corpus copy in ``tmp_path``."""
    corpus, state_path = _staged_tree(tmp_path)
    manifest = stage_cutover(
        plan=_plan(corpus),
        snapshot_root=corpus,
        allowlist_path=ALLOWLIST,
        state_path=state_path,
        recorded_at=RECORDED_AT,
    )
    return state_path, manifest


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, MigrationManifest]:
    """One staged cutover over the pinned corpus, run once for the module."""
    return _stage(tmp_path_factory.mktemp("staged_cutover"))


@pytest.fixture(scope="module")
def corpus_plan() -> CorpusImportPlan:
    """The pinned corpus imported once, for the routing assertions."""
    return CorpusImportPlan.build(
        snapshot=SourceSnapshot.read(FULL_SNAPSHOT), allowlist_path=ALLOWLIST
    )


def test_no_terminal_task_survives_in_the_staged_document(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """Every Task the source finished is in the ledger, not the document."""
    state_path, _ = staged
    document = json.loads(state_path.read_text("utf-8"))
    statuses = {
        key: row[ROW_STATUS_FIELD] for key, row in document[Epoch2Collection.TASK.value].items()
    }

    assert statuses
    terminal = COMPACTING_STATUSES[Epoch2Collection.TASK]
    assert not {key for key, status in statuses.items() if status in terminal}
    assert document_residency_findings(state_path) == ()


def test_every_terminal_lifecycle_record_is_in_its_own_ledger(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """Milestones, Batches and Tasks each land in the ledger their table names."""
    state_path, _ = staged
    for collection in (Epoch2Collection.MILESTONE, Epoch2Collection.BATCH):
        records = read_ledger_records(ledger_path(state_path, collection))
        assert records
        assert all(record.collection is collection for record in records)
        assert all(record.status in COMPACTING_STATUSES[collection] for record in records)
    tasks = read_ledger_records(ledger_path(state_path, Epoch2Collection.TASK))
    assert {record.status for record in tasks} <= COMPACTING_STATUSES[Epoch2Collection.TASK]


def test_the_manifest_reports_per_tier_counts_and_a_bounded_document(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """The placement names what each tier received and what the document weighs."""
    state_path, manifest = staged
    placement = manifest.tier_placement
    assert placement is not None

    assert placement.records_in(StorageTier.LEDGER) == EXPECTED_LEDGER_RECORDS
    assert placement.records_in(StorageTier.DOCUMENT) == EXPECTED_DOCUMENT_RECORDS
    assert placement.document_record_count == EXPECTED_DOCUMENT_RECORDS
    assert placement.document_byte_length == state_path.stat().st_size
    assert placement.document_byte_length < DOCUMENT_BYTE_CEILING
    assert manifest.rollback_boundary is RollbackBoundary.STAGED
    assert manifest.backup is not None


def test_the_placement_accounts_for_every_addressed_record(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """The two tiers together hold exactly what the plan counted."""
    _, manifest = staged
    placement = manifest.tier_placement
    assert placement is not None
    census = manifest.target_census

    assert placement.total_records == census.total_rows - census.planned_rows


def test_the_ledgers_hold_the_history_the_document_stopped_rewriting(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """The bytes moved rather than vanished, and the placement says so."""
    _, manifest = staged
    placement = manifest.tier_placement
    assert placement is not None

    assert placement.ledger_byte_length > placement.document_byte_length
    assert placement.indexed_collections
    assert all(
        tier_for(collection) is StorageTier.LEDGER for collection in placement.indexed_collections
    )


def test_the_staged_write_is_idempotent_in_its_placement(tmp_path: Path) -> None:
    """Two staged writes over one corpus report the same placement digest."""
    _, first = _stage(tmp_path / "one")
    _, second = _stage(tmp_path / "two")

    assert first.tier_placement is not None
    assert second.tier_placement is not None
    assert first.tier_placement.placement_digest == second.tier_placement.placement_digest
    assert first.manifest_digest == second.manifest_digest


def test_index_regen_from_the_staged_ledgers_is_byte_identical(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """Deleting the whole staged index costs a rebuild and nothing else."""
    state_path, _ = staged
    directory = index_dir(state_path)
    before = {item.name: item.read_bytes() for item in sorted(directory.iterdir())}
    assert before

    shutil.rmtree(directory)
    assert not directory.exists()
    rebuilt = regenerate_indexes(state_path)

    after = {item.name: item.read_bytes() for item in sorted(directory.iterdir())}
    assert after == before
    assert set(rebuilt) == {Epoch2Collection(name.removesuffix(".index.json")) for name in before}


def test_index_regen_is_a_pure_function_of_the_ledger_bytes(
    staged: tuple[Path, MigrationManifest],
) -> None:
    """Regenerating twice without deleting reproduces the same bytes again."""
    state_path, _ = staged
    directory = index_dir(state_path)
    before = {item.name: item.read_bytes() for item in sorted(directory.iterdir())}

    regenerate_indexes(state_path)

    assert {item.name: item.read_bytes() for item in sorted(directory.iterdir())} == before


def test_a_document_seeded_with_a_terminal_row_fails_the_cutover(tmp_path: Path) -> None:
    """One finished Task left in the document refuses the write, by name."""
    corpus, state_path = _staged_tree(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "2",
                Epoch2Collection.TASK.value: {
                    "SEEDED-0001": {ROW_STATUS_FIELD: "COMPLETED", ROW_PAYLOAD_FIELD: {}}
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationTerminalInDocumentError) as excinfo:
        stage_cutover(
            plan=_plan(corpus),
            snapshot_root=corpus,
            allowlist_path=ALLOWLIST,
            state_path=state_path,
            recorded_at=RECORDED_AT,
        )

    message = str(excinfo.value)
    assert "task/SEEDED-0001" in message
    assert "COMPLETED is terminal" in message
    assert excinfo.value.code == "migration_terminal_in_document"


def test_a_document_row_with_no_readable_status_fails_the_gate(tmp_path: Path) -> None:
    """A row nobody can prove is in flight is refused like a terminal one."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({Epoch2Collection.BATCH.value: {"SEEDED-0002": {"intent": "x"}}}),
        encoding="utf-8",
    )

    with pytest.raises(MigrationTerminalInDocumentError, match="records no status"):
        require_document_holds_only_work_in_flight(state_path)


def test_an_empty_document_holds_nothing_the_gate_objects_to(tmp_path: Path) -> None:
    """The empty tree is the boundary case: no rows, no findings, no refusal."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"schema_version": "2"}), encoding="utf-8")

    assert document_residency_findings(state_path) == ()
    assert require_document_holds_only_work_in_flight(state_path) is None


def test_every_staged_record_routes_to_a_declared_ledger_collection(
    corpus_plan: CorpusImportPlan,
) -> None:
    """The routing is total over the import: no record lands off the table."""
    records = staged_records(corpus_plan)

    assert len(records) == EXPECTED_LEDGER_RECORDS + EXPECTED_DOCUMENT_RECORDS
    assert all(tier_for(record.collection) is StorageTier.LEDGER for record in records)
    assert len({(record.collection, record.record_key) for record in records}) == len(records)


def test_a_minted_run_is_history_and_never_waits_in_the_document() -> None:
    """An imported Run is a finished episode, so it never sits in the document."""
    for status in ("SUCCEEDED", "FAILED", "TERMINAL_UNCLASSIFIED"):
        record = StagedRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"W01#attempt-{status}",
            status=status,
            payload={},
        )
        assert record.belongs_in_ledger()


def test_an_in_flight_lifecycle_record_stays_in_the_document() -> None:
    """A Task still in flight is read and rewritten, so it stays put."""
    for status in ("DRAFT", "PLANNED", "CLAIMED", "RUNNING", "DROPPED"):
        record = StagedRecord(
            collection=Epoch2Collection.TASK,
            record_key=f"W01-{status}",
            status=status,
            payload={},
        )
        assert not record.belongs_in_ledger()


def test_a_collection_with_no_residency_rule_refuses_rather_than_guessing() -> None:
    """A ledger collection nobody wrote a residency rule for has no home."""
    record = StagedRecord(
        collection=Epoch2Collection.RELEASE,
        record_key="REL-0001",
        status="released",
        payload={},
    )

    with pytest.raises(MigrationTierUndeclaredError, match="no residency rule"):
        record.belongs_in_ledger()


def test_a_history_record_carries_the_declared_imported_status(
    corpus_plan: CorpusImportPlan,
) -> None:
    """A record with no lifecycle of its own says so rather than inventing one."""
    legacy = [
        record
        for record in staged_records(corpus_plan)
        if record.collection is Epoch2Collection.LEGACY
    ]

    assert legacy
    assert {record.status for record in legacy} == {HISTORY_RECORD_STATUS}


def test_a_staged_record_rejects_an_over_long_key() -> None:
    """A key past the ledger's bound is refused where it is built."""
    with pytest.raises(ValueError, match="record_key"):
        StagedRecord(
            collection=Epoch2Collection.TASK,
            record_key="W" * 129,
            status="DRAFT",
            payload={},
        )


def test_a_staged_record_rejects_an_empty_status() -> None:
    """A record with no status cannot be routed by terminality."""
    with pytest.raises(ValueError, match="status"):
        StagedRecord(
            collection=Epoch2Collection.TASK,
            record_key="W01",
            status="",
            payload={},
        )
