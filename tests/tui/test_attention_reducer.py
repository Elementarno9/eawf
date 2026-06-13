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
from eawf.platform.registry.models import RegistryRepoEntry
from eawf.surfaces.tui.attention import (
    EMPTY_FEED_TEXT,
    AttentionItem,
    AttentionKind,
    build_attention_feed,
    build_portfolio_attention_feed,
    format_time_ago,
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


def _wave(
    *,
    wave_id: str,
    status: WaveStatus,
    deps: list[str] | None = None,
    iter_id: str = "P01-I01",
    claimed_at: datetime | None = None,
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_NOW,
        claimed_at=claimed_at,
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


def _wave_pause(*, wave_id: str) -> OpenPause:
    """An open pause stamped with a subject wave (a daemon stale advisory)."""
    return OpenPause(
        pause_urn="urn:eawf:v1:event:QR/needs-user-stale",
        scope_id=_SCOPE,
        session=_SESSION,
        question=_question("over-budget advisory"),
        urgency=Urgency.NORMAL,
        wave_id=wave_id,
    )


def test_build_attention_feed_drops_closed_wave_advisory() -> None:
    # A daemon stale-wave advisory whose subject wave has since closed must
    # not keep surfacing in the operator's needs_user band.
    closed = _wave(wave_id="P01-I01-W77", status=WaveStatus.CLOSED)
    state = _empty_state().model_copy(update={"waves": {closed.id: closed}})

    assert build_attention_feed(state, (_wave_pause(wave_id=closed.id),)) == ()


def test_build_attention_feed_keeps_active_wave_advisory() -> None:
    # The same advisory while its wave is still active is a live signal.
    active = _wave(wave_id="P01-I01-W77", status=WaveStatus.IN_PROGRESS)
    state = _empty_state().model_copy(update={"waves": {active.id: active}})

    feed = build_attention_feed(state, (_wave_pause(wave_id=active.id),))

    assert len(feed) == 1
    assert feed[0].kind is AttentionKind.NEEDS_USER


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


# --------------------------------------------------------------------------
# D2 -- wave signals scoped to the active phase + iter
# --------------------------------------------------------------------------


def _state_with_nonactive_iter() -> State:
    """Return the empty fixture extended with a second, non-active iter.

    The fixture's active iter is ``P01-I01`` under phase ``P01``. This
    splices in a closed-phase iter ``P00-I01`` (under ``P00``) so a wave
    placed there is *out of active scope* -- the case the obsolete-drop
    must filter.
    """
    base = _empty_state()
    iters = dict(base.iters)
    iters["P00-I01"] = base.iters["P01-I01"].model_copy(
        update={"id": "P00-I01", "phase_id": "P00", "title": "Old iter", "wave_ids": []}
    )
    return base.model_copy(update={"iters": iters})


def test_build_attention_feed_failed_wave_under_nonactive_iter_is_dropped() -> None:
    # A FAILED wave under the closed-phase iter is historical -- it must not
    # surface; an active-iter FAILED wave still does (the obsolete-drop).
    state = _state_with_nonactive_iter().model_copy(
        update={
            "waves": {
                "P00-I01-W01": _wave(
                    wave_id="P00-I01-W01", status=WaveStatus.FAILED, iter_id="P00-I01"
                ),
                "P01-I01-W09": _wave(wave_id="P01-I01-W09", status=WaveStatus.FAILED),
            }
        }
    )
    feed = build_attention_feed(state, ())
    failed = [i for i in feed if i.kind is AttentionKind.FAILED_WAVE]
    assert len(failed) == 1
    assert "P01-I01-W09" in failed[0].title


def test_build_attention_feed_ready_wave_under_nonactive_iter_is_dropped() -> None:
    # A ready-to-claim PENDING wave under a non-active iter is not the
    # operator's current next move, so it must not surface.
    state = _state_with_nonactive_iter().model_copy(
        update={
            "waves": {
                "P00-I01-W02": _wave(
                    wave_id="P00-I01-W02", status=WaveStatus.PENDING, iter_id="P00-I01"
                ),
                "P01-I01-W02": _wave(wave_id="P01-I01-W02", status=WaveStatus.PENDING),
            }
        }
    )
    feed = build_attention_feed(state, ())
    ready = [i for i in feed if i.kind is AttentionKind.READY_WAVE]
    assert len(ready) == 1
    assert "P01-I01-W02" in ready[0].title


def test_build_attention_feed_no_active_iter_drops_all_wave_signals() -> None:
    # With no active iter pointer, no wave is "needs you now" -- the
    # wave-derived signals are fully scoped out (incidents stay point-in-time).
    base = _empty_state()
    state = base.model_copy(
        update={
            "current": base.current.model_copy(update={"iter_id": None, "phase_id": None}),
            "waves": {"P01-I01-W09": _wave(wave_id="P01-I01-W09", status=WaveStatus.FAILED)},
        }
    )
    assert build_attention_feed(state, ()) == ()


# --------------------------------------------------------------------------
# D3 -- relative time-ago formatter + per-kind occurred_at sourcing
# --------------------------------------------------------------------------


def test_format_time_ago_sub_minute_is_now() -> None:
    assert format_time_ago(_NOW, _NOW) == "now"
    assert format_time_ago(datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC), _NOW.replace(minute=1)) != ""


def test_format_time_ago_minutes_hours_days_boundaries() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert format_time_ago(base, base.replace(minute=15)) == "15m ago"
    # 1h12m -> one-decimal hours.
    assert format_time_ago(base, base.replace(hour=13, minute=12)) == "1.2h ago"
    assert format_time_ago(base, base.replace(day=4)) == "3d ago"


def test_format_time_ago_none_is_empty() -> None:
    assert format_time_ago(None, _NOW) == ""


def test_build_attention_feed_sources_occurred_at_per_kind() -> None:
    claimed = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    state = _empty_state().model_copy(
        update={
            "waves": {
                "P01-I01-W09": _wave(
                    wave_id="P01-I01-W09", status=WaveStatus.FAILED, claimed_at=claimed
                )
            },
            "incidents": {"INC-1": _incident(severity=IncidentSeverity.CRITICAL)},
            "open_questions": {"OQ-1": _open_question(status=OpenQuestionStatus.BLOCKED)},
        }
    )
    by_kind = {item.kind: item for item in build_attention_feed(state, ())}
    # Failed wave -> claim time; incident -> opened_at; question -> created_at.
    assert by_kind[AttentionKind.FAILED_WAVE].occurred_at == claimed
    assert by_kind[AttentionKind.INCIDENT].occurred_at == _NOW
    assert by_kind[AttentionKind.OPEN_QUESTION].occurred_at == _NOW


def test_build_attention_feed_pause_occurred_at_flows_through() -> None:
    raised = datetime(2026, 1, 1, 8, 30, tzinfo=UTC)
    pause = _pause(urgency=Urgency.NORMAL, question="q", pause_urn="urn:eawf:v1:event:QR/p")
    pause.occurred_at = raised
    feed = build_attention_feed(_empty_state(), (pause,))
    assert feed[0].occurred_at == raised


# --------------------------------------------------------------------------
# D4 -- live auto-clear + session-level explicit dismiss
# --------------------------------------------------------------------------


def test_build_attention_feed_item_auto_clears_when_source_resolves() -> None:
    # An open incident surfaces a row; closing the incident (resolving the
    # source) removes the row on the next reduce -- the live-reducer contract.
    open_state = _empty_state().model_copy(
        update={"incidents": {"INC-1": _incident(severity=IncidentSeverity.HIGH)}}
    )
    assert len(build_attention_feed(open_state, ())) == 1
    resolved_state = _empty_state().model_copy(
        update={
            "incidents": {
                "INC-1": _incident(severity=IncidentSeverity.HIGH, status=IncidentStatus.RESOLVED)
            }
        }
    )
    assert build_attention_feed(resolved_state, ()) == ()


def test_build_attention_feed_dismissed_key_filters_that_row() -> None:
    # A still-live row whose dismiss_key is in the dismissed set drops out,
    # while a sibling live row stays.
    state = _empty_state().model_copy(
        update={
            "incidents": {
                "INC-1": _incident(severity=IncidentSeverity.HIGH),
                "INC-2": _incident(severity=IncidentSeverity.LOW),
            }
        }
    )
    full = build_attention_feed(state, ())
    assert len(full) == 2
    target = next(i for i in full if i.title.startswith("INC-high"))
    survivor = next(i for i in full if i.title.startswith("INC-low"))
    filtered = build_attention_feed(state, (), dismissed=frozenset({target.dismiss_key}))
    assert [i.title for i in filtered] == [survivor.title]


def test_attention_item_dismiss_key_is_stable_and_repo_namespaced() -> None:
    a = AttentionItem(
        urgency=Urgency.URGENT, kind=AttentionKind.FAILED_WAVE, title="P01-I01-W09 x", detail="d"
    )
    a_again = AttentionItem(
        urgency=Urgency.URGENT, kind=AttentionKind.FAILED_WAVE, title="P01-I01-W09 x", detail="d2"
    )
    # Same logical row -> same key regardless of the (volatile) detail.
    assert a.dismiss_key == a_again.dismiss_key
    # A pause keys on its stable urn, not its (mutable) question title.
    pause_item = AttentionItem(
        urgency=Urgency.NORMAL,
        kind=AttentionKind.NEEDS_USER,
        title="some question text",
        detail="d",
        pause_urn="urn:eawf:v1:event:QR/p",
    )
    assert pause_item.dismiss_key == ":needs_user:urn:eawf:v1:event:QR/p"
    # The repo tag namespaces the key so the same wave id under two repos
    # dismisses independently.
    tagged = AttentionItem(
        urgency=Urgency.URGENT,
        kind=AttentionKind.FAILED_WAVE,
        title="P01-I01-W09 x",
        detail="d",
        repo_tag="ABC",
    )
    assert tagged.dismiss_key != a.dismiss_key
    assert tagged.dismiss_key.startswith("ABC:")


# --------------------------------------------------------------------------
# D1 -- portfolio (cross-repo) aggregation through the registry boundary
# --------------------------------------------------------------------------


def _repo_entry(code: str) -> RegistryRepoEntry:
    return RegistryRepoEntry(code=code, path=f"/nowhere/{code}", title=code)


def _repo_state_with_failed_wave() -> State:
    """A single-repo state whose active iter carries one FAILED wave."""
    return _empty_state().model_copy(
        update={"waves": {"P01-I01-W09": _wave(wave_id="P01-I01-W09", status=WaveStatus.FAILED)}}
    )


def test_build_portfolio_attention_feed_aggregates_and_tags_per_repo() -> None:
    repos = [_repo_entry("ABC"), _repo_entry("DEF")]
    states = {
        Path("/nowhere/ABC"): _repo_state_with_failed_wave(),
        Path("/nowhere/DEF"): _empty_state().model_copy(
            update={"incidents": {"INC-1": _incident(severity=IncidentSeverity.CRITICAL)}}
        ),
    }
    feed = build_portfolio_attention_feed(repos, load_state=lambda p: states.get(p))
    # One row per repo, each tagged with its owning repo code.
    tags = {item.repo_tag for item in feed}
    assert tags == {"ABC", "DEF"}
    # URGENT (critical incident, DEF) ranks ahead of URGENT failed wave (ABC)
    # only via the kind tiebreak -- both are URGENT, failed-wave sorts first.
    assert feed[0].repo_tag == "ABC"
    assert feed[0].kind is AttentionKind.FAILED_WAVE


def test_build_portfolio_attention_feed_skips_unreadable_repo() -> None:
    repos = [_repo_entry("ABC"), _repo_entry("DEF")]

    def _loader(path: Path) -> State | None:
        if path == Path("/nowhere/DEF"):
            return None  # unreadable repo -> skipped, not fatal
        return _repo_state_with_failed_wave()

    feed = build_portfolio_attention_feed(repos, load_state=_loader)
    assert {item.repo_tag for item in feed} == {"ABC"}


def test_build_portfolio_attention_feed_honest_empty_across_clean_portfolio() -> None:
    repos = [_repo_entry("ABC"), _repo_entry("DEF")]
    feed = build_portfolio_attention_feed(repos, load_state=lambda _p: _empty_state())
    assert feed == ()


def test_build_portfolio_attention_feed_dismiss_is_repo_namespaced() -> None:
    repos = [_repo_entry("ABC")]
    state = _repo_state_with_failed_wave()
    feed = build_portfolio_attention_feed(repos, load_state=lambda _p: state)
    assert len(feed) == 1
    key = feed[0].dismiss_key
    assert key.startswith("ABC:")
    filtered = build_portfolio_attention_feed(
        repos, load_state=lambda _p: state, dismissed=frozenset({key})
    )
    assert filtered == ()
