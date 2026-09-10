"""Sessions and worktrees import as records, never as live capabilities.

The full-shape corpus carries 45 session rows and 336 worktree rows, and
not one of them describes something epoch 2 can act on: the sessions have
ended and the checkouts are gone. Every one of them imports as an
immutable legacy envelope that mints nothing -- in particular no
WorkLease, which is the one epoch-2 record a worktree row looks like it
should produce and the one that would block real work if it did.

An active worktree is where the temptation is sharpest, because the
source really does say the checkout was open. That fact is kept as an
annotation on the envelope rather than converted into a lease, so an
operator can find the open checkouts without the runtime believing
somebody holds them.

The ledger half of the import asks a different question: not what a row
means but whether it survived. Artifacts, memory notes and audits travel
byte for byte, and only their addressing changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.envelopes import (
    ENVELOPE_MINTS,
    LEGACY_ALIAS_PREFIX,
    RUN_RECORD,
    SESSION_ACTIVE_ANNOTATION,
    WORK_LEASE_RECORD,
    WORKTREE_ACTIVE_ANNOTATION,
    WORKTREE_ACTIVE_STATUS,
    EnvelopeCollection,
    EnvelopeImportPlan,
    LedgerCollection,
    envelope_rule_payload,
    legacy_alias,
    map_ledger_row,
    map_legacy_envelope,
)
from eawf.kernel.migration.epoch2.errors import MigrationDuplicateKeyError
from eawf.kernel.migration.epoch2.origins import build_legacy_origin, source_digest
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.rules import canonical_json
from tests.unit.kernel.migration.conftest import EPOCH1_FULL_SNAPSHOT, build_full_corpus_plan

SOURCE_SCHEMA_VERSION = "1.19"

#: The census the full-shape corpus pins for the two envelope collections.
SESSION_ROW_COUNT = 45
WORKTREE_ROW_COUNT = 336

#: The audit population the union import has to reproduce.
AUDIT_DOCUMENT_ROWS = 81
AUDIT_STORE_ONLY_ROWS = 24


def _worktree_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 worktree row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "WT001",
        "wave_id": "P01-I01-W01",
        "branch": "feature/demo-wt001",
        "base_branch": "feature/demo",
        "path": ".ea/worktrees/WT001",
        "status": "merged",
        "created_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _session_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 agent-session row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "S001",
        "role": "executor",
        "runtime": "claude",
        "scope_id": "P01-I01-W01",
        "status": "closed",
        "started_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_every_session_row_imports_as_an_immutable_legacy_envelope(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    sessions = full_corpus_plan.envelopes.for_collection(EnvelopeCollection.AGENT_SESSIONS)

    assert len(sessions) == SESSION_ROW_COUNT
    for envelope in sessions:
        assert envelope.source_collection == "agent_sessions"
        assert envelope.origin.kind == "legacy"
        assert envelope.payload["id"] == envelope.source_id
        assert envelope.payload_digest == source_digest(envelope.payload)


def test_every_worktree_row_imports_as_an_immutable_legacy_envelope(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    worktrees = full_corpus_plan.envelopes.for_collection(EnvelopeCollection.WORKTREES)

    assert len(worktrees) == WORKTREE_ROW_COUNT
    for envelope in worktrees:
        assert envelope.source_collection == "worktrees"
        assert envelope.origin.kind == "legacy"
        assert envelope.payload["id"] == envelope.source_id


def test_no_envelope_mints_a_work_lease_or_a_run(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """A record of what happened is never a claim on what is happening."""
    for envelope in full_corpus_plan.envelopes.envelopes:
        assert envelope.minted_records == ()
        assert WORK_LEASE_RECORD not in envelope.minted_records
        assert RUN_RECORD not in envelope.minted_records
    assert ENVELOPE_MINTS[EnvelopeCollection.WORKTREES] == ()
    assert ENVELOPE_MINTS[EnvelopeCollection.AGENT_SESSIONS] == ()


def test_an_active_worktree_carries_its_annotation_instead_of_a_lease(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    worktrees = full_corpus_plan.envelopes.for_collection(EnvelopeCollection.WORKTREES)
    active = [
        envelope for envelope in worktrees if envelope.payload["status"] == WORKTREE_ACTIVE_STATUS
    ]

    assert active
    for envelope in active:
        assert envelope.annotations == (WORKTREE_ACTIVE_ANNOTATION,)
        assert envelope.minted_records == ()
        assert envelope.origin.confidence == "supported"


def test_a_closed_worktree_earns_no_annotation(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    worktrees = full_corpus_plan.envelopes.for_collection(EnvelopeCollection.WORKTREES)
    closed = [
        envelope for envelope in worktrees if envelope.payload["status"] != WORKTREE_ACTIVE_STATUS
    ]

    assert closed
    for envelope in closed:
        assert envelope.annotations == ()
        assert envelope.origin.confidence == "exact"


def test_an_active_session_carries_its_own_annotation(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    sessions = full_corpus_plan.envelopes.for_collection(EnvelopeCollection.AGENT_SESSIONS)
    active = [envelope for envelope in sessions if envelope.payload["status"] == "active"]

    assert active
    for envelope in active:
        assert envelope.annotations == (SESSION_ACTIVE_ANNOTATION,)
        assert envelope.minted_records == ()


def test_artifacts_round_trip_byte_identically_under_a_reindexed_alias(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """The bytes are the artifact; the alias is only how it is reached."""
    artifacts = full_corpus_plan.envelopes.for_ledger(LedgerCollection.ARTIFACTS)

    assert [row.source_id for row in artifacts] == ["ART-01", "ART-02", "ART-03"]
    for row in artifacts:
        assert row.ledger == "artifact"
        assert row.alias == f"{LEGACY_ALIAS_PREFIX}:artifacts/{row.source_id}"
        assert row.payload_digest == source_digest(row.payload)
        assert row.store_only is False


def test_artifact_payloads_equal_their_source_rows_byte_for_byte(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    source = json.loads((EPOCH1_FULL_SNAPSHOT / "document.json").read_text(encoding="utf-8"))[
        "artifacts"
    ]

    for row in full_corpus_plan.envelopes.for_ledger(LedgerCollection.ARTIFACTS):
        assert canonical_json(row.payload) == canonical_json(source[row.source_id])


def test_memory_index_rows_land_in_the_memory_ledger(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    memory = full_corpus_plan.envelopes.for_ledger(LedgerCollection.MEMORY_INDEX)

    assert [row.source_id for row in memory] == ["MEM01", "MEM02"]
    for row in memory:
        assert row.ledger == "memory"
        assert row.alias == f"{LEGACY_ALIAS_PREFIX}:memory_index/{row.source_id}"


def test_audits_import_as_the_union_of_the_document_and_the_ledger(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """A row only the store held is imported, not lost, and is marked as such."""
    audits = full_corpus_plan.envelopes.for_ledger(LedgerCollection.AUDITS)
    store_only = [row for row in audits if row.store_only]

    assert len(audits) == AUDIT_DOCUMENT_ROWS + AUDIT_STORE_ONLY_ROWS
    assert len(store_only) == AUDIT_STORE_ONLY_ROWS
    assert len({row.source_id for row in audits}) == len(audits)


def test_every_imported_row_is_reachable_under_exactly_one_alias(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    index = full_corpus_plan.envelopes.alias_index()
    rows = (*full_corpus_plan.envelopes.envelopes, *full_corpus_plan.envelopes.ledger_rows)

    assert len(index) == len(rows)
    for row in rows:
        assert index[row.alias] is row


def test_envelope_import_plan_build_is_deterministic() -> None:
    assert build_full_corpus_plan().envelopes == build_full_corpus_plan().envelopes


def test_envelope_import_plan_build_over_an_empty_document() -> None:
    plan = EnvelopeImportPlan.build(
        document={},
        audit_ledger_rows=(),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert plan.envelopes == ()
    assert plan.ledger_rows == ()
    assert plan.alias_index() == {}


def test_envelope_import_plan_build_over_a_single_row() -> None:
    plan = EnvelopeImportPlan.build(
        document={"worktrees": {"WT001": _worktree_row()}, "agent_sessions": None},
        audit_ledger_rows=(),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert len(plan.envelopes) == 1
    assert plan.for_collection(EnvelopeCollection.AGENT_SESSIONS) == ()
    assert plan.envelopes[0].source_id == "WT001"


def test_envelope_import_plan_build_skips_a_row_that_is_not_an_object() -> None:
    plan = EnvelopeImportPlan.build(
        document={"worktrees": {"WT001": "not a row", "": _worktree_row()}},
        audit_ledger_rows=(),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert plan.envelopes == ()


def test_envelope_import_plan_build_imports_a_store_only_audit_once() -> None:
    plan = EnvelopeImportPlan.build(
        document={"audits": {"A001": {"id": "A001"}}},
        audit_ledger_rows=(
            {"id": "A001", "kind": "audit"},
            {"id": "A002", "kind": "audit"},
            {"id": "A002", "kind": "audit"},
            {"id": ""},
            {"kind": "audit"},
        ),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )
    audits = plan.for_ledger(LedgerCollection.AUDITS)

    assert [(row.source_id, row.store_only) for row in audits] == [
        ("A001", False),
        ("A002", True),
    ]


def test_alias_index_rejects_two_rows_claiming_one_alias() -> None:
    """A collision would make the reindex silently lose whichever came first."""
    envelope = map_legacy_envelope(
        collection=EnvelopeCollection.WORKTREES,
        source_id="WT001",
        row=_worktree_row(),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )
    plan = EnvelopeImportPlan(envelopes=(envelope, envelope), ledger_rows=())

    with pytest.raises(MigrationDuplicateKeyError, match="WT001"):
        plan.alias_index()


def test_legacy_alias_is_collection_qualified() -> None:
    assert legacy_alias(collection="worktrees", source_id="WT001") == "legacy:worktrees/WT001"
    assert legacy_alias(collection="artifacts", source_id="WT001") == "legacy:artifacts/WT001"


def test_legacy_alias_rejects_an_empty_part() -> None:
    with pytest.raises(ValueError, match="collection and a source id"):
        legacy_alias(collection="", source_id="WT001")
    with pytest.raises(ValueError, match="collection and a source id"):
        legacy_alias(collection="worktrees", source_id="")


def test_map_legacy_envelope_rejects_an_empty_source_id() -> None:
    with pytest.raises(ValueError):
        map_legacy_envelope(
            collection=EnvelopeCollection.WORKTREES,
            source_id="",
            row=_worktree_row(),
            source_schema_version=SOURCE_SCHEMA_VERSION,
        )


def test_map_legacy_envelope_rejects_a_row_that_cannot_be_digested() -> None:
    with pytest.raises(TypeError):
        map_legacy_envelope(
            collection=EnvelopeCollection.AGENT_SESSIONS,
            source_id="S001",
            row=_session_row(summary={1, 2}),
            source_schema_version=SOURCE_SCHEMA_VERSION,
        )


def test_map_legacy_envelope_copies_the_row_rather_than_aliasing_it() -> None:
    """A later mutation of the source dict must not reach the imported record."""
    row = _worktree_row()
    envelope = map_legacy_envelope(
        collection=EnvelopeCollection.WORKTREES,
        source_id="WT001",
        row=row,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )
    row["status"] = "active"

    assert envelope.payload["status"] == "merged"
    assert envelope.annotations == ()


def test_map_ledger_row_marks_a_store_only_row() -> None:
    row = map_ledger_row(
        collection=LedgerCollection.AUDITS,
        source_id="A900",
        row={"id": "A900", "kind": "audit"},
        source_schema_version=SOURCE_SCHEMA_VERSION,
        store_only=True,
    )

    assert row.store_only is True
    assert row.ledger == "audit"
    assert row.alias == "legacy:audits/A900"


def test_build_legacy_origin_rejects_an_empty_schema_version() -> None:
    with pytest.raises(ValidationError):
        build_legacy_origin(
            source_kind="worktrees",
            source_id="WT001",
            row=_worktree_row(),
            source_schema_version="",
            confidence="exact",
        )


def test_source_digest_is_stable_across_key_order() -> None:
    assert source_digest({"a": 1, "b": 2}) == source_digest({"b": 2, "a": 1})
    assert source_digest({}).startswith("sha256:")


def test_envelope_rule_payload_states_that_nothing_is_minted() -> None:
    payload = envelope_rule_payload()

    assert payload["envelope_mints"] == {"agent_sessions": [], "worktrees": []}
    assert payload["never_minted"] == ["run", "work_lease"]
    assert payload["alias_prefix"] == LEGACY_ALIAS_PREFIX
    assert payload["active_annotations"]["worktrees"] == WORKTREE_ACTIVE_ANNOTATION
    assert payload["ledger_targets"] == {
        "artifacts": "artifact",
        "memory_index": "memory",
        "audits": "audit",
    }
