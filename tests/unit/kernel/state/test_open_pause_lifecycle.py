"""PLAN-047: an OpenPause projects to exactly one of seven situations, each naming its ender.

One case per row of the projection table, including a Hold placed by another
principal; the schema refusals that run before the projection; and the
distinctions the gallery asserts: held never escalated, a policy pause never
held, and no retry on the unknown-outcome card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.pause import (
    ENDS_WHEN,
    EscalationCause,
    OpenPause,
    PauseSituation,
    PauseVerb,
    project_pause,
)

pytestmark = pytest.mark.unit

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0003"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
EVIDENCE: Final = f"{CONTAINER}/evidence/EVD-0009"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def pause(reason: str = "provider", status: str = "OPEN", **overrides: Any) -> OpenPause:
    """Return a pause, overridden field by field."""
    fields: dict[str, Any] = {
        "key": "PAU-0001",
        "scope_ref": RUN,
        "reason": reason,
        "health_evidence_refs": [EVIDENCE],
        "resume_predicate": {
            "description": "a heartbeat at or after the anchor time",
            "evaluator": "daemon health observer",
            "last_evaluated_at": AT,
            "last_result": False,
        },
        "status": status,
        "opened_at": AT,
    }
    if reason in ("permission", "user"):
        fields["waiting_on_ref"] = ACTION if reason == "permission" else QUESTION
    if status == "HELD":
        fields |= {"held_by": "OP-0002", "hold_id": "HLD-7"}
    if status == "ESCALATED":
        fields["escalation"] = {"cause": "deadline", "raised_at": AT, "raised_ref": ACTION}
    if status == "RESOLVED":
        fields["resolved_at"] = AT
    fields.update(overrides)
    return OpenPause.model_validate(fields)


def situation(p: OpenPause, *, lost: bool = False) -> PauseSituation:
    return project_pause(p, affected_run_lost=lost).situation


# ---- one case per row -----------------------------------------------------------


@pytest.mark.parametrize("reason", ["provider", "lease", "policy", "infrastructure"])
def test_plan_047_waiting_on_a_check(reason: str) -> None:
    assert situation(pause(reason)) is PauseSituation.WAITING_ON_CHECK


@pytest.mark.parametrize(("reason", "linked"), [("permission", ACTION), ("user", QUESTION)])
def test_plan_047_waiting_on_a_person_names_the_linked_record(reason: str, linked: str) -> None:
    p = project_pause(pause(reason), affected_run_lost=False)
    assert p.situation is PauseSituation.WAITING_ON_PERSON
    assert p.linked_ref == linked


@pytest.mark.parametrize("reason", ["provider", "external_truth_ambiguous"])
def test_plan_047_lost_run_unknown_outcome_offers_reconcile_and_let_go_never_retry(
    reason: str,
) -> None:
    p = project_pause(pause(reason), affected_run_lost=True)
    assert p.situation is PauseSituation.CONTROL_OUTCOME_UNKNOWN
    assert p.verbs == (PauseVerb.RECONCILE, PauseVerb.LET_GO)
    assert "retry" not in {v.value for v in p.verbs}


def test_plan_047_a_lost_run_under_another_reason_still_waits_on_a_check() -> None:
    assert situation(pause("lease"), lost=True) is PauseSituation.WAITING_ON_CHECK


def test_plan_047_held_by_another_principal_names_the_holder_and_hold() -> None:
    p = project_pause(pause(status="HELD"), affected_run_lost=False)
    assert p.situation is PauseSituation.HELD
    assert (p.held_by, p.hold_id) == ("OP-0002", "HLD-7")
    assert p.verbs == ()


def test_plan_047_escalated_names_its_cause_and_the_record_it_raised() -> None:
    p = project_pause(pause(status="ESCALATED"), affected_run_lost=True)
    assert p.situation is PauseSituation.ESCALATED
    assert (p.escalation_cause, p.linked_ref) == (EscalationCause.DEADLINE, ACTION)


def test_plan_047_escalated_to_a_successor_pause() -> None:
    raised = {"cause": "ambiguity", "raised_at": AT, "raised_ref": "PAU-0002"}
    p = project_pause(pause(status="ESCALATED", escalation=raised), affected_run_lost=False)
    assert p.linked_ref == "PAU-0002"


def test_plan_047_resolved() -> None:
    assert situation(pause(status="RESOLVED")) is PauseSituation.RESOLVED


def test_plan_047_cancelled() -> None:
    assert situation(pause(status="CANCELLED")) is PauseSituation.CANCELLED


def test_plan_047_the_seven_situations_are_distinct_and_each_names_an_ender() -> None:
    assert len(PauseSituation) == 7
    assert set(ENDS_WHEN) == set(PauseSituation)
    assert all(ENDS_WHEN.values())


def test_plan_047_held_and_escalated_never_project_as_one_another() -> None:
    assert situation(pause(status="HELD"), lost=True) is PauseSituation.HELD
    assert situation(pause(status="ESCALATED"), lost=True) is PauseSituation.ESCALATED


def test_plan_047_a_policy_pause_is_never_held() -> None:
    assert situation(pause("policy")) is PauseSituation.WAITING_ON_CHECK


# ---- schema refusals before the projection runs --------------------------------


@pytest.mark.parametrize(
    ("reason", "status", "overrides"),
    [
        ("permission", "OPEN", {"waiting_on_ref": None}),
        ("user", "OPEN", {"waiting_on_ref": None}),
        ("provider", "OPEN", {"waiting_on_ref": ACTION}),
        ("provider", "HELD", {"held_by": None}),
        ("provider", "HELD", {"hold_id": None}),
        ("provider", "OPEN", {"held_by": "OP-0002", "hold_id": "HLD-7"}),
        ("provider", "ESCALATED", {"escalation": None}),
        (
            "provider",
            "OPEN",
            {"escalation": {"cause": "budget", "raised_at": AT, "raised_ref": ACTION}},
        ),
        ("provider", "RESOLVED", {"resolved_at": None}),
        ("provider", "OPEN", {"health_evidence_refs": []}),
        ("budget", "OPEN", {}),
        ("provider", "EXPIRED", {}),
        ("provider", "OPEN", {"key": "PAU-1"}),
        ("provider", "OPEN", {"scope_ref": EVIDENCE}),
        (
            "provider",
            "OPEN",
            {"resume_predicate": {"description": "x", "evaluator": "y", "last_result": True}},
        ),
    ],
)
def test_plan_047_a_pause_missing_what_its_render_reads_fails_validation(
    reason: str, status: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        pause(reason, status, **overrides)


def test_plan_047_the_projection_is_not_stored_on_the_record() -> None:
    with pytest.raises(ValidationError):
        pause(situation="waiting_on_check")


def test_plan_047_d_pause_a_person_wait_may_cite_the_question_it_observed() -> None:
    """A user pause the daemon opened on a host's question cites that question as its fact."""
    waiting = pause("user", waiting_on_ref=QUESTION, health_evidence_refs=(QUESTION,))
    assert situation(waiting) is PauseSituation.WAITING_ON_PERSON


def test_plan_047_d_pause_only_a_person_wait_cites_anything_but_evidence() -> None:
    with pytest.raises(ValidationError, match="only a person-wait pause cites"):
        pause("provider", health_evidence_refs=(QUESTION,))
