"""Canonical references resolve; legacy-reference strings never dangle.

Two kinds of string in an imported record look alike and are not. A
canonical reference names an epoch-2 record, and if the import never
wrote that record the reference is broken. A legacy-reference string --
a claim-session id, an audit id -- names an epoch-1 row that has no
epoch-2 referent at all; it is carried verbatim on the envelope and no
canonical edge is ever written for it.

That distinction is the whole reason the dangling count can be zero
without inventing anything. Minting a session to make a claim id resolve
would fabricate a source fact; carrying the id as a string asserts only
what the source recorded, which is that somebody once wrote that id down.

The counts below are measured from the staged fixtures rather than
asserted from a design document. The full-shape corpus carries three
audit-id strings, two of which resolve, and no claim-session id at all:
its wave rows predate the field. The ambiguous-history corpus is where
the claim-id carry is exercised, because it is written to hold both a
resolving and an unresolved one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from eawf.kernel.identity.keys import EntityKind
from eawf.kernel.migration.epoch2.errors import MigrationFabricationDetectedError
from eawf.kernel.migration.epoch2.lifecycle import (
    LIFECYCLE_FIELD_ROUTES,
    LifecycleFieldDisposition,
)
from eawf.kernel.migration.epoch2.runs import ATTEMPT_RUN_ID_SEPARATOR, MintedRun, RunSource
from eawf.kernel.migration.epoch2.snapshot import DOCUMENT_LOCATOR, SourceSnapshot
from eawf.kernel.migration.epoch2.validation import (
    CANONICAL_REFERENCE_KINDS,
    LEGACY_REFERENCE_POPULATIONS,
    CorpusIdentity,
    ImportedRow,
    ImportValidationReport,
    ReferenceCensus,
    StagedImport,
    run_source_address,
    source_populations,
    validation_rule_payload,
)
from tests.property.kernel.migration.conftest import (
    AMBIGUOUS_HISTORY_SNAPSHOT,
    EPOCH1_FULL_SNAPSHOT,
    validation_report,
)

#: What the full-shape corpus really carries, counted from the fixture.
#: Three iters name an audit; two of those audits exist in the union of
#: the document collection and the store ledger, and the third does not.
FULL_AUDIT_STRINGS_CARRIED = 3
FULL_AUDIT_STRINGS_RESOLVING = 2

#: No wave row of the full-shape corpus records a claim session, so the
#: claim-id population it carries is empty.
FULL_CLAIM_STRINGS_CARRIED = 0

#: What the ambiguous-history corpus carries: one resolving and one
#: unresolved string in each of the two reference populations.
AMBIGUOUS_STRINGS_CARRIED = 2
AMBIGUOUS_STRINGS_RESOLVING = 1

#: The unresolved strings that corpus names, neither of which is a
#: dangling reference.
UNRESOLVED_AUDIT_ID = "A404"
UNRESOLVED_CLAIM_SESSION_ID = "S404"

#: A canonical reference no corpus backs, injected to prove the write is
#: refused rather than repaired.
FABRICATED_TASK_REFERENCE = "P09-I09-W09"
STAGED_ITER = "P01-I01"


def _staged(*rows: ImportedRow) -> StagedImport:
    """A reduced import over ``rows`` under a fixed source identity."""
    return StagedImport(source_schema_version="1.19", project_code="DEMO", rows=rows)


def _row(**overrides: Any) -> ImportedRow:
    """One reduced import row, overridable field by field."""
    row: dict[str, Any] = {
        "address": "waves/P01-I01-W01",
        "source_kind": "waves",
        "source_id": "P01-I01-W01",
        "entity_kind": EntityKind.TASK,
        "identifier": "P01-I01-W01",
    }
    row.update(overrides)
    return ImportedRow(**row)


def _stage_with_a_fabricated_reference(root: Path) -> Path:
    """Copy the full-shape corpus and add a task reference nothing backs."""
    snapshot_root = root / "snapshot"
    shutil.copytree(EPOCH1_FULL_SNAPSHOT, snapshot_root)
    document_path = snapshot_root / DOCUMENT_LOCATOR
    document = json.loads(document_path.read_text(encoding="utf-8"))
    iter_row = document["iters"][STAGED_ITER]
    iter_row["wave_ids"] = [*iter_row.get("wave_ids", ()), FABRICATED_TASK_REFERENCE]
    document_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return snapshot_root


def test_full_corpus_has_no_dangling_canonical_reference(
    full_report: ImportValidationReport,
) -> None:
    """Every reference between imported records lands on a record."""
    census = full_report.references

    assert census.canonical_references > 0
    assert census.dangling == ()


def test_full_corpus_carries_its_audit_ids_as_legacy_strings(
    full_report: ImportValidationReport,
) -> None:
    """The unresolved audit id is carried, counted, and not a broken edge."""
    audits = full_report.references.count_for("audit_id")

    assert audits.carried == FULL_AUDIT_STRINGS_CARRIED
    assert audits.resolving == FULL_AUDIT_STRINGS_RESOLVING
    assert full_report.references.dangling == ()


def test_full_corpus_records_no_claim_session_id_to_carry(
    full_report: ImportValidationReport,
) -> None:
    """An empty population is a fact about the corpus, not a passing count."""
    claims = full_report.references.count_for("claim_session_id")

    assert claims.carried == FULL_CLAIM_STRINGS_CARRIED
    assert claims.population == "agent_sessions"


def test_full_corpus_import_passes_the_write_gate(
    full_report: ImportValidationReport,
) -> None:
    full_report.require_clean()

    assert full_report.references.dangling == ()
    assert full_report.fabrication.findings == ()


def test_ambiguous_corpus_carries_both_reference_populations_as_strings(
    ambiguous_report: ImportValidationReport,
) -> None:
    """Each population holds one string the source backs and one it does not."""
    for field in ("audit_id", "claim_session_id"):
        count = ambiguous_report.references.count_for(field)
        assert count.carried == AMBIGUOUS_STRINGS_CARRIED
        assert count.resolving == AMBIGUOUS_STRINGS_RESOLVING


def test_ambiguous_corpus_never_counts_a_legacy_string_as_dangling(
    ambiguous_report: ImportValidationReport,
) -> None:
    """The two unresolved strings appear nowhere in the dangling set."""
    dangling = {row.reference for row in ambiguous_report.references.dangling}

    assert ambiguous_report.references.dangling == ()
    assert UNRESOLVED_AUDIT_ID not in dangling
    assert UNRESOLVED_CLAIM_SESSION_ID not in dangling


def test_ambiguous_corpus_import_passes_the_write_gate(
    ambiguous_report: ImportValidationReport,
) -> None:
    """A corpus full of open questions still writes cleanly."""
    ambiguous_report.require_clean()

    assert ambiguous_report.aliases.census.is_injective
    assert ambiguous_report.fabrication.findings == ()


def test_a_fabricated_dangling_canonical_reference_is_named(tmp_path: Path) -> None:
    report = validation_report(_stage_with_a_fabricated_reference(tmp_path))
    dangling = report.references.dangling

    assert [row.reference for row in dangling] == [FABRICATED_TASK_REFERENCE]
    assert dangling[0].field == "task_refs"
    assert dangling[0].referent_kind is EntityKind.TASK
    assert dangling[0].source_address == f"iters/{STAGED_ITER}"


def test_a_fabricated_dangling_canonical_reference_fails_the_write(tmp_path: Path) -> None:
    report = validation_report(_stage_with_a_fabricated_reference(tmp_path))

    with pytest.raises(MigrationFabricationDetectedError) as excinfo:
        report.require_clean()
    assert excinfo.value.code == "migration_fabrication_detected"
    assert FABRICATED_TASK_REFERENCE in str(excinfo.value)


def test_the_canonical_reference_table_covers_every_routed_reference_field() -> None:
    """A reference field added to a route table cannot escape validation."""
    routed = {
        route.target_key
        for table in LIFECYCLE_FIELD_ROUTES.values()
        for route in table.values()
        if route.disposition is LifecycleFieldDisposition.NATIVE_CONVERSION
        and (route.target_key.endswith("_ref") or route.target_key.endswith("_refs"))
    }

    assert routed == set(CANONICAL_REFERENCE_KINDS)


def test_reference_census_of_an_import_with_no_rows_counts_nothing() -> None:
    census = ReferenceCensus.build(_staged())

    assert census.canonical_references == 0
    assert census.dangling == ()
    assert [row.carried for row in census.legacy_strings] == [0] * len(LEGACY_REFERENCE_POPULATIONS)


def test_reference_census_of_one_row_with_no_references_counts_nothing() -> None:
    census = ReferenceCensus.build(_staged(_row()))

    assert census.canonical_references == 0
    assert census.dangling == ()


def test_reference_census_count_for_rejects_an_undeclared_field() -> None:
    with pytest.raises(KeyError):
        ReferenceCensus.build(_staged()).count_for("commit")


def test_source_populations_unions_the_document_audits_with_the_ledger() -> None:
    snapshot = SourceSnapshot.read(AMBIGUOUS_HISTORY_SNAPSHOT)
    populations = source_populations(snapshot)

    assert populations["audits"] == frozenset({"A001"})
    assert populations["agent_sessions"] == frozenset({"S001"})
    assert UNRESOLVED_AUDIT_ID not in populations["audits"]


def test_run_source_address_separates_a_claim_run_from_an_attempt_run() -> None:
    claim = MintedRun(
        run_source=RunSource.RESOLVING_CLAIM,
        source_id="S001",
        wave_id="P01-I01-W01",
        attempt_key=None,
        exit_status=None,
        binding=None,
        status="TERMINAL_UNCLASSIFIED",
    )
    attempt = claim.model_copy(update={"run_source": RunSource.WAVE_ATTEMPT, "attempt_key": "1"})

    assert run_source_address(claim) == "P01-I01-W01#claim"
    assert run_source_address(attempt) == f"P01-I01-W01{ATTEMPT_RUN_ID_SEPARATOR}1"
    assert run_source_address(claim) != run_source_address(attempt)


def test_corpus_identity_renders_a_task_urn_under_the_project_code() -> None:
    identity = CorpusIdentity(
        workspace_key="WSP-DEFAULT", project_key="PRJ-DEMO", repository_key="REP-DEMO"
    )

    assert identity.project_code == "DEMO"
    assert (
        identity.urn_for(kind=EntityKind.TASK, entity_key="DEMO-0001")
        == "eawf://WSP-DEFAULT/PRJ-DEMO/REP-DEMO/task/DEMO-0001"
    )


@given(slot=st.sampled_from(["", "prj-demo", "1DEMO", "A" * 32]))
def test_corpus_identity_rejects_a_slot_that_is_not_a_symbol_key(slot: str) -> None:
    with pytest.raises(ValidationError):
        CorpusIdentity(workspace_key=slot, project_key="PRJ-DEMO", repository_key="REP-DEMO")


@pytest.mark.parametrize(
    "snapshot_root", [AMBIGUOUS_HISTORY_SNAPSHOT, EPOCH1_FULL_SNAPSHOT], ids=["ambiguous", "full"]
)
def test_validation_report_digest_is_byte_identical_on_a_second_run(snapshot_root: Path) -> None:
    """The cutover plan validates twice and compares; the two must agree."""
    first = validation_report(snapshot_root)
    second = validation_report(snapshot_root)

    assert first.report_digest == second.report_digest
    assert first.digest_payload() == second.digest_payload()
    assert first.aliases.census.identity_digest == second.aliases.census.identity_digest


def test_validation_report_digest_separates_two_corpora(
    full_report: ImportValidationReport,
    ambiguous_report: ImportValidationReport,
) -> None:
    assert full_report.report_digest != ambiguous_report.report_digest
    assert full_report.snapshot_digest != ambiguous_report.snapshot_digest


def test_validation_report_pins_the_snapshot_revision_it_covers(
    full_report: ImportValidationReport,
) -> None:
    snapshot = SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT)

    assert full_report.snapshot_digest == snapshot.identity.snapshot_digest
    assert full_report.mapping_rules


def test_validation_rule_payload_names_every_declared_reference_field() -> None:
    payload = validation_rule_payload()

    assert set(payload["canonical_references"]) == set(CANONICAL_REFERENCE_KINDS)
    assert payload["legacy_reference_populations"] == dict(LEGACY_REFERENCE_POPULATIONS)
