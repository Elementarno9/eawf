"""PLAN-015: decisions keep alternatives, evidence, resolved questions and their chain.

The ratify, supersede and obsolete paths, and the refusals: a missing option,
missing evidence, a supersession that does not cite what it replaces, and a
chain that cycles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.decision import (
    Decision,
    DecisionError,
    DecisionStatus,
    obsolete,
    ratify,
    supersede,
    validate_decision_chain,
)

pytestmark = pytest.mark.unit

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
SCOPE: Final = f"{CONTAINER}/milestone/MLS-0030"
OTHER_SCOPE: Final = f"{CONTAINER}/milestone/MLS-0031"
EVIDENCE: Final = f"{CONTAINER}/evidence/EVD-0001"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
OPTIONS: Final = [
    {"key": "per_phase", "label": "Release every phase"},
    {"key": "per_train", "label": "Release every train"},
]


def decision(key: str = "D10", **overrides: Any) -> Decision:
    """Return a proposed decision, overridden field by field."""
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": SCOPE,
        "title": "Release every phase",
        "decision": "Each closed phase ships as at least a minor release.",
        "rationale": "A phase is the unit an operator can accept or roll back.",
        "alternatives": OPTIONS,
        "chosen_option_key": "per_phase",
        "consequences": ["Every phase close bumps the version"],
        "evidence_refs": [EVIDENCE],
        "hypothesis_refs": ["H01-01"],
        "resolved_question_refs": [QUESTION],
        "effective_policy_revision": 3,
        "created_at": AT,
    }
    fields.update(overrides)
    return Decision.model_validate(fields)


def active(key: str = "D10", **overrides: Any) -> Decision:
    return ratify(decision(key, **overrides), by="OP-0001", at=AT)


# ---- the three paths -----------------------------------------------------------


def test_plan_015_ratify_keeps_alternatives_evidence_and_resolved_questions() -> None:
    d = active()
    assert d.status is DecisionStatus.ACTIVE
    assert (d.ratified_by, d.ratified_at) == ("OP-0001", AT)
    assert [o.key for o in d.alternatives] == ["per_phase", "per_train"]
    assert len(d.evidence_refs) == len(d.resolved_question_refs) == 1


def test_plan_015_supersede_links_both_ends_and_leaves_the_new_row_untouched() -> None:
    old = active("D10")
    new = active("D11", supersedes="D10", chosen_option_key="per_train")
    retired = supersede(old, new)
    assert retired.status is DecisionStatus.SUPERSEDED
    assert retired.superseded_by == "D11"
    assert retired.evidence_refs == old.evidence_refs
    validate_decision_chain([retired, new])


@pytest.mark.parametrize("make", [decision, active])
def test_plan_015_obsolete_from_proposed_or_active(make: Any) -> None:
    assert obsolete(make()).status is DecisionStatus.OBSOLETE


def test_plan_015_there_is_no_reversed_state() -> None:
    assert {s.value for s in DecisionStatus} == {"PROPOSED", "ACTIVE", "SUPERSEDED", "OBSOLETE"}
    with pytest.raises(ValidationError):
        decision(status="REVERSED")


# ---- refusals ------------------------------------------------------------------


def test_plan_015_a_single_option_without_a_reason_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        decision(alternatives=OPTIONS[:1])


def test_plan_015_a_single_option_with_its_reason_is_admitted() -> None:
    d = decision(alternatives=OPTIONS[:1], no_alternative_reason="The law leaves no other route.")
    assert len(d.alternatives) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"alternatives": []},
        {"alternatives": [OPTIONS[0], OPTIONS[0]]},
        {"chosen_option_key": "per_week"},
        {"evidence_refs": []},
        {"evidence_refs": [QUESTION]},
        {"supersedes": "D10"},
        {"status": "SUPERSEDED", "ratified_by": "OP-0001", "ratified_at": AT},
        {"status": "ACTIVE"},
        {"ratified_by": "OP-0001", "ratified_at": AT},
        {"key": "DEC-10"},
        {"effective_policy_revision": 0},
    ],
)
def test_plan_015_an_incomplete_decision_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        decision(**overrides)


def test_plan_015_activation_needs_consequences() -> None:
    with pytest.raises(ValidationError, match="consequences"):
        ratify(decision(consequences=[]), by="OP-0001", at=AT)


def test_plan_015_ratifying_twice_is_illegal() -> None:
    with pytest.raises(DecisionError) as caught:
        ratify(active(), by="OP-0001", at=AT)
    assert caught.value.code == "decision_transition_illegal"


def test_plan_015_a_proposed_decision_cannot_be_superseded() -> None:
    with pytest.raises(DecisionError) as caught:
        supersede(decision("D10"), active("D11", supersedes="D10"))
    assert caught.value.code == "decision_transition_illegal"


@pytest.mark.parametrize(
    "new",
    [
        lambda: decision("D11", supersedes="D10"),
        lambda: active("D11"),
        lambda: active("D11", supersedes="D10", scope_ref=OTHER_SCOPE),
    ],
)
def test_plan_015_a_superseder_must_be_active_cite_the_old_and_share_its_scope(
    new: Any,
) -> None:
    with pytest.raises(DecisionError) as caught:
        supersede(active("D10"), new())
    assert caught.value.code == "decision_supersession_invalid"


def test_plan_015_a_terminal_decision_cannot_be_obsoleted() -> None:
    with pytest.raises(DecisionError):
        obsolete(obsolete(decision()))


def test_plan_015_a_supersession_cycle_is_rejected() -> None:
    a = active("D10", supersedes="D11").model_copy(
        update={"status": DecisionStatus.SUPERSEDED, "superseded_by": "D11"}
    )
    b = active("D11", supersedes="D10").model_copy(
        update={"status": DecisionStatus.SUPERSEDED, "superseded_by": "D10"}
    )
    with pytest.raises(DecisionError, match="cycles"):
        validate_decision_chain([a, b])


def test_plan_015_a_dangling_or_uncited_link_is_rejected() -> None:
    old = supersede(active("D10"), active("D11", supersedes="D10"))
    with pytest.raises(DecisionError, match="does not cite"):
        validate_decision_chain([old])
    with pytest.raises(DecisionError, match="unknown"):
        validate_decision_chain([active("D11", supersedes="D10")])


def test_plan_015_an_empty_set_is_a_valid_chain() -> None:
    validate_decision_chain([])
