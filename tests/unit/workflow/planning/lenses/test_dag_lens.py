"""Lens 1 (schema/reference), lens 3 (outcome coverage), lens 4 (DAG).

The DAG lens is the one whose subject the schema itself cannot check: a
``depends_on`` edge is just another ``TaskUrn``, so nothing at the loader
tells a resolving edge from a dangling one, or an acyclic graph from a
cyclic one. Both are checked here, over Kahn's algorithm run in stable
key order so two spellings of one cyclic graph name the same remainder.

The two gate-fire proofs at the bottom exercise :func:`validate_plan_proposal`
directly rather than the full daemon transaction: the function reads its
``document`` argument and writes nothing, which they prove by snapshotting
the document before the call and asserting it is byte-for-byte unchanged
after -- checking against an empty ``batch``/``task`` collection the
fixture never populates in the first place would hold whether or not the
function ever wrote to it.
"""

from __future__ import annotations

import copy
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
FOREIGN_SLOT = "eawf://WSP-MAIN/PRJ-OTHER/REP-OTHER"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0040"
BATCH = f"{SLOT}/batch/BAT-0001"
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


def _task(urn: str, *, batch_ref: str = BATCH, depends_on: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return one loader-valid Task payload, keyed off *urn*'s own suffix."""
    suffix = urn.rsplit("-", 1)[-1]
    return {
        "urn": urn,
        "batch_ref": batch_ref,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": [_criterion(f"CR-{suffix}")],
        "depends_on": list(depends_on),
    }


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0040",
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
        "tasks": [_task(f"{SLOT}/task/EAWF-0001")],
        "citations": [],
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


# ---- the positive case --------------------------------------------------------


def test_run_plan_lenses_admits_a_valid_plan() -> None:
    """A single Batch, single Task plan clears all four lenses."""
    assert run_plan_lenses(make_body()) == ()


# ---- lens 1: schema/reference --------------------------------------------------


def test_schema_reference_lens_flags_a_cross_scope_batch() -> None:
    """A Batch addressed outside the Milestone's own workspace/project is caught."""
    foreign_batch = f"{FOREIGN_SLOT}/batch/BAT-0001"
    body = make_body(
        batches=[{"urn": foreign_batch, "repository_ref": REPOSITORY}],
        tasks=[_task(f"{SLOT}/task/EAWF-0001", batch_ref=foreign_batch)],
    )

    findings = run_plan_lenses(body)

    assert len(findings) == 1
    assert findings[0].lens is PlanLens.SCHEMA_REFERENCE
    assert findings[0].code is PlanFindingCode.CROSS_SCOPE_MISMATCH
    assert foreign_batch in findings[0].entity_refs


# ---- lens 3: outcome coverage --------------------------------------------------


def test_outcome_coverage_lens_flags_a_batch_no_task_reaches() -> None:
    """A planned Batch nobody targets reaches no criterion."""
    unreached = f"{SLOT}/batch/BAT-0002"
    body = make_body(
        batches=[
            {"urn": BATCH, "repository_ref": REPOSITORY},
            {"urn": unreached, "repository_ref": REPOSITORY},
        ]
    )

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.OUTCOME_COVERAGE]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.OUTCOME_BATCH_UNREACHED
    assert findings[0].entity_refs == (unreached,)


# ---- lens 4: DAG ----------------------------------------------------------------


def test_dag_lens_admits_a_valid_chain() -> None:
    """A Task that depends on a Task the same plan creates is not a finding."""
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(tasks=[_task(first), _task(second, depends_on=(first,))])

    assert run_plan_lenses(body) == ()


def test_dag_lens_flags_a_dangling_dependency() -> None:
    """A dependency on a Task the plan never creates is a missing node, not a cycle."""
    ghost = f"{SLOT}/task/EAWF-9999"
    body = make_body(tasks=[_task(f"{SLOT}/task/EAWF-0001", depends_on=(ghost,))])

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.DAG]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.DAG_DEPENDENCY_UNRESOLVED
    assert ghost in findings[0].entity_refs


def test_dag_lens_flags_a_two_task_cycle() -> None:
    """Two Tasks each waiting on the other resolve nothing, ever."""
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(tasks=[_task(first, depends_on=(second,)), _task(second, depends_on=(first,))])

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.DAG]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.DAG_CYCLE_DETECTED
    assert set(findings[0].entity_refs) == {first, second}


def test_dag_lens_flags_a_self_dependency() -> None:
    """A Task that depends on itself is the one-node boundary of a cycle."""
    looped = f"{SLOT}/task/EAWF-0001"
    body = make_body(tasks=[_task(looped, depends_on=(looped,))])

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.DAG]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.DAG_CYCLE_DETECTED
    assert findings[0].entity_refs == (looped,)


# ---- CR-01: gate-fire proofs ----------------------------------------------------


def test_submit_refuses_a_task_cycle_writing_nothing() -> None:
    """A Task cycle blocks at submit and leaves the document untouched.

    Deleting the lens-runner call in ``validate_plan_proposal`` reds the
    ``outcome`` assertion below: the proposal is otherwise a well-typed,
    fully-resolving plan. The document is compared against a snapshot
    taken before the call, not against an empty ``batch``/``task``
    collection the fixture never populates -- the fixture never having
    those keys to begin with would satisfy that check whether or not the
    function ever wrote to it.
    """
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(tasks=[_task(first, depends_on=(second,)), _task(second, depends_on=(first,))])
    document = make_document()
    before = copy.deepcopy(document)

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_dag"
    assert document == before


def test_submit_refuses_an_unmapped_source_atom_writing_nothing() -> None:
    """An unmapped source atom blocks at submit and leaves the document untouched.

    Deleting the lens-runner call in ``validate_plan_proposal`` reds the
    ``outcome`` assertion below the same way the cycle case does, for the
    source-atomization lens instead of the DAG lens.
    """
    body = make_body(
        source_atoms=[{"atom_id": "AT-01", "text": "the design names this requirement"}]
    )
    document = make_document()
    before = copy.deepcopy(document)

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_source_atomization"
    assert document == before
