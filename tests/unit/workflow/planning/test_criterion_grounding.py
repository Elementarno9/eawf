"""PLAN-028..032: criterion grounding, and its APPROVE-time falsification.

Two mechanisms are covered here and nothing else needs a daemon to check
them, because every rule under test is a pure schema or a pure function
of a record.

``CriterionSpec.grounding`` is a closed ``measured | assumed |
accepted-risk`` grade paired with ``contract_refs`` and
``accepted_risk_decision_ref``. Each grade admits exactly the citation it
names and no other: ``measured`` requires a resolving ``contract_refs``
entry and forbids a Decision reference, ``accepted-risk`` requires a
resolving Decision reference and forbids a contract reference, and
``assumed`` -- the default -- permits neither. ``accepted-risk`` is also
disjoint from ``waiver_reason``: the first grades a claim as knowingly
unverified while its gate keeps running, the second skips the gate
outright, and a criterion carrying both would let one field mean two
things.

The approval gate is the falsification fixture PLAN-032 names: a
PlanRevision carrying exactly one criterion, graded assumed, refuses the
``VALIDATED -> APPROVED`` edge; the same fixture regraded ``measured``
with a resolving contract, or ``accepted-risk`` with a resolving
Decision, both transition to ``APPROVED``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import CriterionGrounding, CriterionSpec
from eawf.kernel.state.epoch2.plan_revision import (
    PlanApproval,
    PlanBody,
    PlanRevision,
    PlanRevisionStatus,
    plan_content_digest,
)
from eawf.workflow.planning.revision import (
    PlanRefusal,
    PlanRefusalCode,
    PlanRevisionAdvanced,
    advance_plan_revision,
    ungrounded_approval_criteria,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "planning"
    / "grounding"
    / "single_criterion_body.json"
)

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
ACTION = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0031"
HEAD = "a" * 40
OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}

_CRITERION_BASE: dict[str, Any] = {
    "id": "CR-01",
    "text": "the sole criterion this plan carries is graded assumed",
    "kind": "functional_suitability",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "approval refuses while grounding stays assumed",
}


def load_fixture_body() -> dict[str, Any]:
    """Return the single-criterion plan body payload, freshly parsed."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_revision(body: PlanBody) -> PlanRevision:
    """Return a VALIDATED revision carrying *body*, ready for approval."""
    payload: dict[str, Any] = {
        "key": "PRV-0031",
        "revision": 1,
        "status": PlanRevisionStatus.VALIDATED.value,
        "author": OPERATOR,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "content_digest": plan_content_digest(body),
        "base_state_revision": 1,
        "policy_revision": 1,
        "head_bindings": [{"repository_ref": REPOSITORY, "head_sha": HEAD}],
        "body": body.model_dump(mode="json"),
        "approval": None,
    }
    return PlanRevision.model_validate(payload)


def make_approval(revision: PlanRevision) -> PlanApproval:
    """Return the receipt a human principal seals over *revision*."""
    return PlanApproval.model_validate(
        {
            "action_ref": ACTION,
            "approved_by": OPERATOR,
            "approved_at": AT.isoformat(),
            "content_digest": plan_content_digest(revision.body),
            "base_state_revision": revision.base_state_revision,
            "policy_revision": revision.policy_revision,
            "head_bindings": [item.model_dump(mode="json") for item in revision.head_bindings],
        }
    )


def approve(body: PlanBody) -> PlanRevisionAdvanced | PlanRefusal:
    """Advance a fresh VALIDATED revision over *body* to APPROVED."""
    revision = make_revision(body)
    return advance_plan_revision(
        revision, to=PlanRevisionStatus.APPROVED, at=AT, approval=make_approval(revision)
    )


# ---- CR-01 / PLAN-031 / PLAN-032: the gate-fire falsification fixture ------


def test_approval_refuses_the_sole_criterion_graded_assumed() -> None:
    """The fixture's one criterion sits at assumed; APPROVE refuses it."""
    body = PlanBody.model_validate(load_fixture_body())

    refused = approve(body)

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.TRANSITION_GUARD_FAILED
    assert refused.guard == "criteria_grounded_at_approval"
    assert "CR-01" in refused.detail


def test_approval_admits_the_fixture_regraded_measured() -> None:
    """Regrading the criterion measured, with a resolving contract, admits it."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    approved = approve(body)

    assert isinstance(approved, PlanRevisionAdvanced)
    assert approved.record.status is PlanRevisionStatus.APPROVED


def test_approval_admits_the_fixture_regraded_accepted_risk() -> None:
    """Regrading the criterion accepted-risk, with a resolving Decision, admits it."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-0001"
    )
    body = PlanBody.model_validate(payload)

    approved = approve(body)

    assert isinstance(approved, PlanRevisionAdvanced)
    assert approved.record.status is PlanRevisionStatus.APPROVED


# ---- PLAN-025: the measured grade is checked against a real resolver ------


def approve_with_resolver(
    body: PlanBody, *, contract_is_resolvable: Callable[[str], bool]
) -> PlanRevisionAdvanced | PlanRefusal:
    """Advance a fresh VALIDATED revision over *body*, with a contract resolver."""
    revision = make_revision(body)
    return advance_plan_revision(
        revision,
        to=PlanRevisionStatus.APPROVED,
        at=AT,
        approval=make_approval(revision),
        contract_is_resolvable=contract_is_resolvable,
    )


def test_approval_refuses_a_measured_criterion_whose_contract_ref_does_not_resolve() -> None:
    """The gate-fire proof: a resolver that says no refuses the same as assumed."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    refused = approve_with_resolver(body, contract_is_resolvable=lambda _: False)

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.TRANSITION_GUARD_FAILED
    assert refused.guard == "criteria_grounded_at_approval"
    assert "CR-01" in refused.detail


def test_approval_admits_a_measured_criterion_whose_contract_ref_resolves() -> None:
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    approved = approve_with_resolver(body, contract_is_resolvable=lambda _: True)

    assert isinstance(approved, PlanRevisionAdvanced)
    assert approved.record.status is PlanRevisionStatus.APPROVED


# ---- ungrounded_approval_criteria: the pure predicate the gate reads -------


def test_ungrounded_approval_criteria_empty_when_nothing_is_assumed() -> None:
    """Boundary: a plan whose sole criterion resolves names nothing."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    assert ungrounded_approval_criteria(body) == ()


def test_ungrounded_approval_criteria_names_only_the_assumed_criterion() -> None:
    """Two criteria on one Task: only the assumed one is named, by id pair."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"] = [
        dict(_CRITERION_BASE, id="CR-01", grounding="assumed"),
        dict(
            _CRITERION_BASE,
            id="CR-02",
            grounding="measured",
            contract_refs=["MCT-0001"],
        ),
    ]
    body = PlanBody.model_validate(payload)
    task_urn = str(body.tasks[0].urn)

    assert ungrounded_approval_criteria(body) == (f"{task_urn}/CR-01",)


# ---- ungrounded_approval_criteria: resolving measured contract_refs -------


def test_ungrounded_approval_criteria_skips_the_resolution_check_with_no_resolver() -> None:
    """With no resolver, a measured citation is trusted the way it always was."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    assert ungrounded_approval_criteria(body, contract_is_resolvable=None) == ()


def test_ungrounded_approval_criteria_flags_a_measured_criterion_that_does_not_resolve() -> None:
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)
    task_urn = str(body.tasks[0].urn)

    ungrounded = ungrounded_approval_criteria(body, contract_is_resolvable=lambda _: False)

    assert ungrounded == (f"{task_urn}/CR-01",)


def test_ungrounded_approval_criteria_admits_a_measured_criterion_that_resolves() -> None:
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=["MCT-0001"])
    body = PlanBody.model_validate(payload)

    assert ungrounded_approval_criteria(body, contract_is_resolvable=lambda _: True) == ()


def test_ungrounded_approval_criteria_requires_every_ref_of_a_measured_criterion_to_resolve() -> (
    None
):
    """One resolvable ref among several does not carry the others."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="measured", contract_refs=["MCT-0001", "MCT-0002"]
    )
    body = PlanBody.model_validate(payload)
    task_urn = str(body.tasks[0].urn)

    ungrounded = ungrounded_approval_criteria(
        body, contract_is_resolvable=lambda ref: ref == "MCT-0001"
    )

    assert ungrounded == (f"{task_urn}/CR-01",)


# ---- H1: resolving accepted-risk Decision refs the same way -----------------


def approve_with_decision_resolver(
    body: PlanBody, *, decision_is_resolvable: Callable[[str], bool]
) -> PlanRevisionAdvanced | PlanRefusal:
    """Advance a fresh VALIDATED revision over *body*, with a Decision resolver."""
    revision = make_revision(body)
    return advance_plan_revision(
        revision,
        to=PlanRevisionStatus.APPROVED,
        at=AT,
        approval=make_approval(revision),
        decision_is_resolvable=decision_is_resolvable,
    )


def test_approval_refuses_an_accepted_risk_criterion_whose_decision_ref_does_not_resolve() -> None:
    """H1 gate-fire proof: a resolver that says no refuses an unknown Decision id.

    Reverting the resolver wiring in ``advance_plan_revision`` reds this
    test: with no resolver passed, an accepted-risk grade with any
    non-empty citation admits unconditionally (see the fixture-regrade
    test above), the same gap ``DEC-bogus`` exploited.
    """
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-BOGUS"
    )
    body = PlanBody.model_validate(payload)

    refused = approve_with_decision_resolver(body, decision_is_resolvable=lambda _: False)

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.TRANSITION_GUARD_FAILED
    assert refused.guard == "criteria_grounded_at_approval"
    assert "CR-01" in refused.detail
    assert "Decision" in refused.detail


def test_approval_admits_an_accepted_risk_criterion_whose_decision_ref_resolves() -> None:
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="D67"
    )
    body = PlanBody.model_validate(payload)

    approved = approve_with_decision_resolver(body, decision_is_resolvable=lambda ref: ref == "D67")

    assert isinstance(approved, PlanRevisionAdvanced)
    assert approved.record.status is PlanRevisionStatus.APPROVED


def test_ungrounded_approval_criteria_skips_the_decision_resolution_check_with_no_resolver() -> (
    None
):
    """With no resolver, an accepted-risk citation is trusted the way it always was."""
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-BOGUS"
    )
    body = PlanBody.model_validate(payload)

    assert ungrounded_approval_criteria(body, decision_is_resolvable=None) == ()


def test_ungrounded_approval_criteria_flags_an_accepted_risk_criterion_that_does_not_resolve() -> (
    None
):
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-BOGUS"
    )
    body = PlanBody.model_validate(payload)
    task_urn = str(body.tasks[0].urn)

    ungrounded = ungrounded_approval_criteria(body, decision_is_resolvable=lambda _: False)

    assert ungrounded == (f"{task_urn}/CR-01",)


def test_ungrounded_approval_criteria_admits_an_accepted_risk_criterion_that_resolves() -> None:
    payload = load_fixture_body()
    payload["tasks"][0]["criteria"][0].update(
        grounding="accepted-risk", accepted_risk_decision_ref="D67"
    )
    body = PlanBody.model_validate(payload)

    ungrounded = ungrounded_approval_criteria(body, decision_is_resolvable=lambda ref: ref == "D67")

    assert ungrounded == ()


# ---- PLAN-028: the grounding enum admits exactly one grade's citation -----


def test_measured_criterion_requires_contract_refs() -> None:
    """A measured grade with no contract to resolve fails at the schema."""
    with pytest.raises(ValidationError, match="cites no contract_refs"):
        CriterionSpec.model_validate(dict(_CRITERION_BASE, grounding="measured"))


def test_measured_criterion_forbids_a_decision_ref() -> None:
    """A measured grade naming a Decision reference carries the wrong citation."""
    with pytest.raises(ValidationError, match="only accepted-risk carries"):
        CriterionSpec.model_validate(
            dict(
                _CRITERION_BASE,
                grounding="measured",
                contract_refs=["MCT-0001"],
                accepted_risk_decision_ref="DEC-0001",
            )
        )


def test_assumed_criterion_forbids_contract_refs() -> None:
    """The assumed default permits no citation at all (surplus denial)."""
    with pytest.raises(ValidationError, match="only measured carries"):
        CriterionSpec.model_validate(
            dict(_CRITERION_BASE, grounding="assumed", contract_refs=["MCT-0001"])
        )


def test_assumed_criterion_forbids_a_decision_ref() -> None:
    """The assumed default permits no Decision citation either."""
    with pytest.raises(ValidationError, match="only accepted-risk carries"):
        CriterionSpec.model_validate(
            dict(_CRITERION_BASE, grounding="assumed", accepted_risk_decision_ref="DEC-0001")
        )


def test_grounding_defaults_to_assumed_with_no_citation_required() -> None:
    """Boundary: omitting grounding entirely still validates, as assumed."""
    criterion = CriterionSpec.model_validate(_CRITERION_BASE)

    assert criterion.grounding is CriterionGrounding.ASSUMED
    assert criterion.contract_refs == ()
    assert criterion.accepted_risk_decision_ref is None


# ---- CR-02 / PLAN-029: accepted-risk is Decision-backed and waiver-disjoint


def test_accepted_risk_requires_a_resolving_decision() -> None:
    """accepted-risk with no Decision to resolve fails at the schema boundary."""
    with pytest.raises(ValidationError, match="cites no resolving Decision"):
        CriterionSpec.model_validate(dict(_CRITERION_BASE, grounding="accepted-risk"))


def test_accepted_risk_forbids_contract_refs() -> None:
    """accepted-risk naming a contract reference carries the wrong citation."""
    with pytest.raises(ValidationError, match="only measured carries"):
        CriterionSpec.model_validate(
            dict(
                _CRITERION_BASE,
                grounding="accepted-risk",
                accepted_risk_decision_ref="DEC-0001",
                contract_refs=["MCT-0001"],
            )
        )


def test_accepted_risk_with_waiver_reason_raises() -> None:
    """accepted-risk and waiver_reason are disjoint exits; both is refused."""
    with pytest.raises(ValidationError, match="disjoint exits"):
        CriterionSpec.model_validate(
            dict(
                _CRITERION_BASE,
                grounding="accepted-risk",
                accepted_risk_decision_ref="DEC-0001",
                waiver_reason="reviewed by hand",
            )
        )


def test_accepted_risk_with_a_resolving_decision_validates() -> None:
    """The positive case: a resolving Decision and no waiver both hold."""
    criterion = CriterionSpec.model_validate(
        dict(_CRITERION_BASE, grounding="accepted-risk", accepted_risk_decision_ref="DEC-0001")
    )

    assert criterion.grounding is CriterionGrounding.ACCEPTED_RISK
    assert criterion.waiver_reason is None
