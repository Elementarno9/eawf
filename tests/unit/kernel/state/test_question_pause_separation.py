"""Question status and supersession stay distinct from ANSWERED.

:class:`~eawf.kernel.state.models.OpenQuestion` carries an acyclic
``superseded_by_question_ref`` alongside a paired ``drop_reason``: a dropped
question may omit the reason, a reason belongs only to a dropped question,
and a superseded drop names the question it was superseded by.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

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


# --- Round reconcile marks a policy pairing auto-resolved (CR-01) ----------


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
