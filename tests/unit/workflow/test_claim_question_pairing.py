"""Round-end reconcile pairs a claim to an open question only by elimination.

A researcher finding carries no link to the question it addresses, so
:func:`~eawf.runtime.daemon.methods.research.reconcile_round_claims` may pair
a question only when it is the scope's single OPEN, non-blocking candidate.
The pairing is a policy inference, not an operator answering the question, so
it lands the question AUTO_RESOLVED, never ANSWERED (PLAN-036 keeps the two
distinct). With two or more candidates any pairing would be a guess, so every
question must stay OPEN.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.models import OpenQuestion, State
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.runtime.daemon.methods.research import reconcile_round_claims

pytestmark = pytest.mark.unit

_SCOPE = "ABC"
_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def _question(qid: str, *, scope: str = _SCOPE, blocking: bool = False) -> OpenQuestion:
    return OpenQuestion(
        id=qid,
        scope_id=scope,
        title=f"Question {qid}",
        status=OpenQuestionStatus.OPEN,
        blocking=blocking,
        created_at=_NOW,
    )


def _state(*questions: OpenQuestion) -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": f"urn:eawf:v1:state:{_SCOPE}",
            "updated_at": _NOW.isoformat(),
            "project": {
                "code": _SCOPE,
                "slug": "abc",
                "title": _SCOPE,
                "description": None,
                "domains": ["x"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": f"urn:eawf:v1:repo:{_SCOPE}",
            },
            "current": {"project_code": _SCOPE},
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
            "open_questions": {q.id: q.model_dump(mode="json") for q in questions},
        }
    )


def _findings(*lines: str) -> RoundFindings:
    body = ResearcherReportBody.model_validate(
        {
            "role": "researcher",
            "verdict": "pass",
            "confidence": "medium",
            "summary": "surveyed d",
            "question": "what does d reveal",
            "findings": list(lines),
            "recommendation": "pursue d",
            "evidence_refs": [{"kind": "store_record", "ref": "src/d.py:1"}],
        }
    )
    return RoundFindings(round_number=1, bodies=(body,), domains=("d",))


def _status(state: State, qid: str) -> OpenQuestionStatus:
    assert state.open_questions is not None
    return state.open_questions[qid].status


def test_reconcile_single_candidate_is_auto_resolved_by_first_claim() -> None:
    """One OPEN non-blocking question is every claim's single candidate."""
    state = _state(_question("OQ-a"))

    written = reconcile_round_claims(
        state, _findings("claim one", "claim two"), scope_id=None, now=_NOW
    )

    assert state.open_questions is not None
    question = state.open_questions["OQ-a"]
    assert question.status is OpenQuestionStatus.AUTO_RESOLVED
    assert question.answered_by_claim_id == written[0]
    assert question.resolved_at == _NOW
    assert state.claims is not None
    assert state.claims[written[0]].answers_question_id == "OQ-a"
    assert state.claims[written[1]].answers_question_id is None


def test_reconcile_two_candidates_answers_neither() -> None:
    """Two candidates make any pairing a guess, so both stay OPEN and unlinked."""
    state = _state(_question("OQ-a"), _question("OQ-b"))

    written = reconcile_round_claims(
        state, _findings("claim one", "claim two"), scope_id=None, now=_NOW
    )

    assert _status(state, "OQ-a") is OpenQuestionStatus.OPEN
    assert _status(state, "OQ-b") is OpenQuestionStatus.OPEN
    assert state.claims is not None
    assert all(state.claims[cid].answers_question_id is None for cid in written)


def test_reconcile_blocking_question_is_not_a_candidate() -> None:
    """A blocking checkpoint never auto-resolves and does not count as a candidate."""
    state = _state(_question("OQ-a"), _question("OQ-gate", blocking=True))

    reconcile_round_claims(state, _findings("claim one"), scope_id=None, now=_NOW)

    assert _status(state, "OQ-a") is OpenQuestionStatus.AUTO_RESOLVED
    assert _status(state, "OQ-gate") is OpenQuestionStatus.OPEN


def test_reconcile_other_scope_question_is_not_a_candidate() -> None:
    """A question in another scope neither resolves nor blocks the pairing."""
    state = _state(_question("OQ-a"), _question("OQ-other", scope="XYZ"))

    reconcile_round_claims(state, _findings("claim one"), scope_id=None, now=_NOW)

    assert _status(state, "OQ-a") is OpenQuestionStatus.AUTO_RESOLVED
    assert _status(state, "OQ-other") is OpenQuestionStatus.OPEN


def test_reconcile_no_findings_leaves_single_candidate_open() -> None:
    """A round that writes no claim has nothing to pair, even with one candidate."""
    state = _state(_question("OQ-a"))

    written = reconcile_round_claims(state, _findings(), scope_id=None, now=_NOW)

    assert written == []
    assert _status(state, "OQ-a") is OpenQuestionStatus.OPEN


def test_reconcile_no_candidates_writes_claims_unlinked() -> None:
    """With no open question the claims land OPEN and answer nothing."""
    state = _state()

    written = reconcile_round_claims(state, _findings("claim one"), scope_id=None, now=_NOW)

    assert state.claims is not None
    assert state.claims[written[0]].answers_question_id is None
