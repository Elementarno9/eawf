"""Question status/supersession and OpenPause stay distinct from ANSWERED.

Covers three separate defects this wave closes:

* :class:`~eawf.kernel.state.enums.OpenQuestionStatus` gains ``AUTO_RESOLVED``
  and ``SEALED`` members distinct from ``ANSWERED``, so a real answered-count
  projection (:func:`~eawf.surfaces.tui.modes.research_board.compute_round_progress`)
  never folds a defaulted question into the answered tally.
* :class:`~eawf.kernel.state.models.OpenQuestion` carries an acyclic
  ``superseded_by_question_ref`` alongside a paired ``drop_reason``; a State
  holding a supersession cycle across two questions fails validation.
* ``needs_user.raise`` refuses a caller that hands in daemon-owned
  :class:`~eawf.workflow.skills.needs_user.OpenPause` bookkeeping fields, and
  the daemon's pause wire projection (:class:`~eawf.runtime.daemon.methods.needs_user.ParkedPause`)
  never cross-validates as an :class:`~eawf.kernel.state.models.OpenQuestion`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.state.enums import OpenQuestionDropReason, OpenQuestionStatus, StoreKind
from eawf.kernel.state.models import OpenQuestion, State
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.needs_user import PauseFabricationError, raise_needs_user
from eawf.runtime.daemon.methods.research import reconcile_round_claims
from eawf.surfaces.tui.modes.research_board import compute_round_progress
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption

pytestmark = pytest.mark.unit

_SCOPE = "QR"
_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
_QUESTION = UserQuestion(
    question="Apply roadmap?",
    options=[UserQuestionOption(label="apply"), UserQuestionOption(label="revise")],
)


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


# --- OpenQuestionStatus / answered-count separation (CR-01) -----------------


def test_answered_count_excludes_an_auto_resolved_question() -> None:
    defaulted = _question("QST-1", status=OpenQuestionStatus.AUTO_RESOLVED)
    progress = compute_round_progress(campaigns=(), claims=(), questions=(defaulted,))
    assert progress.answered_count == 0


def test_answered_count_excludes_a_sealed_question() -> None:
    sealed = _question("QST-1", status=OpenQuestionStatus.SEALED)
    progress = compute_round_progress(campaigns=(), claims=(), questions=(sealed,))
    assert progress.answered_count == 0


def test_answered_count_still_counts_a_genuinely_answered_question() -> None:
    answered = _question("QST-1", status=OpenQuestionStatus.ANSWERED, resolved_at=_NOW)
    progress = compute_round_progress(campaigns=(), claims=(), questions=(answered,))
    assert progress.answered_count == 1


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

    # The real answered-count projection excludes the auto-resolved pairing.
    progress = compute_round_progress(campaigns=(), claims=(), questions=(resolved,))
    assert progress.answered_count == 0
    assert progress.auto_resolved_count == 1


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


# --- OpenPause stays daemon-observed (CR-02) --------------------------------


def _state_path(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.touch()
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-09-25T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=None,
        state_path=state_path,
        event_path=store_path(state_path, StoreKind.EVENT),
    )


@pytest.mark.parametrize(
    "fabricated_field,fabricated_value",
    [
        ("pause_urn", "urn:eawf:v1:event:QR/needs-user-fake"),
        ("occurred_at", _NOW.isoformat()),
        ("wave_id", "P01-I01-W01"),
    ],
)
def test_needs_user_raise_refuses_a_caller_fabricated_pause_field(
    tmp_path: Path, fabricated_field: str, fabricated_value: str
) -> None:
    ctx = _ctx(_state_path(tmp_path))
    params = {
        "scope_id": f"urn:eawf:v1:state:{_SCOPE}",
        "session": "urn:eawf:v1:session:cli/SES-test",
        "question": _QUESTION.model_dump(mode="json"),
        fabricated_field: fabricated_value,
    }

    async def body() -> dict[str, object]:
        return await raise_needs_user(ctx, params)

    with pytest.raises(PauseFabricationError, match="daemon-owned fields"):
        asyncio.run(body())


def test_needs_user_raise_still_accepts_a_legitimate_pause(tmp_path: Path) -> None:
    ctx = _ctx(_state_path(tmp_path))
    params = {
        "scope_id": f"urn:eawf:v1:state:{_SCOPE}",
        "session": "urn:eawf:v1:session:cli/SES-test",
        "question": _QUESTION.model_dump(mode="json"),
    }

    async def body() -> dict[str, object]:
        return await raise_needs_user(ctx, params)

    result = asyncio.run(body())
    assert isinstance(result["pause_urn"], str)


def test_parked_pause_row_never_validates_as_an_open_question() -> None:
    from eawf.runtime.daemon.methods.needs_user import ParkedPause

    pause_row = ParkedPause(
        pause_urn="urn:eawf:v1:event:QR/needs-user-abc123",
        scope_id=_SCOPE,
        session="urn:eawf:v1:session:cli/SES-test",
        question=_QUESTION,
    ).model_dump(mode="json")
    with pytest.raises(ValidationError):
        OpenQuestion.model_validate(pause_row)


def test_open_question_row_never_validates_as_a_parked_pause() -> None:
    from eawf.runtime.daemon.methods.needs_user import ParkedPause

    question_row = _question("QST-1").model_dump(mode="json")
    with pytest.raises(ValidationError):
        ParkedPause.model_validate(question_row)
