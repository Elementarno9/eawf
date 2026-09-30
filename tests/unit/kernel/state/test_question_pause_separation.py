"""Question status and supersession stay distinct from ANSWERED.

Covers two separate defects:

* :class:`~eawf.kernel.state.enums.OpenQuestionStatus` gains ``AUTO_RESOLVED``
  and ``SEALED`` members distinct from ``ANSWERED``, and a round reconcile that
  pairs a claim by elimination lands ``AUTO_RESOLVED``, never ``ANSWERED``.
* :class:`~eawf.kernel.state.models.OpenQuestion` carries an acyclic
  ``superseded_by_question_ref`` alongside a paired ``drop_reason``; a State
  holding a supersession cycle across two questions fails validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.state.enums import OpenQuestionDropReason, OpenQuestionStatus
from eawf.kernel.state.models import OpenQuestion, State
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.runtime.daemon.methods.research import reconcile_round_claims

pytestmark = pytest.mark.unit

_SCOPE = "QR"
_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _question(qid: str, **overrides: Any) -> OpenQuestion:
    fields: dict[str, Any] = {
        "id": qid,
        "scope_id": _SCOPE,
        "title": f"Question {qid}",
        "status": OpenQuestionStatus.OPEN,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return OpenQuestion(**fields)


def _state_document(**overrides: Any) -> dict[str, Any]:
    """Build a minimal State document with no phases, iters or waves."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": f"urn:eawf:v1:state:{_SCOPE}",
        "updated_at": _NOW.isoformat(),
        "project": {
            "code": _SCOPE,
            "slug": "qr",
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
    }
    document.update(overrides)
    return document


def _state_with_questions(*questions: OpenQuestion) -> dict[str, Any]:
    """Build a State document whose open_questions holds *questions*."""
    return _state_document(open_questions={q.id: q.model_dump(mode="json") for q in questions})


# --- Round reconcile marks a policy pairing auto-resolved (CR-01) ----------


def _round_findings(*lines: str) -> RoundFindings:
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


def test_round_reconcile_auto_resolves_the_sole_candidate_not_answers_it() -> None:
    # The producer: reconcile_round_claims pairs a claim to the scope's sole
    # OPEN non-blocking question by elimination -- a policy inference, never
    # an operator answering it, so the real writer must land AUTO_RESOLVED.
    question = _question("QST-1")
    state = State.model_validate(_state_with_questions(question))

    written = reconcile_round_claims(state, _round_findings("claim one"), scope_id=None, now=_NOW)

    assert state.open_questions is not None
    resolved = state.open_questions["QST-1"]
    assert resolved.status is OpenQuestionStatus.AUTO_RESOLVED
    assert resolved.answered_by_claim_id == written[0]


# --- OpenQuestion drop_reason / supersession invariants ---------------------


def test_plain_dropped_question_may_omit_a_drop_reason() -> None:
    # A moot / out-of-scope drop is not required to state why (only the
    # superseded pairing below is a hard invariant).
    dropped = _question("QST-1", status=OpenQuestionStatus.DROPPED)
    assert dropped.drop_reason is None


def test_drop_reason_forbidden_outside_dropped() -> None:
    with pytest.raises(ValidationError, match="forbidden outside DROPPED"):
        _question("QST-1", status=OpenQuestionStatus.OPEN, drop_reason=OpenQuestionDropReason.MOOT)


def test_superseded_drop_reason_requires_the_reference() -> None:
    with pytest.raises(ValidationError, match="required exactly when"):
        _question(
            "QST-1",
            status=OpenQuestionStatus.DROPPED,
            drop_reason=OpenQuestionDropReason.SUPERSEDED,
        )


def test_reference_forbidden_without_superseded_drop_reason() -> None:
    with pytest.raises(ValidationError, match="required exactly when"):
        _question(
            "QST-1",
            status=OpenQuestionStatus.DROPPED,
            drop_reason=OpenQuestionDropReason.MOOT,
            superseded_by_question_ref="QST-2",
        )


def test_question_cannot_supersede_itself() -> None:
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _question(
            "QST-1",
            status=OpenQuestionStatus.DROPPED,
            drop_reason=OpenQuestionDropReason.SUPERSEDED,
            superseded_by_question_ref="QST-1",
        )


def test_a_resolving_supersession_chain_validates() -> None:
    q1 = _question(
        "QST-1",
        status=OpenQuestionStatus.DROPPED,
        drop_reason=OpenQuestionDropReason.SUPERSEDED,
        superseded_by_question_ref="QST-2",
    )
    q2 = _question("QST-2", status=OpenQuestionStatus.OPEN)
    state = State.model_validate(_state_with_questions(q1, q2))
    assert state.open_questions is not None
    assert state.open_questions["QST-1"].superseded_by_question_ref == "QST-2"


def test_state_rejects_a_supersession_cycle() -> None:
    q1 = _question(
        "QST-1",
        status=OpenQuestionStatus.DROPPED,
        drop_reason=OpenQuestionDropReason.SUPERSEDED,
        superseded_by_question_ref="QST-2",
    )
    q2 = _question(
        "QST-2",
        status=OpenQuestionStatus.DROPPED,
        drop_reason=OpenQuestionDropReason.SUPERSEDED,
        superseded_by_question_ref="QST-1",
    )
    with pytest.raises(ValidationError, match="supersession chain cycles"):
        State.model_validate(_state_with_questions(q1, q2))
