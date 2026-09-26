"""The research board surfaces auto-resolved questions + drop reasons.

Two independent defects, both against the question lifecycle
surfaces:

* :func:`~eawf.surfaces.tui.modes.research_board.compute_round_progress`
  already derived ``auto_resolved_count``, but none of the four render sites
  that print the ``open / answered / pruned`` triad (:func:`build_tree_nodes`'s
  scope-questions node, :func:`render_progress`'s BUDGET band, and
  :func:`render_campaign_stats`'s QUESTIONS + BUDGET bands) ever displayed it --
  a run that auto-paired its one question read as ``0 open / 0 answered /
  0 pruned``, silently dropping the only signal that anything happened.
* :func:`~eawf.runtime.daemon.methods.research._apply_resolve_question`
  dropped a question without ever writing :attr:`OpenQuestion.drop_reason`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import ClaimStatus, OpenQuestionDropReason, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion, State
from eawf.runtime.daemon.methods.research import ResolveQuestionParams, _apply_resolve_question
from eawf.surfaces.tui.modes.research_board import (
    CampaignRow,
    build_tree_nodes,
    render_campaign_stats,
    render_progress,
)

_T0 = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


def _campaign_row(campaign_id: str = "RC-0001") -> CampaignRow:
    return CampaignRow(
        campaign_id=campaign_id,
        topic="Survey the options-pricing landscape",
        domains=("market-structure",),
        default_depth="medium",
    )


def _auto_resolved_question(question_id: str = "OQ-0001") -> OpenQuestion:
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title="which venue dominates",
        status=OpenQuestionStatus.AUTO_RESOLVED,
        answered_by_claim_id="CL-0001",
        created_at=_T0,
        resolved_at=_T0,
    )


def _answering_claim(claim_id: str = "CL-0001") -> Claim:
    return Claim(
        id=claim_id,
        scope_id="QR",
        title="dark pools dominate block volume",
        status=ClaimStatus.SUPPORTED,
        answers_question_id="OQ-0001",
        created_at=_T0,
    )


# --------------------------------------------------------------------------
# The four render sites surface auto_resolved_count
# --------------------------------------------------------------------------


def test_build_tree_nodes_scope_questions_detail_shows_auto_resolved_count() -> None:
    """A run that only auto-paired its one question must not read as 0/0/0."""
    questions = (_auto_resolved_question(),)
    claims = (_answering_claim(),)

    nodes = build_tree_nodes((), questions, claims=claims)

    scope_node = next(n for n in nodes if n.label.startswith("scope questions"))
    assert scope_node.detail == "0 open / 0 answered / 1 auto-resolved / 0 pruned"


def test_build_tree_nodes_scope_questions_detail_zero_auto_resolved() -> None:
    """The added field renders 0 (not blank) when nothing auto-paired."""
    nodes = build_tree_nodes((), (), claims=())

    assert nodes == ()  # empty ledger renders no scope-questions node at all


def test_render_progress_budget_band_shows_auto_resolved_count() -> None:
    questions = (_auto_resolved_question(),)
    claims = (_answering_claim(),)

    rendered = render_progress((), claims, questions, checkpoints=0)

    budget_line = next(line for line in rendered.splitlines() if "BUDGET" in line)
    assert "1 auto-resolved" in budget_line


def test_render_campaign_stats_shows_auto_resolved_count() -> None:
    campaign = _campaign_row()
    questions = (_auto_resolved_question(),)
    claims = (_answering_claim(),)

    rendered = render_campaign_stats(campaign, (campaign,), claims, questions, (), checkpoints=0)

    questions_line = next(line for line in rendered.splitlines() if "QUESTIONS" in line)
    budget_line = next(line for line in rendered.splitlines() if "BUDGET" in line)
    assert "1 auto-resolved" in questions_line
    assert "1 auto-resolved" in budget_line


# --------------------------------------------------------------------------
# The resolve path writes a drop reason
# --------------------------------------------------------------------------


def _question(question_id: str = "OQ-1") -> OpenQuestion:
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title="which venue dominates",
        status=OpenQuestionStatus.OPEN,
        created_at=_T0,
    )


def test_apply_resolve_question_drop_writes_a_reason() -> None:
    """Dropping a question through the canonical writer never leaves the reason blank."""
    question = _question()
    state = State.model_construct(claims={}, open_questions={question.id: question}, project=None)

    _apply_resolve_question(state, ResolveQuestionParams(question_id=question.id, drop=True))

    assert state.open_questions is not None
    dropped = state.open_questions[question.id]
    assert dropped.status is OpenQuestionStatus.DROPPED
    assert dropped.drop_reason is OpenQuestionDropReason.OUT_OF_SCOPE


def test_apply_resolve_question_answer_leaves_no_drop_reason() -> None:
    """An ANSWERED resolve carries no drop reason -- the model forbids one."""
    question = _question()
    state = State.model_construct(claims={}, open_questions={question.id: question}, project=None)

    _apply_resolve_question(state, ResolveQuestionParams(question_id=question.id, drop=False))

    assert state.open_questions is not None
    answered = state.open_questions[question.id]
    assert answered.status is OpenQuestionStatus.ANSWERED
    assert answered.drop_reason is None
