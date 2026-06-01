"""Unit tests for the clarify->needs_user bridge (P29-I01-W17).

Covers the bridge that lands a research-campaign clarify proposal in the
durable ``needs_user`` pause store carrying its urgency, plus the additive
urgency wiring on :mod:`eawf.workflow.skills.needs_user` that the bridge
threads through:

- a clarify proposal bridges into a pause that carries its urgency;
- the no-proposal / no-urgency path defaults a recorded pause to ``NORMAL``;
- every :class:`~eawf.kernel.state.enums.Urgency` ladder value propagates
  through the pause round trip;
- the proposal model rejects out-of-shape input (forbid-extra, bad option
  count, missing question).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import Urgency
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.clarify import ClarifyProposal, bridge_clarify_to_pause
from eawf.workflow.skills.needs_user import list_open_pauses, record_pause

_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-test"
_QUESTION = UserQuestion(
    question="Which corpus should the campaign sweep first?",
    options=[
        UserQuestionOption(label="primary", description="the canonical corpus"),
        UserQuestionOption(label="mirror"),
    ],
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.touch()
    return path


def test_bridge_clarify_lands_pause_carrying_urgency(state_path: Path) -> None:
    """A clarify proposal bridges into an open pause carrying its urgency."""
    proposal = ClarifyProposal(question=_QUESTION, urgency=Urgency.HIGH)
    pause_urn = bridge_clarify_to_pause(state_path, proposal, scope_id=_SCOPE, session=_SESSION)
    pauses = list_open_pauses(state_path, scope_id=_SCOPE)
    assert len(pauses) == 1
    pause = pauses[0]
    assert pause.pause_urn == pause_urn
    assert pause.scope_id == _SCOPE
    assert pause.session == _SESSION
    assert pause.question.question == _QUESTION.question
    # The disconnect is resolved: the proposal's urgency rides the pause.
    assert pause.urgency is Urgency.HIGH


def test_bridge_clarify_publishes_through_record_pause(state_path: Path) -> None:
    """The optional bus publisher is forwarded to the pause append."""
    published: list[object] = []
    proposal = ClarifyProposal(question=_QUESTION, urgency=Urgency.URGENT)
    bridge_clarify_to_pause(
        state_path,
        proposal,
        scope_id=_SCOPE,
        session=_SESSION,
        publish=published.append,
    )
    assert len(published) == 1


@pytest.mark.parametrize("urgency", list(Urgency))
def test_bridge_clarify_propagates_every_urgency_rung(state_path: Path, urgency: Urgency) -> None:
    """Every urgency ladder value propagates through the bridged pause."""
    proposal = ClarifyProposal(question=_QUESTION, urgency=urgency)
    bridge_clarify_to_pause(state_path, proposal, scope_id=_SCOPE, session=_SESSION)
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is urgency


def test_clarify_proposal_defaults_urgency_to_normal() -> None:
    """An unranked clarify proposal defaults to NORMAL."""
    proposal = ClarifyProposal(question=_QUESTION)
    assert proposal.urgency is Urgency.NORMAL


def test_bridge_clarify_default_proposal_lands_normal_pause(state_path: Path) -> None:
    """A default (unranked) proposal lands a NORMAL pause."""
    bridge_clarify_to_pause(
        state_path, ClarifyProposal(question=_QUESTION), scope_id=_SCOPE, session=_SESSION
    )
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is Urgency.NORMAL


def test_record_pause_without_urgency_lands_normal(state_path: Path) -> None:
    """The no-urgency path: a pause recorded without urgency defaults to NORMAL.

    This is the additive contract the bridge relies on — every pre-bridge
    caller of ``record_pause`` (skill degrade paths, daemon ``needs_user.raise``)
    keeps recording NORMAL pauses without passing the new field.
    """
    record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is Urgency.NORMAL


def test_record_pause_threads_explicit_urgency(state_path: Path) -> None:
    """``record_pause`` persists an explicitly passed urgency."""
    record_pause(
        state_path,
        scope_id=_SCOPE,
        session=_SESSION,
        question=_QUESTION,
        urgency=Urgency.LOW,
    )
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is Urgency.LOW


def test_clarify_proposal_forbids_extra_keys() -> None:
    """The proposal model rejects unknown keys (strict-validation rule)."""
    with pytest.raises(ValidationError):
        ClarifyProposal.model_validate(
            {"question": _QUESTION.model_dump(), "urgency": "high", "bogus": 1}
        )


def test_clarify_proposal_rejects_bad_option_count() -> None:
    """The wrapped question still enforces its 2-4-option bound."""
    with pytest.raises(ValidationError):
        ClarifyProposal.model_validate(
            {"question": {"question": "pick", "options": [{"label": "only"}]}}
        )


def test_clarify_proposal_rejects_missing_question() -> None:
    """A proposal without a question fails validation."""
    with pytest.raises(ValidationError):
        ClarifyProposal.model_validate({"urgency": "normal"})


def test_clarify_proposal_rejects_out_of_ladder_urgency() -> None:
    """An urgency token outside the closed ladder fails validation."""
    with pytest.raises(ValidationError):
        ClarifyProposal.model_validate({"question": _QUESTION.model_dump(), "urgency": "yesterday"})
