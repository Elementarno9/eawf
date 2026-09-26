"""Decisions, incidents, sandbox policies and the project keep their own key.

A Decision is cited by its ``D-*`` id and by the ``urn:eawf:v1:decision``
URN built from it, so the conversion is judged on one thing above all:
after it, the same id and the same URN still name the same row. The
other three collections ride the same converter and are held to the same
key-preserving contract, minus a URN they were never cited by.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.errors import MigrationFabricationDetectedError
from eawf.kernel.migration.epoch2.native_records import (
    NATIVE_RECORD_TARGETS,
    ImportedNativeRecord,
    NativeRecordCollection,
    NativeRecordImportPlan,
    map_native_record,
    native_record_rule_payload,
    recorded_project_code,
)
from eawf.kernel.migration.epoch2.origins import source_digest
from eawf.kernel.state.urn import parse as parse_urn
from eawf.kernel.store.tiers import Epoch2Collection

SOURCE_SCHEMA_VERSION = "1.19"
PROJECT_CODE = "DEMO"


def _decision(decision_id: str) -> dict[str, Any]:
    """Return one epoch-1 decision row keyed ``decision_id``."""
    return {
        "id": decision_id,
        "scope_id": PROJECT_CODE,
        "status": "active",
        "superseded_by": None,
        "title": f"Decision {decision_id}",
        "rationale": "recorded at the time of the call",
        "created_at": "2026-01-01T00:00:00Z",
    }


def _document(**collections: Any) -> dict[str, Any]:
    """Return an epoch-1 document carrying the project block and ``collections``."""
    return {"project": {"code": PROJECT_CODE, "title": "demo"}, **collections}


def _build(document: dict[str, Any]) -> NativeRecordImportPlan:
    return NativeRecordImportPlan.build(
        document=document, source_schema_version=SOURCE_SCHEMA_VERSION
    )


def test_map_native_record_keeps_the_d_id_and_its_urn() -> None:
    """The converted Decision is keyed and URN-addressed exactly as the source cited it."""
    row = _decision("D-SUP-01")
    record = map_native_record(
        collection=NativeRecordCollection.DECISIONS,
        record_key="D-SUP-01",
        row=row,
        project_code=PROJECT_CODE,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert record.record_key == "D-SUP-01"
    assert record.target is Epoch2Collection.DECISION
    assert record.urn == "urn:eawf:v1:decision:DEMO/D-SUP-01"
    parsed = parse_urn(record.urn)
    assert (parsed.kind, parsed.owner, parsed.id) == ("decision", PROJECT_CODE, "D-SUP-01")
    assert record.payload == row
    assert record.payload_digest == source_digest(row)
    assert record.origin.source_kind == "decisions"
    assert record.origin.source_id == "D-SUP-01"


def test_map_native_record_mints_no_urn_for_a_collection_never_cited_by_one() -> None:
    """An incident had no v1 URN kind, so none is invented at import."""
    record = map_native_record(
        collection=NativeRecordCollection.INCIDENTS,
        record_key="INC-P30-01",
        row={"id": "INC-P30-01", "status": "resolved"},
        project_code=PROJECT_CODE,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert record.urn is None
    assert record.target is Epoch2Collection.INCIDENT


def test_map_native_record_refuses_a_key_a_urn_cannot_spell() -> None:
    """A slash would split the URN id, so the key is refused rather than mangled."""
    with pytest.raises(ValueError, match="may not contain"):
        map_native_record(
            collection=NativeRecordCollection.DECISIONS,
            record_key="D01/extra",
            row=_decision("D01/extra"),
            project_code=PROJECT_CODE,
            source_schema_version=SOURCE_SCHEMA_VERSION,
        )


def test_map_native_record_accepts_the_longest_key_and_refuses_one_past_it() -> None:
    """The record key shares the ledger's 128-character bound."""
    longest = "D" * 128
    record = map_native_record(
        collection=NativeRecordCollection.INCIDENTS,
        record_key=longest,
        row={"id": longest},
        project_code=PROJECT_CODE,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )
    assert record.record_key == longest

    with pytest.raises(ValidationError, match="record_key"):
        map_native_record(
            collection=NativeRecordCollection.INCIDENTS,
            record_key=longest + "D",
            row={"id": longest + "D"},
            project_code=PROJECT_CODE,
            source_schema_version=SOURCE_SCHEMA_VERSION,
        )


def test_map_native_record_refuses_an_unknown_collection() -> None:
    """Only the four declared collections have a native target."""
    with pytest.raises(KeyError):
        map_native_record(
            collection="hypotheses",  # type: ignore[arg-type]
            record_key="H01-01",
            row={"id": "H01-01"},
            project_code=PROJECT_CODE,
            source_schema_version=SOURCE_SCHEMA_VERSION,
        )


def test_imported_native_record_refuses_an_empty_key() -> None:
    """A record with no key could not be cited at all."""
    with pytest.raises(ValidationError, match="record_key"):
        ImportedNativeRecord.model_validate(
            {
                "source_collection": "decisions",
                "record_key": "",
                "target": "decision",
                "urn": None,
                "origin": {"kind": "native"},
                "payload": {},
                "payload_digest": source_digest({}),
            }
        )


def test_recorded_project_code_reads_the_project_block() -> None:
    assert recorded_project_code(_document()) == PROJECT_CODE


@pytest.mark.parametrize(
    "project",
    [None, {}, {"code": ""}, {"code": 7}, "DEMO"],
    ids=["null", "empty", "empty-code", "non-string-code", "not-a-mapping"],
)
def test_recorded_project_code_refuses_a_document_that_names_no_project(project: Any) -> None:
    """A guessed code would re-address every Decision URN, so none is guessed."""
    with pytest.raises(MigrationFabricationDetectedError, match="no project code"):
        recorded_project_code({"project": project})


def test_native_record_import_plan_converts_every_declared_collection() -> None:
    """Each collection lands in its declared target under its own key."""
    plan = _build(
        _document(
            decisions={"D02": _decision("D02"), "D01": _decision("D01")},
            incidents={"INC01": {"id": "INC01"}},
            sandbox_policies={"SP01": {"id": "SP01"}},
        )
    )

    assert [(row.source_collection.value, row.record_key) for row in plan.records] == [
        ("decisions", "D01"),
        ("decisions", "D02"),
        ("incidents", "INC01"),
        ("sandbox_policies", "SP01"),
        ("project", PROJECT_CODE),
    ]
    assert all(row.target is NATIVE_RECORD_TARGETS[row.source_collection] for row in plan.records)


def test_native_record_import_plan_converts_only_the_project_when_nothing_else_is_held() -> None:
    """Empty and null collections convert to nothing; the project block still lands."""
    plan = _build(_document(decisions={}, incidents=None))

    assert [(row.source_collection, row.record_key) for row in plan.records] == [
        (NativeRecordCollection.PROJECT, PROJECT_CODE)
    ]


def test_native_record_import_plan_converts_a_single_decision() -> None:
    plan = _build(_document(decisions={"D01": _decision("D01")}))

    decisions = [
        row for row in plan.records if row.source_collection is NativeRecordCollection.DECISIONS
    ]
    assert [row.urn for row in decisions] == ["urn:eawf:v1:decision:DEMO/D01"]


def test_native_record_import_plan_converts_nothing_without_a_project_block() -> None:
    """With no project there is no owner for a URN, so nothing converts."""
    plan = _build({"decisions": {"D01": _decision("D01")}})

    assert plan.records == ()


def test_native_record_import_plan_refuses_a_project_block_without_a_code() -> None:
    with pytest.raises(MigrationFabricationDetectedError, match="no project code"):
        _build({"project": {"title": "nameless"}, "decisions": {"D01": _decision("D01")}})


def test_native_record_import_plan_skips_rows_that_are_not_objects() -> None:
    """A non-object row is the row contract's to refuse; the converter reads objects only."""
    plan = _build(_document(decisions={"D01": "not a row", "D02": _decision("D02")}))

    assert [
        row.record_key
        for row in plan.records
        if row.source_collection is NativeRecordCollection.DECISIONS
    ] == ["D02"]


def test_native_record_rule_payload_names_every_target_and_urn_kind() -> None:
    """The totality digest moves whenever a target or URN kind changes."""
    payload = native_record_rule_payload()

    assert payload["targets"] == {
        "decisions": "decision",
        "incidents": "incident",
        "sandbox_policies": "sandbox_policy",
        "project": "project",
    }
    assert payload["urn_kinds"] == {"decisions": "decision"}
