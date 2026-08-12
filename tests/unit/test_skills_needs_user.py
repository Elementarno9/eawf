"""Unit tests for the :mod:`eawf.workflow.skills.needs_user` pause store.

Covers record/list/resolve over the event-store-backed pause records:
the round trip, scope filtering, the open-vs-resolved pairing, label
validation, and the error paths (unknown urn, already resolved, invalid
choice).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind, Urgency
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import (
    PAUSE_EVENT_TYPE,
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


def test_open_pause_defaults_urgency_to_normal() -> None:
    pause = OpenPause(pause_urn="x", scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    assert pause.urgency is Urgency.NORMAL


def test_record_pause_round_trips_urgency(state_path: Path) -> None:
    record_pause(
        state_path,
        scope_id=_SCOPE,
        session=_SESSION,
        question=_QUESTION,
        urgency=Urgency.URGENT,
    )
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is Urgency.URGENT


def test_record_pause_defaults_urgency_to_normal(state_path: Path) -> None:
    record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
    pause = list_open_pauses(state_path, scope_id=_SCOPE)[0]
    assert pause.urgency is Urgency.NORMAL


def test_list_open_pauses_legacy_row_without_urgency_decodes_normal(state_path: Path) -> None:
    """A pre-urgency pause row (no urgency key in extras) decodes as NORMAL."""
    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    pause_urn = "urn:eawf:v1:event:QR/needs-user-legacy"
    payload = EventPayload(
        timestamp=now,
        event_type=PAUSE_EVENT_TYPE,
        actor="skill",
        command="skill pause",
        args_hash="",
        status="needs_user",
        message=_QUESTION.question,
        extras={
            "pause_urn": pause_urn,
            "scope_id": _SCOPE,
            "session": _SESSION,
            "user_question": _QUESTION.model_dump_json(),
        },
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id="EV-legacy",
            kind=StoreKind.EVENT,
            scope_id=_SCOPE,
            created_at=now,
            updated_at=None,
            summary="legacy needs_user pause",
            payload=payload.model_dump(mode="json"),
        ),
    )
    pause = find_open_pause(state_path, pause_urn)
    assert pause.urgency is Urgency.NORMAL


def test_list_open_pauses_out_of_ladder_urgency_decodes_normal(state_path: Path) -> None:
    """A pause row with a corrupt urgency token decodes as NORMAL, not a crash."""
    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    pause_urn = "urn:eawf:v1:event:QR/needs-user-corrupt"
    payload = EventPayload(
        timestamp=now,
        event_type=PAUSE_EVENT_TYPE,
        actor="skill",
        command="skill pause",
        args_hash="",
        status="needs_user",
        message=_QUESTION.question,
        extras={
            "pause_urn": pause_urn,
            "scope_id": _SCOPE,
            "session": _SESSION,
            "user_question": _QUESTION.model_dump_json(),
            "urgency": "yesterday",
        },
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id="EV-corrupt",
            kind=StoreKind.EVENT,
            scope_id=_SCOPE,
            created_at=now,
            updated_at=None,
            summary="corrupt-urgency needs_user pause",
            payload=payload.model_dump(mode="json"),
        ),
    )
    pause = find_open_pause(state_path, pause_urn)
    assert pause.urgency is Urgency.NORMAL
