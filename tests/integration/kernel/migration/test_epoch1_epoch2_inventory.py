"""The epoch-1 source census, end to end over three pinned snapshots.

``epoch1-full`` carries the shape of the real corpus: 38 top-level keys,
ten of them serialized as JSON null, and an audit ledger holding 24 rows
the document never had. ``epoch1-empty-collections`` widens the empty
case so both drop-proof forms appear at once. ``epoch1-malformed`` holds
nothing but bad rows, one per failure code, so a pass over it proves the
census reports every offending row rather than stopping at the first.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.audits import AuditUnionCensus, check_audit_union
from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    DropProofForm,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationCollectionOmittedError,
    MigrationCollectionUnknownError,
    MigrationCountMismatchError,
    MigrationDuplicateKeyError,
    MigrationRowValidationError,
    MigrationSourceMutatedError,
    MigrationSourceUnreadableError,
)
from eawf.kernel.migration.epoch2.rows import (
    COLLECTION_ROW_CONTRACT_INDEX,
    RowFailureCode,
    validate_collection,
)
from eawf.kernel.migration.epoch2.snapshot import (
    CONFIG_DIRECTORY,
    DOCUMENT_LOCATOR,
    REGISTRY_LOCATOR,
    STORE_DIRECTORY,
    TELEMETRY_LOCATOR,
    SourceSnapshot,
    SourceSurface,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
EMPTY_SNAPSHOT = FIXTURES / "epoch1-empty-collections" / "snapshot"
MALFORMED_SNAPSHOT = FIXTURES / "epoch1-malformed" / "snapshot"

# The ten keys the epoch-1 document serializes as JSON null.
NULL_SLOTS = (
    "claims",
    "fleet_run",
    "health",
    "hypotheses",
    "mcp_grants",
    "mcp_servers",
    "open_questions",
    "outcomes",
    "tracks",
    "workspace",
)

# The six present-but-empty containers the empty-collections fixture adds.
ZERO_ROW_SLOTS = (
    "close_attempts",
    "indexes",
    "plugins",
    "wave_dependency_barriers",
    "wave_dependency_bindings",
    "wave_integrations",
)


@pytest.fixture(scope="module")
def full_census() -> SourceCensus:
    """The census of the full-shape epoch-1 snapshot."""
    return SourceCensus.build(SourceSnapshot.read(FULL_SNAPSHOT))


@pytest.fixture(scope="module")
def empty_census() -> SourceCensus:
    """The census of the empty-collections epoch-1 snapshot."""
    return SourceCensus.build(SourceSnapshot.read(EMPTY_SNAPSHOT))


@pytest.fixture(scope="module")
def malformed_census() -> SourceCensus:
    """The census of the all-bad-rows epoch-1 snapshot."""
    return SourceCensus.build(SourceSnapshot.read(MALFORMED_SNAPSHOT))


@pytest.fixture
def writable_snapshot(tmp_path: Path) -> Path:
    """A throwaway copy of the full snapshot the test may corrupt."""
    destination = tmp_path / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, destination)
    return destination


def _rewrite_document(root: Path, mutate: Callable[[dict[str, Any]], object]) -> None:
    """Apply ``mutate`` to the snapshot's decoded document and write it back."""
    path = root / DOCUMENT_LOCATOR
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def test_source_snapshot_read_pins_every_declared_surface() -> None:
    snapshot = SourceSnapshot.read(FULL_SNAPSHOT)
    identity = snapshot.identity

    assert identity.locators_for(SourceSurface.DOCUMENT) == (DOCUMENT_LOCATOR,)
    assert identity.locators_for(SourceSurface.REGISTRY) == (REGISTRY_LOCATOR,)
    assert identity.locators_for(SourceSurface.TELEMETRY) == (TELEMETRY_LOCATOR,)
    assert identity.locators_for(SourceSurface.STORE) == (
        f"{STORE_DIRECTORY}/audit.jsonl",
        f"{STORE_DIRECTORY}/decision.jsonl",
    )
    assert identity.locators_for(SourceSurface.CONFIG) == (
        f"{CONFIG_DIRECTORY}/base.yaml",
        f"{CONFIG_DIRECTORY}/overlay.yaml",
    )
    document_digest = identity.digest_for(DOCUMENT_LOCATOR)
    assert document_digest.byte_length == (FULL_SNAPSHOT / DOCUMENT_LOCATOR).stat().st_size
    assert len(identity.snapshot_digest) == 64


def test_source_snapshot_read_digests_are_stable_across_reads() -> None:
    first = SourceSnapshot.read(FULL_SNAPSHOT).identity
    second = SourceSnapshot.read(FULL_SNAPSHOT).identity

    assert first == second


def test_source_snapshot_read_rejects_a_missing_document(writable_snapshot: Path) -> None:
    (writable_snapshot / DOCUMENT_LOCATOR).unlink()

    with pytest.raises(MigrationSourceUnreadableError, match=DOCUMENT_LOCATOR):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_missing_store_directory(writable_snapshot: Path) -> None:
    shutil.rmtree(writable_snapshot / STORE_DIRECTORY)

    with pytest.raises(MigrationSourceUnreadableError, match=STORE_DIRECTORY):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_missing_config_directory(writable_snapshot: Path) -> None:
    shutil.rmtree(writable_snapshot / CONFIG_DIRECTORY)

    with pytest.raises(MigrationSourceUnreadableError, match=CONFIG_DIRECTORY):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_missing_registry(writable_snapshot: Path) -> None:
    (writable_snapshot / REGISTRY_LOCATOR).unlink()

    with pytest.raises(MigrationSourceUnreadableError, match=REGISTRY_LOCATOR):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_duplicated_collection_key(writable_snapshot: Path) -> None:
    (writable_snapshot / DOCUMENT_LOCATOR).write_text(
        '{"phases": {}, "phases": {}}', encoding="utf-8"
    )

    with pytest.raises(MigrationDuplicateKeyError, match="phases"):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_document_that_is_not_an_object(
    writable_snapshot: Path,
) -> None:
    (writable_snapshot / DOCUMENT_LOCATOR).write_text("[]", encoding="utf-8")

    with pytest.raises(MigrationSourceUnreadableError, match="expected a JSON object"):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_document_that_is_not_json(
    writable_snapshot: Path,
) -> None:
    (writable_snapshot / DOCUMENT_LOCATOR).write_text("{oops", encoding="utf-8")

    with pytest.raises(MigrationSourceUnreadableError, match="not valid JSON"):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_rejects_a_ledger_line_that_is_not_an_object(
    writable_snapshot: Path,
) -> None:
    ledger = writable_snapshot / STORE_DIRECTORY / "audit.jsonl"
    ledger.write_text('{"id": "A001"}\n"not a row"\n', encoding="utf-8")

    with pytest.raises(MigrationSourceUnreadableError, match="line 2"):
        SourceSnapshot.read(writable_snapshot)


def test_source_snapshot_read_accepts_a_ledger_with_no_rows(writable_snapshot: Path) -> None:
    (writable_snapshot / STORE_DIRECTORY / "audit.jsonl").write_text("\n", encoding="utf-8")

    snapshot = SourceSnapshot.read(writable_snapshot)

    assert snapshot.ledger("audit") == ()
    assert SourceCensus.build(snapshot).audits.ledger_rows == 0


def test_source_snapshot_ledger_rejects_an_absent_ledger(writable_snapshot: Path) -> None:
    (writable_snapshot / STORE_DIRECTORY / "audit.jsonl").unlink()
    snapshot = SourceSnapshot.read(writable_snapshot)

    with pytest.raises(MigrationSourceUnreadableError, match="audit"):
        snapshot.ledger("audit")


def test_source_snapshot_verify_unchanged_accepts_an_untouched_snapshot() -> None:
    SourceSnapshot.read(FULL_SNAPSHOT).verify_unchanged()


def test_source_snapshot_verify_unchanged_rejects_a_mutated_surface(
    writable_snapshot: Path,
) -> None:
    snapshot = SourceSnapshot.read(writable_snapshot)
    (writable_snapshot / TELEMETRY_LOCATOR).write_text('{"schema_version": "9.9"}', "utf-8")

    with pytest.raises(MigrationSourceMutatedError, match=TELEMETRY_LOCATOR):
        snapshot.verify_unchanged()


def test_source_snapshot_verify_unchanged_rejects_a_removed_surface(
    writable_snapshot: Path,
) -> None:
    snapshot = SourceSnapshot.read(writable_snapshot)
    (writable_snapshot / REGISTRY_LOCATOR).unlink()

    with pytest.raises(MigrationSourceUnreadableError, match=REGISTRY_LOCATOR):
        snapshot.verify_unchanged()


def test_source_snapshot_identity_digest_for_rejects_an_unread_locator() -> None:
    identity = SourceSnapshot.read(FULL_SNAPSHOT).identity

    with pytest.raises(KeyError):
        identity.digest_for("store/nonexistent.jsonl")


def test_source_census_build_covers_every_declared_collection(full_census: SourceCensus) -> None:
    censused = tuple(row.source_collection for row in full_census.collections)

    assert len(censused) == 38
    assert censused == tuple(sorted(COLLECTION_DISPOSITION_INDEX))
    assert set(censused) == set(COLLECTION_DISPOSITION_INDEX)


def test_source_census_build_rejects_an_omitted_collection(writable_snapshot: Path) -> None:
    _rewrite_document(writable_snapshot, lambda document: document.pop("worktrees"))

    with pytest.raises(MigrationCollectionOmittedError, match="worktrees"):
        SourceCensus.build(SourceSnapshot.read(writable_snapshot))


def test_source_census_build_rejects_an_unknown_collection(writable_snapshot: Path) -> None:
    _rewrite_document(writable_snapshot, lambda document: document.update({"sprints": {}}))

    with pytest.raises(MigrationCollectionUnknownError, match="sprints"):
        SourceCensus.build(SourceSnapshot.read(writable_snapshot))


def test_source_census_build_records_null_not_empty_for_null_slots(
    full_census: SourceCensus,
    empty_census: SourceCensus,
) -> None:
    for census in (full_census, empty_census):
        nulls = {
            name
            for name, form in census.proof_forms().items()
            if form is DropProofForm.NULL_NOT_EMPTY
        }
        assert nulls == set(NULL_SLOTS)
        for name in NULL_SLOTS:
            # A null slot has no container, so it has no row count either.
            assert census.collection(name).row_count is None


def test_source_census_build_records_zero_rows_for_empty_containers(
    empty_census: SourceCensus,
) -> None:
    zeroes = {
        name for name, form in empty_census.proof_forms().items() if form is DropProofForm.ZERO_ROWS
    }

    assert zeroes == set(ZERO_ROW_SLOTS)
    for name in ZERO_ROW_SLOTS:
        assert empty_census.collection(name).row_count == 0


def test_source_census_build_counts_rows_for_a_populated_collection(
    full_census: SourceCensus,
) -> None:
    waves = full_census.collection("waves")

    assert waves.row_count == 3
    assert waves.proof_form is None
    assert full_census.collection("urn").row_count is None
    assert full_census.collection("urn").proof_form is None


def test_source_census_build_reports_no_failures_for_the_full_fixture(
    full_census: SourceCensus,
) -> None:
    assert full_census.row_failures == ()
    full_census.require_importable()


def test_source_census_build_is_deterministic() -> None:
    first = SourceCensus.build(SourceSnapshot.read(FULL_SNAPSHOT))
    second = SourceCensus.build(SourceSnapshot.read(FULL_SNAPSHOT))

    assert first == second


def test_source_census_build_carries_the_registry_rule_versions(
    full_census: SourceCensus,
) -> None:
    assert tuple(rule.rule_id for rule in full_census.mapping_rules) == ("DOM-004", "DOM-045")


def test_source_census_collection_rejects_an_unknown_name(full_census: SourceCensus) -> None:
    with pytest.raises(KeyError):
        full_census.collection("sprints")


def test_source_census_build_pins_the_snapshot_identity(full_census: SourceCensus) -> None:
    assert full_census.identity == SourceSnapshot.read(FULL_SNAPSHOT).identity


def test_audit_union_census_build_imports_each_id_once(full_census: SourceCensus) -> None:
    audits = full_census.audits

    assert audits.document_rows == 81
    assert audits.ledger_rows == 105
    assert audits.union_rows == 105
    assert audits.store_only_imported == 24
    assert audits.union_rows == audits.document_rows + audits.store_only_imported


def test_audit_union_census_build_resolves_iter_refs_against_the_union(
    full_census: SourceCensus,
) -> None:
    # Two iters name audits that exist; the third names one that never did.
    assert full_census.audits.refs_resolving_in_neither == ("A999",)


def test_audit_union_census_build_on_the_empty_collections_fixture(
    empty_census: SourceCensus,
) -> None:
    audits = empty_census.audits

    assert (audits.document_rows, audits.ledger_rows, audits.union_rows) == (2, 3, 3)
    assert audits.store_only_imported == 1


def test_audit_union_census_build_accepts_empty_populations() -> None:
    census = AuditUnionCensus.build(document_ids=(), ledger_ids=(), iter_audit_refs=())

    assert census.union_rows == 0
    assert census.store_only_imported == 0
    assert census.refs_resolving_in_neither == ()


def test_audit_union_census_build_counts_a_repeated_id_once() -> None:
    census = AuditUnionCensus.build(
        document_ids=("A001",),
        ledger_ids=("A001", "A001", "A002"),
        iter_audit_refs=("A001",),
    )

    assert (census.document_rows, census.ledger_rows, census.union_rows) == (1, 2, 2)
    assert census.store_only_imported == 1


def _union_counts(
    *, document_rows: int, ledger_rows: int, union_rows: int, store_only_imported: int
) -> AuditUnionCensus:
    """Build an audit census straight from counts, bypassing reconciliation."""
    return AuditUnionCensus(
        document_rows=document_rows,
        ledger_rows=ledger_rows,
        union_rows=union_rows,
        store_only_imported=store_only_imported,
        refs_resolving_in_neither=(),
    )


def test_check_audit_union_accepts_a_union_covering_both_inputs() -> None:
    check_audit_union(
        _union_counts(document_rows=81, ledger_rows=105, union_rows=105, store_only_imported=24)
    )


def test_check_audit_union_rejects_a_union_smaller_than_either_input() -> None:
    with pytest.raises(MigrationCountMismatchError, match="fewer than its largest input"):
        check_audit_union(
            _union_counts(document_rows=81, ledger_rows=105, union_rows=80, store_only_imported=24)
        )


def test_check_audit_union_rejects_a_union_that_drops_store_only_rows() -> None:
    with pytest.raises(MigrationCountMismatchError, match="the store contributes"):
        check_audit_union(
            _union_counts(document_rows=81, ledger_rows=105, union_rows=105, store_only_imported=0)
        )


def test_source_census_build_reports_every_malformed_row(
    malformed_census: SourceCensus,
) -> None:
    # Nine bad document slots plus one bad ledger row, none of them skipped.
    assert len(malformed_census.row_failures) == 10


def test_source_census_build_locates_every_malformed_row(
    malformed_census: SourceCensus,
) -> None:
    assert set(malformed_census.failure_locators()) == {
        "phases",
        "waves",
        "urn",
        "iters.P01-I01.id",
        "backlog.<empty>",
        "decisions.D01",
        "incidents.INC01.cause",
        "artifacts.ART-01.kind",
        "audits.A001.created_at",
        "store/audit.jsonl#1",
    }


def test_source_census_build_covers_every_malformed_failure_code(
    malformed_census: SourceCensus,
) -> None:
    assert {failure.code for failure in malformed_census.row_failures} == set(RowFailureCode)


def test_source_census_build_reports_a_malformed_ledger_row(
    malformed_census: SourceCensus,
) -> None:
    ledger_failures = [
        failure
        for failure in malformed_census.row_failures
        if failure.locator.startswith(f"{STORE_DIRECTORY}/")
    ]

    assert [failure.locator for failure in ledger_failures] == ["store/audit.jsonl#1"]
    assert ledger_failures[0].code is RowFailureCode.REQUIRED_FIELD_MISSING


def test_source_census_require_importable_names_every_malformed_locator(
    malformed_census: SourceCensus,
) -> None:
    with pytest.raises(MigrationRowValidationError) as raised:
        malformed_census.require_importable()

    message = str(raised.value)
    assert "10 epoch-1 rows fail their declared schema" in message
    for locator in malformed_census.failure_locators():
        assert locator in message


def test_source_census_require_importable_accepts_a_census_with_no_malformed_rows(
    empty_census: SourceCensus,
) -> None:
    assert empty_census.row_failures == ()
    empty_census.require_importable()


def test_source_census_build_censuses_a_malformed_document_in_full(
    malformed_census: SourceCensus,
) -> None:
    # A bad row never truncates the census: all 38 collections are still
    # reported, so the operator sees the whole corpus and every failure.
    assert len(malformed_census.collections) == 38


def test_validate_collection_rejects_an_undeclared_collection() -> None:
    with pytest.raises(KeyError):
        validate_collection(collection="sprints", value={})


def test_collection_row_contracts_are_total_over_the_disposition_table() -> None:
    assert set(COLLECTION_ROW_CONTRACT_INDEX) == set(COLLECTION_DISPOSITION_INDEX)
