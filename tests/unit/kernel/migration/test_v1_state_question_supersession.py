"""The v1 ``State`` accepts an acyclic question supersession chain and refuses a cycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import OpenQuestionDropReason, OpenQuestionStatus
from eawf.kernel.state.models import OpenQuestion

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
