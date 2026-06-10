"""Tests for the clarify-run -> needs_user ledger bridge (P30-I10-W12).

Covers :func:`eawf.workflow.skills.clarify.bridge_clarify_run_to_ledger`, the
batch bridge that seeds the durable needs_user / OpenQuestion ledger with one
resolvable pause per clarify-run question instead of discarding them:

- a run with N questions seeds exactly N ledger rows, each resolvable through
  the ordinary :func:`~eawf.workflow.skills.needs_user.resolve_pause` path;
- each seeded row carries its own proposal urgency;
- the bus publisher fires once per seeded row;
- the empty-run boundary is a no-op (zero rows seeded);
- an invalid proposal (out-of-shape question) fails at model construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import Urgency
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.clarify import (
    ClarifyProposal,
    bridge_clarify_run_to_ledger,
)
from eawf.workflow.skills.needs_user import list_open_pauses, resolve_pause

_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-test"


def _question(prompt: str) -> UserQuestion:
    return UserQuestion(
        question=prompt,
        options=[
            UserQuestionOption(label="primary", description="the canonical pick"),
            UserQuestionOption(label="mirror"),
        ],
    )


def _proposals(count: int, *, urgency: Urgency = Urgency.NORMAL) -> list[ClarifyProposal]:
    return [
        ClarifyProposal(question=_question(f"clarify-{i}?"), urgency=urgency) for i in range(count)
    ]


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.touch()
    return path


def test_bridge_clarify_run_seeds_one_row_per_question(state_path: Path) -> None:
    """A clarify run with N questions seeds exactly N ledger rows."""
    proposals = _proposals(3)
    pause_urns = bridge_clarify_run_to_ledger(
        state_path, proposals, scope_id=_SCOPE, session=_SESSION
    )
    assert len(pause_urns) == 3
    assert len(set(pause_urns)) == 3
    open_pauses = list_open_pauses(state_path, scope_id=_SCOPE)
    assert len(open_pauses) == 3
    seeded_prompts = {p.question.question for p in open_pauses}
    assert seeded_prompts == {p.question.question for p in proposals}
    assert {p.pause_urn for p in open_pauses} == set(pause_urns)


def test_bridge_clarify_run_rows_are_each_resolvable(state_path: Path) -> None:
    """Every seeded row resolves through the ordinary resume path."""
    pause_urns = bridge_clarify_run_to_ledger(
        state_path, _proposals(2), scope_id=_SCOPE, session=_SESSION
    )
    for pause_urn in pause_urns:
        resolved = resolve_pause(state_path, pause_urn=pause_urn, choice="primary")
        assert resolved.pause_urn == pause_urn
    # All seeded rows answered -> the ledger has no open question left.
    assert list_open_pauses(state_path, scope_id=_SCOPE) == []


def test_bridge_clarify_run_carries_per_proposal_urgency(state_path: Path) -> None:
    """Each seeded row carries the urgency of its originating proposal."""
    proposals = [
        ClarifyProposal(question=_question("low?"), urgency=Urgency.LOW),
        ClarifyProposal(question=_question("urgent?"), urgency=Urgency.URGENT),
    ]
    bridge_clarify_run_to_ledger(state_path, proposals, scope_id=_SCOPE, session=_SESSION)
    by_prompt = {p.question.question: p.urgency for p in list_open_pauses(state_path)}
    assert by_prompt["low?"] is Urgency.LOW
    assert by_prompt["urgent?"] is Urgency.URGENT


def test_bridge_clarify_run_publishes_once_per_row(state_path: Path) -> None:
    """The bus publisher fires once for each seeded ledger row."""
    published: list[object] = []
    bridge_clarify_run_to_ledger(
        state_path,
        _proposals(4),
        scope_id=_SCOPE,
        session=_SESSION,
        publish=published.append,
    )
    assert len(published) == 4


def test_bridge_clarify_run_empty_is_noop(state_path: Path) -> None:
    """The empty-run boundary seeds zero rows and returns an empty list."""
    pause_urns = bridge_clarify_run_to_ledger(state_path, [], scope_id=_SCOPE, session=_SESSION)
    assert pause_urns == []
    assert list_open_pauses(state_path, scope_id=_SCOPE) == []


def test_bridge_clarify_run_single_question_seeds_one_row(state_path: Path) -> None:
    """The single-question boundary seeds exactly one resolvable row."""
    pause_urns = bridge_clarify_run_to_ledger(
        state_path, _proposals(1), scope_id=_SCOPE, session=_SESSION
    )
    assert len(pause_urns) == 1
    assert len(list_open_pauses(state_path, scope_id=_SCOPE)) == 1


def test_bridge_clarify_run_rejects_invalid_proposal() -> None:
    """An out-of-shape question fails at proposal construction (error path)."""
    with pytest.raises(ValidationError):
        ClarifyProposal.model_validate(
            {"question": {"question": "pick", "options": [{"label": "only"}]}}
        )
