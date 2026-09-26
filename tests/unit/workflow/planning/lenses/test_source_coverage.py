"""Lens 2: source atomization.

A source atom is the plan's own claim that some unit of the design was
considered; the lens exists because a claim like that costs nothing to
make and nothing in the schema forces it to be honest. Coverage is
binary and total: every declared atom maps to a criterion this plan's
own Tasks declare, or is named in ``dropped_atoms`` with a reason. There
is no third option, which is what makes "silently drop it and say
nothing" the one failure mode this lens exists to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.revision import PlanRefusal, PlanRevisionAdvanced

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0041"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"
HEAD = "a" * 40

OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}


def _criterion(id_: str) -> dict[str, Any]:
    """Return one loader-valid criterion payload keyed *id_*."""
    return {
        "id": id_,
        "text": "the described behaviour holds",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    }


def _design_fixture_atoms() -> list[dict[str, Any]]:
    """Return the three-atom multi-item design fixture, fully covered.

    ``AT-01`` and ``AT-02`` each map to one of the plan's two criteria;
    ``AT-03`` is a deliberate typed drop. All three dispositions are
    exercised so a caller that breaks any one of them has somewhere to
    fail.
    """
    return [
        {
            "atom_id": "AT-01",
            "text": "the design requires the first behaviour",
            "mapped_criterion_ids": ["CR-01"],
        },
        {
            "atom_id": "AT-02",
            "text": "the design requires the second behaviour",
            "mapped_criterion_ids": ["CR-02"],
        },
        {"atom_id": "AT-03", "text": "the design mentions an out-of-scope behaviour"},
    ]


def _design_fixture_drops() -> list[dict[str, Any]]:
    """Return the typed drop that discharges ``AT-03``."""
    return [{"atom_ref": "AT-03", "reason": "out of scope for this Milestone"}]


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid plan body payload, covered by the design fixture."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0041",
            "primary_track_ref": TRACK,
            "title": "Ship the described behaviour",
            "outcome": "An operator observes the described behaviour end to end.",
            "appetite": "M",
            "exclusions": ["none"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "observe the described behaviour",
                    "expected_observation": "the behaviour is present",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
        "batches": [{"urn": BATCH, "repository_ref": REPOSITORY}],
        "tasks": [
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [_criterion("CR-01"), _criterion("CR-02")],
            }
        ],
        "citations": [],
        "source_atoms": _design_fixture_atoms(),
        "dropped_atoms": _design_fixture_drops(),
    }
    payload.update(overrides)
    return payload


def make_body(**overrides: Any) -> PlanBody:
    """Return a validated plan body."""
    return PlanBody.model_validate(body_payload(**overrides))


def make_document() -> dict[str, Any]:
    """Return a locked-document fixture that resolves every world binding."""
    return {
        "track": {"TRK-RUNTIME": {"revision": 1, "status": "ACTIVE", "policy": {"revision": 1}}},
        "repository": {"REP-EAWF": {"head_sha": HEAD}},
    }


def make_proposal(body: PlanBody, *, key: str = "PRV-0001") -> PlanRevisionProposal:
    """Return the strict proposal *body* is submitted as."""
    return PlanRevisionProposal.model_validate(
        {"key": key, "author": OPERATOR, "body": body.model_dump(mode="json")}
    )


def _without_second_mapping() -> list[dict[str, Any]]:
    """Return the design fixture atoms with ``AT-02``'s mapping removed."""
    atoms = _design_fixture_atoms()
    atoms[1] = {**atoms[1], "mapped_criterion_ids": []}
    return atoms


# ---- the positive case ----------------------------------------------------------


def test_source_atomization_lens_admits_the_fully_covered_design_fixture() -> None:
    """Every atom of the design fixture maps to a criterion or a typed drop."""
    findings = [f for f in run_plan_lenses(make_body()) if f.lens is PlanLens.SOURCE_ATOMIZATION]

    assert findings == []


# ---- CR-02: removing a mapping is caught -----------------------------------------


def test_source_atomization_lens_flags_a_removed_mapping() -> None:
    """Removing one sub-item's mapping, with no compensating drop, is a finding."""
    body = make_body(source_atoms=_without_second_mapping())

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.SOURCE_ATOMIZATION]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.SOURCE_ATOM_UNMAPPED
    assert findings[0].entity_refs == ("AT-02",)


# ---- dangling references ----------------------------------------------------------


def test_source_atomization_lens_flags_a_mapping_to_an_undeclared_criterion() -> None:
    """A mapping naming a criterion id this plan never declares is dangling."""
    atoms = _design_fixture_atoms()
    atoms[0] = {**atoms[0], "mapped_criterion_ids": ["CR-99"]}
    body = make_body(source_atoms=atoms)

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.SOURCE_ATOMIZATION]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.SOURCE_ATOM_REFERENCE_UNRESOLVED
    assert "CR-99" in findings[0].entity_refs


def test_source_atomization_lens_flags_a_drop_of_an_undeclared_atom() -> None:
    """A typed drop naming an atom this plan's source_atoms never declares is dangling."""
    body = make_body(dropped_atoms=[{"atom_ref": "AT-99", "reason": "never declared"}])

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.SOURCE_ATOMIZATION]

    dangling = [f for f in findings if f.code is PlanFindingCode.SOURCE_ATOM_REFERENCE_UNRESOLVED]
    assert "AT-99" in dangling[0].entity_refs


# ---- boundary: no atoms declared at all --------------------------------------------


def test_source_atomization_lens_is_vacuous_with_no_declared_atoms() -> None:
    """A plan that declares no source atoms has nothing for the lens to check."""
    body = make_body(source_atoms=[], dropped_atoms=[])

    assert [f for f in run_plan_lenses(body) if f.lens is PlanLens.SOURCE_ATOMIZATION] == []


# ---- CR-02: gate-fire proofs -------------------------------------------------------


def test_submit_validates_the_fully_covered_design_fixture() -> None:
    """The positive case: a fully-covered design fixture reaches VALIDATED."""
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(make_body()), at=AT)

    assert isinstance(outcome, PlanRevisionAdvanced)


def test_submit_refuses_a_removed_mapping_writing_nothing() -> None:
    """Removing one sub-item's mapping blocks at submit and leaves the document untouched.

    Deleting the lens-runner call in ``validate_plan_proposal`` reds this
    test: the proposal is otherwise a well-typed, fully-resolving plan.
    """
    body = make_body(source_atoms=_without_second_mapping())
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_source_atomization"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}
