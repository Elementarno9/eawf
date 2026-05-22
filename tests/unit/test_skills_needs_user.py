"""Unit tests for the :mod:`eawf.skills.needs_user` pause store (P26-I02-W07).

Covers record/list/resolve over the event-store-backed pause records:
the round trip, scope filtering, the open-vs-resolved pairing, label
validation, and the error paths (unknown urn, already resolved, invalid
choice).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.skills.needs_user import (
    OpenPause,
    PauseError,
    build_pause_urn,
    find_open_pause,
    list_open_pauses,
    record_pause,
    resolve_pause,
    validate_choice,
)

_SCOPE = "urn:eawf:v1:state:QR"
_OTHER_SCOPE = "urn:eawf:v1:state:ZZ"
_SESSION = "urn:eawf:v1:session:cli/SES-test"
_QUESTION = UserQuestion(
    question="Apply the proposed roadmap?",
    options=[
        UserQuestionOption(label="apply", description="apply as-is"),
        UserQuestionOption(label="revise"),
        UserQuestionOption(label="cancel"),
    ],
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.touch()
    return path


def test_build_pause_urn_is_event_kind() -> None:
    urn = build_pause_urn(_SCOPE)
    assert urn.startswith("urn:eawf:v1:event:")
    assert "needs-user-" in urn


def test_list_open_pauses_empty_when_no_store(state_path: Path) -> None:
    assert list_open_pauses(state_path) == []


def test_record_then_list_round_trips_question(state_path: Path) -> None:
    pause_urn = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    pauses = list_open_pauses(state_path, scope_id=_SCOPE)
    assert len(pauses) == 1
    pause = pauses[0]
    assert isinstance(pause, OpenPause)
    assert pause.pause_urn == pause_urn
    assert pause.scope_id == _SCOPE
    assert pause.session == _SESSION
    assert pause.question.question == "Apply the proposed roadmap?"
    assert [o.label for o in pause.question.options] == ["apply", "revise", "cancel"]


def test_list_open_pauses_filters_by_scope(state_path: Path) -> None:
    record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    record_pause(state_path, scope_id=_OTHER_SCOPE, session=_SESSION, question=_QUESTION)
    assert len(list_open_pauses(state_path, scope_id=_SCOPE)) == 1
    assert len(list_open_pauses(state_path, scope_id=_OTHER_SCOPE)) == 1
    assert len(list_open_pauses(state_path)) == 2


def test_resolve_pause_marks_pause_resolved(state_path: Path) -> None:
    pause_urn = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    resolved = resolve_pause(state_path, pause_urn=pause_urn, choice="revise")
    assert resolved.pause_urn == pause_urn
    assert list_open_pauses(state_path, scope_id=_SCOPE) == []


def test_resolve_pause_oldest_first_ordering(state_path: Path) -> None:
    first = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    second = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    pauses = list_open_pauses(state_path, scope_id=_SCOPE)
    assert [p.pause_urn for p in pauses] == [first, second]


def test_find_open_pause_unknown_urn_raises(state_path: Path) -> None:
    record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    with pytest.raises(PauseError, match="unknown or already-resolved pause"):
        find_open_pause(state_path, "urn:eawf:v1:event:QR/needs-user-deadbeef")


def test_resolve_pause_already_resolved_raises(state_path: Path) -> None:
    pause_urn = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    resolve_pause(state_path, pause_urn=pause_urn, choice="apply")
    with pytest.raises(PauseError, match="unknown or already-resolved pause"):
        resolve_pause(state_path, pause_urn=pause_urn, choice="revise")


def test_resolve_pause_invalid_choice_raises(state_path: Path) -> None:
    pause_urn = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    with pytest.raises(PauseError, match="invalid choice"):
        resolve_pause(state_path, pause_urn=pause_urn, choice="nope")
    # The pause is still open after the rejected choice.
    assert len(list_open_pauses(state_path, scope_id=_SCOPE)) == 1


def test_validate_choice_accepts_known_label() -> None:
    pause = OpenPause(pause_urn="x", scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    assert validate_choice(pause, "apply") == "apply"


def test_validate_choice_rejects_unknown_label() -> None:
    pause = OpenPause(pause_urn="x", scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    with pytest.raises(PauseError, match="invalid choice 'nope'"):
        validate_choice(pause, "nope")
