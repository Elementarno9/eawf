"""Tests for the pure attention-feed reducer (P29-I02-W19).

The :func:`~eawf.surfaces.tui.attention.build_attention_feed` reducer folds
several state-resident attention sources plus the open ``needs_user``
pauses onto one urgency-ranked list. These tests pin the ranking and the
per-source derivation **without** a Textual mount -- the reducer is pure,
so the order, the honest-empty case, and the mixed-source fold are all
asserted against the returned tuple directly.

The base state is the active-wave fixture (one in-progress wave, no
incidents / questions); extra entities are constructed in-memory and
spliced in via ``State.model_copy`` so each case isolates one source mix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import (
    IncidentSeverity,
    IncidentStatus,
    OpenQuestionStatus,
    Urgency,
    WaveStatus,
)
from eawf.kernel.state.models import Incident, OpenQuestion, State, Wave
from eawf.surfaces.tui.attention import (
    EMPTY_FEED_TEXT,
    AttentionItem,
    AttentionKind,
    build_attention_feed,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-tui"

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _base_state() -> State:
    """Load the active-wave fixture (1 in-progress wave; no other signals)."""
    return State.model_validate_json(_PHASE_ITER_WAVE.read_text(encoding="utf-8"))


def _empty_state() -> State:
    """Return the fixture state with no waves / incidents / questions."""
    return _base_state().model_copy(update={"waves": {}, "incidents": {}, "open_questions": {}})


def _question(text: str) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
    )


def _pause(*, urgency: Urgency, question: str, pause_urn: str) -> OpenPause:
    return OpenPause(
        pause_urn=pause_urn,
        scope_id=_SCOPE,
        session=_SESSION,
        question=_question(question),
        urgency=urgency,
    )


def _wave(*, wave_id: str, status: WaveStatus, deps: list[str] | None = None) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_NOW,
    )


def _incident(
    *, severity: IncidentSeverity, status: IncidentStatus = IncidentStatus.OPEN
) -> Incident:
    return Incident(
        id=f"INC-{severity.value}",
        scope_id="QR",
        severity=severity,
        title=f"Incident {severity.value}",
        status=status,
        opened_at=_NOW,
    )


def _open_question(
    *, status: OpenQuestionStatus, urgency: Urgency = Urgency.HIGH, blocking: bool = True
) -> OpenQuestion:
    return OpenQuestion(
        id="OQ-1",
        scope_id="QR",
        title="Pick a path",
        status=status,
        blocking=blocking,
        urgency=urgency,
        created_at=_NOW,
    )


# --------------------------------------------------------------------------
# Honest-empty
# --------------------------------------------------------------------------


def test_build_attention_feed_empty_state_and_no_pauses_is_empty() -> None:
    assert build_attention_feed(_empty_state(), ()) == ()


def test_build_attention_feed_none_state_no_pauses_is_empty() -> None:
    # The cold-load / portfolio window: no state contributes no items.
    assert build_attention_feed(None, ()) == ()


def test_build_attention_feed_in_progress_wave_is_not_attention() -> None:
    # The base fixture's single wave is IN_PROGRESS -- not a needs-operator
    # signal, so a feed over just the fixture (no pauses) is honest-empty.
    assert build_attention_feed(_base_state(), ()) == ()


def test_empty_feed_text_constant_is_stable() -> None:
    assert EMPTY_FEED_TEXT == "nothing needs you"


# --------------------------------------------------------------------------
# Single-source derivation
# --------------------------------------------------------------------------


def test_build_attention_feed_pause_row_is_actionable() -> None:
    pause = _pause(
        urgency=Urgency.NORMAL, question="resume me?", pause_urn="urn:eawf:v1:event:QR/p"
    )
    feed = build_attention_feed(_empty_state(), (pause,))
    assert len(feed) == 1
    item = feed[0]
    assert item.kind is AttentionKind.NEEDS_USER
    assert item.actionable
    assert item.pause_urn == pause.pause_urn
    assert item.question is pause.question
    assert "resume me?" in item.title


def test_build_attention_feed_failed_wave_is_urgent_and_not_actionable() -> None:
    state = _empty_state().model_copy(
        update={"waves": {"P01-I01-W09": _wave(wave_id="P01-I01-W09", status=WaveStatus.FAILED)}}
    )
    feed = build_attention_feed(state, ())
    assert len(feed) == 1
    assert feed[0].kind is AttentionKind.FAILED_WAVE
    assert feed[0].urgency is Urgency.URGENT
    assert not feed[0].actionable
    assert "P01-I01-W09" in feed[0].title


def test_build_attention_feed_incident_severity_maps_to_urgency() -> None:
    cases = (
        (IncidentSeverity.CRITICAL, Urgency.URGENT),
        (IncidentSeverity.HIGH, Urgency.HIGH),
        (IncidentSeverity.MEDIUM, Urgency.NORMAL),
        (IncidentSeverity.LOW, Urgency.LOW),
    )
    for severity, expected in cases:
        state = _empty_state().model_copy(
            update={"incidents": {"INC-1": _incident(severity=severity)}}
        )
        feed = build_attention_feed(state, ())
        assert len(feed) == 1, severity
        assert feed[0].kind is AttentionKind.INCIDENT
        assert feed[0].urgency is expected


def test_build_attention_feed_skips_non_open_incident() -> None:
    state = _empty_state().model_copy(
        update={
            "incidents": {
                "INC-1": _incident(
                    severity=IncidentSeverity.CRITICAL, status=IncidentStatus.RESOLVED
                )
            }
        }
    )
    assert build_attention_feed(state, ()) == ()


def test_build_attention_feed_surfaces_only_blocked_questions() -> None:
    blocked = _open_question(status=OpenQuestionStatus.BLOCKED, urgency=Urgency.HIGH)
    state = _empty_state().model_copy(update={"open_questions": {"OQ-1": blocked}})
    feed = build_attention_feed(state, ())
    assert len(feed) == 1
    assert feed[0].kind is AttentionKind.OPEN_QUESTION
    assert feed[0].urgency is Urgency.HIGH
    # An OPEN (non-blocking-status) question is not surfaced.
    open_q = _open_question(status=OpenQuestionStatus.OPEN)
    state_open = _empty_state().model_copy(update={"open_questions": {"OQ-1": open_q}})
    assert build_attention_feed(state_open, ()) == ()


def test_build_attention_feed_ready_wave_when_deps_closed() -> None:
    waves = {
        "P01-I01-W01": _wave(wave_id="P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave(
            wave_id="P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]
        ),
    }
    state = _empty_state().model_copy(update={"waves": waves})
    feed = build_attention_feed(state, ())
    ready = [i for i in feed if i.kind is AttentionKind.READY_WAVE]
    assert len(ready) == 1
    assert ready[0].urgency is Urgency.NORMAL
    assert "P01-I01-W02" in ready[0].title


def test_build_attention_feed_pending_wave_with_unmet_dep_is_not_ready() -> None:
    waves = {
        "P01-I01-W01": _wave(wave_id="P01-I01-W01", status=WaveStatus.IN_PROGRESS),
        "P01-I01-W02": _wave(
            wave_id="P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]
        ),
    }
    state = _empty_state().model_copy(update={"waves": waves})
    assert build_attention_feed(state, ()) == ()


# --------------------------------------------------------------------------
# Mixed sources + ranking
# --------------------------------------------------------------------------


def test_build_attention_feed_ranks_mixed_sources_by_urgency() -> None:
    # A LOW pause + an URGENT failed wave + a NORMAL ready wave: the feed
    # must order URGENT (failed) -> NORMAL (ready) -> LOW (pause).
    pause = _pause(urgency=Urgency.LOW, question="low pause", pause_urn="urn:eawf:v1:event:QR/p")
    waves = {
        "P01-I01-W08": _wave(wave_id="P01-I01-W08", status=WaveStatus.FAILED),
        "P01-I01-W09": _wave(wave_id="P01-I01-W09", status=WaveStatus.PENDING),
    }
    state = _empty_state().model_copy(update={"waves": waves})
    feed = build_attention_feed(state, (pause,))
    assert [item.urgency for item in feed] == [Urgency.URGENT, Urgency.NORMAL, Urgency.LOW]
    assert [item.kind for item in feed] == [
        AttentionKind.FAILED_WAVE,
        AttentionKind.READY_WAVE,
        AttentionKind.NEEDS_USER,
    ]


def test_build_attention_feed_same_tier_orders_needs_user_before_incident() -> None:
    # A URGENT pause and a CRITICAL (=> URGENT) incident share a tier; the
    # kind tiebreak puts the actionable needs_user row first.
    pause = _pause(
        urgency=Urgency.URGENT, question="urgent pause", pause_urn="urn:eawf:v1:event:QR/p"
    )
    state = _empty_state().model_copy(
        update={"incidents": {"INC-1": _incident(severity=IncidentSeverity.CRITICAL)}}
    )
    feed = build_attention_feed(state, (pause,))
    assert [item.kind for item in feed] == [AttentionKind.NEEDS_USER, AttentionKind.INCIDENT]
    assert all(item.urgency is Urgency.URGENT for item in feed)


def test_attention_item_actionable_only_for_pause_rows() -> None:
    pause_item = AttentionItem(
        urgency=Urgency.NORMAL,
        kind=AttentionKind.NEEDS_USER,
        title="q",
        detail="d",
        pause_urn="urn:eawf:v1:event:QR/p",
        question=_question("q"),
    )
    wave_item = AttentionItem(
        urgency=Urgency.URGENT, kind=AttentionKind.FAILED_WAVE, title="w", detail="d"
    )
    assert pause_item.actionable
    assert not wave_item.actionable
