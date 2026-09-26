"""Lens 6: criterion fidelity, CR-02.

The lens reuses :func:`~eawf.kernel.spec.common.assign_oracle_tier`
rather than re-deriving its rules, so "the verb-to-tier mapping is
total" and "the lens agrees with the tier compute" are the same fact
proved once. What the lens adds on top is turning that function's
``ValueError`` into a blocking :class:`PlanFinding` instead of letting
it propagate. A ``JUDGED`` clause without a ``jury_reason`` never gets
this far: :class:`~eawf.kernel.spec.common.CriterionSpec`'s own
validator refuses it at load, so the negative fixtures here use the
malformed clauses a loaded plan can still carry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.spec.common import (
    ObserveVerb,
    OracleTier,
    ResponseClause,
    assign_oracle_tier,
)
from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.revision import PlanRefusal, PlanRevisionAdvanced

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0043"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"
HEAD = "a" * 40

OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}

#: Every non-JUDGED observe verb, each already resolving through the
#: closed :data:`eawf.kernel.spec.common._VERB_TIER` map. Exercised in
#: :func:`test_criterion_fidelity_lens_admits_every_deterministic_verb`
#: to demonstrate the mapping's totality at the lens boundary, not just
#: at :func:`~eawf.kernel.spec.common.assign_oracle_tier` itself.
_DETERMINISTIC_VERBS: tuple[ObserveVerb, ...] = tuple(
    verb for verb in ObserveVerb if verb is not ObserveVerb.JUDGED
)

#: The tier each non-JUDGED verb must resolve to, pinned independently of
#: :data:`eawf.kernel.spec.common._VERB_TIER` so a verb that resolves
#: successfully to the WRONG tier is caught here too -- asserting only
#: that the lens raises no finding cannot tell "resolved correctly" from
#: "resolved, but to the wrong bucket".
_EXPECTED_TIER: dict[ObserveVerb, OracleTier] = {
    ObserveVerb.VALIDATES: OracleTier.T1_STATIC,
    ObserveVerb.MATCHES_PATTERN: OracleTier.T1_STATIC,
    ObserveVerb.EXITS: OracleTier.T2_STRUCTURAL,
    ObserveVerb.EMITS: OracleTier.T2_STRUCTURAL,
    ObserveVerb.TRANSITIONS_TO: OracleTier.T2_STRUCTURAL,
    ObserveVerb.TRIGGERS_ACTION: OracleTier.T2_STRUCTURAL,
    ObserveVerb.RENDERS_TOKEN: OracleTier.T3_SNAPSHOT,
    ObserveVerb.RETURNS: OracleTier.T4_CONTRACT,
    ObserveVerb.RAISES: OracleTier.T4_CONTRACT,
    ObserveVerb.HOLDS_FOR_ALL: OracleTier.T4_CONTRACT,
    ObserveVerb.FILE_MATCHES: OracleTier.T5_GOLDEN,
}


def _criterion(id_: str, *, response: dict[str, Any] | None = None) -> dict[str, Any]:
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
    if response is not None:
        payload["response"] = response
    return payload


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0043",
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
                "criteria": [_criterion("CR-0001")],
                "write_claims": ["src/eawf/product/base.py"],
            }
        ],
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


def _body_with_orphan_gate_ref() -> PlanBody:
    """Return a plan body whose one criterion names a gate kind nothing recognises."""
    return make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "write_claims": ["src/eawf/product/base.py"],
                "criteria": [
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
            }
        ]
    )


# ---- CR-02: the mapping is total over every deterministic verb --------------------


@pytest.mark.parametrize("verb", _DETERMINISTIC_VERBS, ids=lambda verb: verb.value)
def test_criterion_fidelity_lens_admits_every_deterministic_verb(verb: ObserveVerb) -> None:
    """Every non-JUDGED verb resolves its pinned tier and earns no finding.

    Asserting only ``findings == []`` cannot fail if ``assign_oracle_tier``
    silently resolved the WRONG tier for a verb -- an absent finding just
    means it did not raise. Comparing against :data:`_EXPECTED_TIER`
    first catches a verb that resolves successfully to the wrong bucket,
    not only one that fails to resolve at all.
    """
    quantifier = "forall" if verb is ObserveVerb.HOLDS_FOR_ALL else "single"
    locus = "hypothesis" if quantifier == "forall" else "pytest"
    response = ResponseClause.model_validate(
        {
            "observe": verb.value,
            "object": "the described behaviour holds",
            "locus": locus,
            "quantifier": quantifier,
        }
    )
    assert assign_oracle_tier(response) == _EXPECTED_TIER[verb]

    body = make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "write_claims": ["src/eawf/product/base.py"],
                "criteria": [
                    _criterion(
                        "CR-0001",
                        response={
                            "observe": verb.value,
                            "object": "the described behaviour holds",
                            "locus": locus,
                            "quantifier": quantifier,
                        },
                    )
                ],
            }
        ]
    )

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.CRITERION_FIDELITY]

    assert findings == []


def test_criterion_fidelity_lens_admits_a_criterion_with_no_response_clause() -> None:
    """A criterion authoring no response clause is not this lens's concern."""
    assert [f for f in run_plan_lenses(make_body()) if f.lens is PlanLens.CRITERION_FIDELITY] == []


# ---- CR-02: an orphan gate_ref is a finding -----------------------------------------


def test_criterion_fidelity_lens_flags_an_unresolved_gate_ref() -> None:
    """A gate_ref naming a gate kind nothing recognises is a finding."""
    body = _body_with_orphan_gate_ref()

    findings = [f for f in run_plan_lenses(body) if f.lens is PlanLens.CRITERION_FIDELITY]

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.CRITERION_FIDELITY_UNRESOLVED
    assert "not_a_real_gate_kind" in findings[0].message


# ---- CR-02: gate-fire proofs --------------------------------------------------------


def test_submit_validates_a_well_formed_response_clause() -> None:
    """The positive case: a criterion with a resolving response clause reaches VALIDATED."""
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(make_body()), at=AT)

    assert isinstance(outcome, PlanRevisionAdvanced)


def test_submit_refuses_an_unresolved_gate_ref_writing_nothing() -> None:
    """An orphan gate_ref blocks at submit and leaves the document untouched.

    Deleting the criterion-fidelity-lens call in ``validate_plan_proposal``
    reds this test: the proposal is otherwise a well-typed, fully-resolving
    plan.
    """
    document = make_document()

    outcome = validate_plan_proposal(
        document, proposal=make_proposal(_body_with_orphan_gate_ref()), at=AT
    )

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_criterion_fidelity"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}
