"""The plan that applies the backlog and criteria rules to a whole collection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus
from eawf.kernel.migration.epoch2.dispositions import DropProofForm
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.plan import BacklogImportPlan, CriteriaImportPlan
from eawf.kernel.migration.epoch2.status_map import (
    INTENT_FROM_TITLE_ANNOTATION,
    PRIORITY_DEFAULT_ANNOTATION,
)
from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    ObserveVerb,
    ProofLocus,
    QualityDimension,
    ResponseClause,
)
from tests.unit.kernel.migration.conftest import ALLOWLIST_PATH


def _plan(source: dict[str, dict[str, Any]] | None, corpus: Any) -> BacklogImportPlan:
    """Build a plan over ``source`` against the shared allowlist."""
    return BacklogImportPlan.build(source=source, corpus=corpus, allowlist_path=ALLOWLIST_PATH)


def test_backlog_import_plan_build_over_the_epoch1_full_corpus(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    plan = _plan(epoch1_full.backlog, epoch1_full.resolution_corpus())
    assert plan.drop_proof is None
    assert len(plan.rows) == 131
    dropped = [row for row in plan.rows if row.target_status == "DROPPED"]
    assert len(dropped) == 71
    assert all(row.resolution is not None for row in dropped)
    assert all(row.resolution is None for row in plan.rows if row.target_status == "DRAFT")
    assert tuple(rule.rule_id for rule in plan.mapping_rules) == (
        "DOM-004",
        "DOM-018",
        "DOM-041",
        "DOM-043",
        "DOM-044",
        "DOM-045",
    )


def test_backlog_import_plan_build_annotates_defaults_and_keeps_them_for_the_ledger(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    """A defaulted field is still marked defaulted once the row reaches its ledger."""
    corpus = epoch1_full.resolution_corpus()
    source = {"B900": {"status": "open", "title": "Linux CI matrix"}}
    plan = _plan(source, corpus)
    row = plan.rows[0]
    assert row.record["priority"] == "P2"
    assert row.record["intent"] == "Linux CI matrix"
    assert PRIORITY_DEFAULT_ANNOTATION in row.ledger_annotations
    assert INTENT_FROM_TITLE_ANNOTATION in row.ledger_annotations


def test_backlog_import_plan_build_marks_an_obsolete_row(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    source = {"B900": {"status": "open", "title": "Drop the wave integration record"}}
    plan = _plan(source, epoch1_full.resolution_corpus())
    assert plan.rows[0].obsolete is True
    assert plan.obsolescence.obsolete_ids == ("B900",)


def test_backlog_import_plan_build_on_a_null_collection_proves_null_not_empty(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    plan = _plan(None, epoch1_full.resolution_corpus())
    assert plan.drop_proof is DropProofForm.NULL_NOT_EMPTY
    assert plan.rows == ()


def test_backlog_import_plan_build_on_an_empty_collection_proves_zero_rows(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    plan = _plan({}, epoch1_full.resolution_corpus())
    assert plan.drop_proof is DropProofForm.ZERO_ROWS
    assert plan.obsolescence.rows_scanned == 0


def test_backlog_import_plan_build_unknown_status_fails_the_plan(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    source = {"B900": {"status": "parked", "title": "t"}}
    with pytest.raises(MigrationCountMismatchError):
        _plan(source, epoch1_full.resolution_corpus())


def test_backlog_import_plan_build_missing_allowlist_raises(
    epoch1_full: Epoch1BacklogCorpus, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        BacklogImportPlan.build(
            source={},
            corpus=epoch1_full.resolution_corpus(),
            allowlist_path=tmp_path / "absent.txt",
        )


def _criterion(criterion_id: str, *, gate_ids: list[str]) -> CriterionSpec:
    """Build an epoch-1 criterion for the criteria plan."""
    return CriterionSpec(
        id=criterion_id,
        text="the plan converts every epoch-1 criterion it is given",
        kind="attested",
        acceptance_style="binary",
        evidence_kind="attested",
        gate_ids=gate_ids,
        quality_dimension=QualityDimension.MAINTAINABILITY,
        measurable_signal="the criteria plan yields one imported criterion per source row",
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="the criteria plan",
            locus=ProofLocus.HUMAN,
            jury_reason="fixture criterion is satisfied by the focused test assertion",
        ),
    )


def _gate(gate_id: str, criterion_id: str) -> GateSpec:
    """Build an epoch-1 gate bound to ``criterion_id``."""
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind="regex_match",
        args={"pattern": "^ok$"},
        policy="block",
        cadence="every-wave",
    )


def test_criteria_import_plan_build_converts_every_criterion() -> None:
    plan = CriteriaImportPlan.build(
        criteria=[_criterion("CR-01", gate_ids=["G-01"]), _criterion("CR-02", gate_ids=[])],
        gates=[_gate("G-01", "CR-01"), _gate("G-02", "CR-02")],
    )
    assert [item.id for item in plan.criteria] == ["CR-01", "CR-02"]
    assert plan.criteria[0].gate_ids == ("G-01",)
    assert plan.criteria[1].gate_ids == ("G-02",)
    assert all(item.policy_ref == "legacy-import" for item in plan.criteria)


def test_criteria_import_plan_build_on_no_criteria() -> None:
    assert CriteriaImportPlan.build(criteria=[], gates=[]).criteria == ()


def test_criteria_import_plan_build_consumes_a_generator_of_gates_once() -> None:
    """The gates iterable is materialised, so the second criterion still sees them."""
    plan = CriteriaImportPlan.build(
        criteria=[_criterion("CR-01", gate_ids=["G-01"]), _criterion("CR-02", gate_ids=["G-02"])],
        gates=(gate for gate in (_gate("G-01", "CR-01"), _gate("G-02", "CR-02"))),
    )
    assert plan.criteria[1].gate_ids == ("G-02",)


def test_criteria_import_plan_build_rejects_a_non_iterable_criteria_argument() -> None:
    with pytest.raises(TypeError):
        CriteriaImportPlan.build(criteria=17, gates=[])  # type: ignore[arg-type]
