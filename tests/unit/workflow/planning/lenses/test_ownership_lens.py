"""Lens 5 (ownership) and lens 6 (criterion fidelity), CR-01.

The ownership lens is the one whose subject spans Batches: two Tasks
overlap or they do not regardless of which Batch owns them, so the
check below never scopes its search to one Batch at a time. A valid
parallel graph -- two Tasks with disjoint claims, or overlapping claims
one ``depends_on`` edge orders -- is not a finding; two Tasks with an
overlapping claim and no ordering is.

The criterion fidelity lens's gate-fire proof lives here too, per CR-01:
an unresolved ``gate_ref`` is exactly the same "blocks at submit, writes
nothing" shape as an ownership overlap, so the second proof reuses the
same document/proposal fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.revision import PlanRefusal

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0042"
BATCH_A = f"{SLOT}/batch/BAT-0001"
BATCH_B = f"{SLOT}/batch/BAT-0002"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"
HEAD = "a" * 40

OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}


def _criterion(id_: str, **overrides: Any) -> dict[str, Any]:
    """Return one loader-valid criterion payload keyed *id_*."""
    payload: dict[str, Any] = {
        "id": id_,
        "text": "the described behaviour holds",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    }
    payload.update(overrides)
    return payload


def _task(
    urn: str,
    *,
    batch_ref: str = BATCH_A,
    depends_on: tuple[str, ...] = (),
    write_claims: tuple[str, ...] = (),
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return one loader-valid Task payload, keyed off *urn*'s own suffix."""
    suffix = urn.rsplit("-", 1)[-1]
    return {
        "urn": urn,
        "batch_ref": batch_ref,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": criteria if criteria is not None else [_criterion(f"CR-{suffix}")],
        "depends_on": list(depends_on),
        "write_claims": list(write_claims),
    }


def _milestone_payload() -> dict[str, Any]:
    """Return the one Milestone every fixture in this file shares."""
    return {
        "key": "MLS-0042",
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
    }


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid two-Batch, two-Task plan body payload.

    Both Batches are always reached (one Task apiece by default) so a
    caller overriding only ``tasks`` for a single-Batch scenario must also
    override ``batches``, and the outcome-coverage lens (which runs ahead
    of ownership in :data:`FIXED_LENS_ORDER`) never contaminates a
    gate-fire proof aimed at a later lens.
    """
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": _milestone_payload(),
        "batches": [
            {"urn": BATCH_A, "repository_ref": REPOSITORY},
            {"urn": BATCH_B, "repository_ref": REPOSITORY},
        ],
        "tasks": [
            _task(f"{SLOT}/task/EAWF-0001", write_claims=("src/eawf/product/base.py",)),
            _task(
                f"{SLOT}/task/EAWF-0002",
                batch_ref=BATCH_B,
                write_claims=("src/eawf/product/other.py",),
            ),
        ],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def single_batch_body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": _milestone_payload(),
        "batches": [{"urn": BATCH_A, "repository_ref": REPOSITORY}],
        "tasks": [
            _task(f"{SLOT}/task/EAWF-0001", write_claims=("src/eawf/product/base.py",)),
        ],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def make_body(**overrides: Any) -> PlanBody:
    """Return a validated two-Batch plan body."""
    return PlanBody.model_validate(body_payload(**overrides))


def make_single_batch_body(**overrides: Any) -> PlanBody:
    """Return a validated single-Batch plan body."""
    return PlanBody.model_validate(single_batch_body_payload(**overrides))


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


# ---- the positive case: a valid parallel graph -----------------------------------


def test_ownership_lens_admits_a_valid_parallel_graph() -> None:
    """Two Tasks with disjoint write claims and no ordering is not a finding."""
    assert [f for f in run_plan_lenses(make_body()) if f.lens is PlanLens.OWNERSHIP] == []


def test_ownership_lens_admits_an_overlap_ordered_by_depends_on() -> None:
    """An overlapping claim is fine once one Task depends on the other."""
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(
        tasks=[
            _task(first, write_claims=("src/eawf/product/base.py",)),
            _task(
                second,
                batch_ref=BATCH_B,
                depends_on=(first,),
                write_claims=("src/eawf/product/base.py",),
            ),
        ]
    )

    assert [f for f in run_plan_lenses(body) if f.lens is PlanLens.OWNERSHIP] == []


def test_ownership_lens_admits_an_overlap_ordered_transitively() -> None:
    """A depends_on chain two hops long still counts as ordering the ends."""
    first = f"{SLOT}/task/EAWF-0001"
    middle = f"{SLOT}/task/EAWF-0002"
    last = f"{SLOT}/task/EAWF-0003"
    body = make_body(
        tasks=[
            _task(first, write_claims=("src/eawf/product/base.py",)),
            _task(middle, batch_ref=BATCH_B, depends_on=(first,)),
            _task(
                last,
                batch_ref=BATCH_B,
                depends_on=(middle,),
                write_claims=("src/eawf/product/base.py",),
            ),
        ]
    )

    assert [f for f in run_plan_lenses(body) if f.lens is PlanLens.OWNERSHIP] == []


# ---- lens 5: an unordered overlap, including across Batches -----------------------


def test_ownership_lens_flags_an_unordered_overlap_across_batches() -> None:
    """Two Tasks in different Batches claiming the same path, unordered, conflict."""
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(
        tasks=[
            _task(first, write_claims=("src/eawf/product/base.py",)),
            _task(second, batch_ref=BATCH_B, write_claims=("src/eawf/product/base.py",)),
        ]
    )

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.OWNERSHIP]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.WRITE_CLAIM_OVERLAP_UNORDERED
    assert first in findings[0].entity_refs
    assert second in findings[0].entity_refs
    assert "src/eawf/product/base.py" in findings[0].entity_refs


# ---- CR-01: gate-fire proofs -------------------------------------------------------


def test_submit_refuses_an_unordered_write_claim_overlap_writing_nothing() -> None:
    """An unordered overlap blocks at submit and leaves the document untouched.

    Deleting the ownership-lens call in ``validate_plan_proposal`` reds
    this test: the proposal is otherwise a well-typed, fully-resolving
    plan.
    """
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(
        tasks=[
            _task(first, write_claims=("src/eawf/product/base.py",)),
            _task(second, batch_ref=BATCH_B, write_claims=("src/eawf/product/base.py",)),
        ]
    )
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_ownership"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}


def test_submit_refuses_a_criterion_with_an_unresolved_gate_ref_writing_nothing() -> None:
    """A response clause naming an unrecognised gate kind blocks at submit.

    Deleting the criterion-fidelity-lens call in ``validate_plan_proposal``
    reds this test the same way the overlap case does, for the fidelity
    lens instead of ownership.
    """
    task_urn = f"{SLOT}/task/EAWF-0001"
    body = make_single_batch_body(
        tasks=[
            _task(
                task_urn,
                write_claims=("src/eawf/product/base.py",),
                criteria=[
                    _criterion(
                        "CR-0001",
                        response={
                            "observe": "exits",
                            "object": "the described behaviour holds",
                            "locus": "pytest",
                            "quantifier": "single",
                            "gate_ref": "not_a_real_gate_kind",
                        },
                    )
                ],
            )
        ]
    )
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_criterion_fidelity"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}
