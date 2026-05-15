"""Unit tests for the detail-overlay helpers (P20-I01-W04).

Covers the per-overlay panel builders, the keymap registry, and the
single :func:`open_overlay` dispatch entry. Integration-level golden
snapshots live in ``tests/integration/test_tui_overlays.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.state.enums import (
    Confidence,
    DecisionStatus,
    EffortBucket,
    HypothesisStatus,
    HypothesisVerdict,
    MemoryStatus,
    MemoryTier,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.state.models import (
    CurrentPointers,
    Decision,
    Hypothesis,
    MemorySummary,
    Project,
    State,
    Wave,
)
from eawf.store.kinds.event import EventPayload
from eawf.tui.overlays import (
    KNOWN_OVERLAY_KINDS,
    OVERLAY_KEY_DECISION,
    OVERLAY_KEY_DISPATCH,
    OVERLAY_KEY_EVENTS,
    OVERLAY_KEY_HYPOTHESIS,
    OVERLAY_KEY_MEMORY,
    OVERLAY_KEYMAP,
    build_decision_overlay,
    build_dispatch_overlay,
    build_events_overlay,
    build_hypothesis_overlay,
    build_memory_overlay,
    open_overlay,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc() -> datetime:
    """Deterministic UTC timestamp for fixtures."""
    return datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)


def _make_hypothesis(
    hid: str = "H01-01",
    *,
    text: str = "the canary halves wall-clock",
    status: HypothesisStatus = HypothesisStatus.PENDING,
    verdict: HypothesisVerdict | None = None,
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        scope_id="EAWF",
        text=text,
        metric="latency_p50_ms",
        confirm="p50 <= 80ms across 100 runs",
        reject="p50 > 200ms across 100 runs",
        status=status,
        verdict=verdict,
        audit_id=None,
        source_artifact_id=None,
    )


def _make_decision(
    did: str = "D01",
    *,
    summary: str = "lock TUI on rich",
    rationale: str = "rich already pinned; Textual adds a runtime dep",
    alternatives: list[str] | None = None,
    status: DecisionStatus = DecisionStatus.ACTIVE,
    superseded_by: str | None = None,
) -> Decision:
    return Decision(
        id=did,
        scope_id="EAWF",
        summary=summary,
        rationale=rationale,
        alternatives=alternatives or [],
        status=status,
        created_at=_utc(),
        superseded_by=superseded_by,
    )


def _make_memory(mid: str = "M01") -> MemorySummary:
    return MemorySummary(
        id=mid,
        scope_id="EAWF",
        summary="P20-I01 dogfood findings",
        confidence=Confidence.MEDIUM,
        status=MemoryStatus.ACTIVE,
        store_record_id="urn:eawf:v1:memory:M01",
        review_due=None,
        promoted_to_artifact_id=None,
        tier=MemoryTier.WORKING,
    )


def _make_wave(
    wid: str = "P20-I01-W04",
    *,
    iter_id: str = "P20-I01",
    status: WaveStatus = WaveStatus.PENDING,
    deps: list[str] | None = None,
    blocks: list[str] | None = None,
    success_criteria: list[str] | None = None,
    token_budget: int | None = None,
    tokens_consumed: int = 0,
) -> Wave:
    return Wave(
        id=wid,
        iter_id=iter_id,
        title=f"feat: {wid}",
        status=status,
        deps=deps or [],
        blocks=blocks or [],
        file_scopes=[],
        success_criteria=success_criteria or [],
        agent_role=None,
        effort_bucket=EffortBucket.M,
        claim_session_id=None,
        worktree_id=None,
        token_budget=token_budget,
        tokens_consumed=tokens_consumed,
        outcome=None,
        commit=None,
        opened_at=_utc(),
        closed_at=None,
    )


def _make_event(actor: str = "executor", command: str = "wave claim") -> EventPayload:
    return EventPayload(
        timestamp=_utc(),
        event_type="wave_claim",
        actor=actor,
        command=command,
        args_hash="0" * 16,
        before_state_version="v0",
        after_state_version="v1",
        status="ok",
        message="claimed P20-I01-W04",
    )


def _make_state(
    *,
    waves: list[Wave] | None = None,
    hypotheses: list[Hypothesis] | None = None,
    decisions: list[Decision] | None = None,
    memories: list[MemorySummary] | None = None,
    iter_id: str = "P20-I01",
) -> State:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _utc(),
        "project": Project(
            code="EAWF",
            slug="eawf",
            title="EAWF",
            description=None,
            domains=["dev"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:EAWF",
        ).model_dump(mode="json"),
        "current": CurrentPointers(
            project_code="EAWF",
            phase_id=iter_id.split("-")[0],
            iter_id=iter_id,
        ).model_dump(mode="json"),
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {w.id: w.model_dump(mode="json") for w in (waves or [])},
        "hypotheses": {h.id: h.model_dump(mode="json") for h in (hypotheses or [])} or None,
        "decisions": {d.id: d.model_dump(mode="json") for d in (decisions or [])},
        "memory_index": {m.id: m.model_dump(mode="json") for m in (memories or [])} or None,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _render(renderable: Any) -> str:
    """Render a Rich object into a string buffer for inspection."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Keymap registry / known kinds
# ---------------------------------------------------------------------------


def test_overlay_keys_use_open_verb_prefix() -> None:
    """Every overlay shortcut starts with the open-verb ``o``."""
    assert OVERLAY_KEY_HYPOTHESIS == "oH"
    assert OVERLAY_KEY_DECISION == "oD"
    assert OVERLAY_KEY_MEMORY == "oM"
    assert OVERLAY_KEY_EVENTS == "oE"
    assert OVERLAY_KEY_DISPATCH == "oR"
    for shortcut in OVERLAY_KEYMAP:
        assert shortcut.startswith("o"), f"shortcut {shortcut!r} missing open-verb prefix"


def test_overlay_keymap_covers_every_known_kind() -> None:
    """OVERLAY_KEYMAP is exhaustive over KNOWN_OVERLAY_KINDS."""
    assert set(OVERLAY_KEYMAP.values()) == set(KNOWN_OVERLAY_KINDS)


def test_overlay_keymap_has_no_duplicate_shortcuts() -> None:
    """Two overlays cannot share a shortcut."""
    shortcuts = list(OVERLAY_KEYMAP)
    assert len(shortcuts) == len(set(shortcuts))


def test_known_overlay_kinds_is_five_tuple() -> None:
    """Success criterion 1 — exactly five overlays."""
    assert KNOWN_OVERLAY_KINDS == (
        "hypothesis",
        "decision",
        "memory",
        "events",
        "dispatch",
    )


# ---------------------------------------------------------------------------
# build_hypothesis_overlay
# ---------------------------------------------------------------------------


def test_build_hypothesis_overlay_renders_id_status_and_text() -> None:
    h = _make_hypothesis(hid="H01-01", status=HypothesisStatus.CONFIRMED)
    panel = build_hypothesis_overlay(h)
    rendered = _render(panel)
    assert "hypothesis" in rendered
    assert "H01-01" in rendered
    assert "confirmed" in rendered
    assert "the canary halves wall-clock" in rendered
    assert "latency_p50_ms" in rendered


def test_build_hypothesis_overlay_renders_verdict_placeholder_when_none() -> None:
    panel = build_hypothesis_overlay(_make_hypothesis(verdict=None))
    rendered = _render(panel)
    lines = rendered.splitlines()
    verdict_line = next(line for line in lines if "verdict:" in line)
    assert "-" in verdict_line


def test_build_hypothesis_overlay_renders_verdict_when_set() -> None:
    h = _make_hypothesis(verdict=HypothesisVerdict.REJECTED)
    rendered = _render(build_hypothesis_overlay(h))
    assert "rejected" in rendered


# ---------------------------------------------------------------------------
# build_decision_overlay
# ---------------------------------------------------------------------------


def test_build_decision_overlay_renders_summary_and_rationale() -> None:
    d = _make_decision(did="D15", summary="TUI stack: rich", rationale="no new dep")
    rendered = _render(build_decision_overlay(d))
    assert "decision" in rendered
    assert "D15" in rendered
    assert "TUI stack: rich" in rendered
    assert "no new dep" in rendered


def test_build_decision_overlay_renders_alternatives_when_present() -> None:
    d = _make_decision(alternatives=["Textual", "blessed"])
    rendered = _render(build_decision_overlay(d))
    assert "Textual" in rendered
    assert "blessed" in rendered


def test_build_decision_overlay_uses_placeholder_when_no_alternatives() -> None:
    d = _make_decision(alternatives=[])
    rendered = _render(build_decision_overlay(d))
    lines = rendered.splitlines()
    # Find the alternatives block and verify the next non-border line
    # carries the ``-`` placeholder.
    idx = next(i for i, line in enumerate(lines) if "alternatives:" in line)
    after = lines[idx + 1]
    assert "-" in after


def test_build_decision_overlay_renders_superseded_link_when_set() -> None:
    d = _make_decision(status=DecisionStatus.SUPERSEDED, superseded_by="D22")
    rendered = _render(build_decision_overlay(d))
    assert "D22" in rendered
    assert "superseded" in rendered


# ---------------------------------------------------------------------------
# build_memory_overlay
# ---------------------------------------------------------------------------


def test_build_memory_overlay_renders_tier_status_summary() -> None:
    m = _make_memory()
    rendered = _render(build_memory_overlay(m))
    assert "memory" in rendered
    assert "M01" in rendered
    assert "working" in rendered
    assert "active" in rendered
    assert "P20-I01 dogfood findings" in rendered


def test_build_memory_overlay_renders_review_due_placeholder_when_none() -> None:
    rendered = _render(build_memory_overlay(_make_memory()))
    lines = rendered.splitlines()
    review_line = next(line for line in lines if "review_due:" in line)
    assert "-" in review_line


def test_build_memory_overlay_renders_promoted_link_when_set() -> None:
    m = _make_memory()
    m_with_link = m.model_copy(update={"promoted_to_artifact_id": "ART-99"})
    rendered = _render(build_memory_overlay(m_with_link))
    assert "ART-99" in rendered


# ---------------------------------------------------------------------------
# build_events_overlay
# ---------------------------------------------------------------------------


def test_build_events_overlay_renders_count_header() -> None:
    rendered = _render(build_events_overlay([_make_event(), _make_event()]))
    assert "events" in rendered
    assert "(2 shown)" in rendered


def test_build_events_overlay_empty_uses_placeholder() -> None:
    rendered = _render(build_events_overlay([]))
    assert "(0 shown)" in rendered
    assert "no events to show" in rendered


def test_build_events_overlay_renders_actor_and_command() -> None:
    rendered = _render(build_events_overlay([_make_event(actor="dispatcher")]))
    assert "dispatcher" in rendered
    assert "wave claim" in rendered


# ---------------------------------------------------------------------------
# build_dispatch_overlay
# ---------------------------------------------------------------------------


def test_build_dispatch_overlay_renders_id_status_budget() -> None:
    state = _make_state(
        waves=[
            _make_wave(
                "P20-I01-W04",
                token_budget=8000,
                tokens_consumed=2000,
                success_criteria=["overlay one", "overlay two"],
            ),
        ]
    )
    rendered = _render(build_dispatch_overlay(state.waves["P20-I01-W04"], state=state))
    assert "dispatch" in rendered
    assert "P20-I01-W04" in rendered
    assert "2000 / 8000" in rendered
    assert "overlay one" in rendered
    assert "overlay two" in rendered


def test_build_dispatch_overlay_uses_typed_dag_edges() -> None:
    """Detail must use :func:`wave_graph.edges`, not Wave.blocks directly."""
    state = _make_state(
        waves=[
            _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS, blocks=["P20-I01-W04"]),
            _make_wave("P20-I01-W04", status=WaveStatus.PENDING, deps=["P20-I01-W01"]),
        ]
    )
    rendered = _render(build_dispatch_overlay(state.waves["P20-I01-W04"], state=state))
    # P20-I01-W01 is in_progress → blocks W04 → blocked_by lists it.
    lines = rendered.splitlines()
    blocked_line = next(line for line in lines if "blocked_by:" in line)
    assert "P20-I01-W01" in blocked_line


def test_build_dispatch_overlay_blocked_by_empty_when_dep_closed() -> None:
    state = _make_state(
        waves=[
            _make_wave("P20-I01-W01", status=WaveStatus.CLOSED, blocks=["P20-I01-W04"]),
            _make_wave("P20-I01-W04", status=WaveStatus.PENDING, deps=["P20-I01-W01"]),
        ]
    )
    rendered = _render(build_dispatch_overlay(state.waves["P20-I01-W04"], state=state))
    lines = rendered.splitlines()
    blocked_line = next(line for line in lines if "blocked_by:" in line)
    # The dep is closed, so blocked_by must collapse to ``-``.
    assert "-" in blocked_line


def test_build_dispatch_overlay_renders_dash_for_empty_criteria() -> None:
    state = _make_state(waves=[_make_wave("P20-I01-W04", success_criteria=[])])
    rendered = _render(build_dispatch_overlay(state.waves["P20-I01-W04"], state=state))
    # Empty criteria collapses to a ``-`` placeholder line.
    lines = rendered.splitlines()
    idx = next(i for i, line in enumerate(lines) if "criteria:" in line)
    assert "-" in lines[idx + 1]


# ---------------------------------------------------------------------------
# open_overlay — single dispatch entry
# ---------------------------------------------------------------------------


def test_open_overlay_hypothesis_returns_layout_with_header_and_body() -> None:
    h = _make_hypothesis()
    state = _make_state(hypotheses=[h])
    layout = open_overlay("hypothesis", state, h.id)
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "EAWF" in rendered
    assert "hypothesis H01-01" in rendered
    assert "H01-01" in rendered


def test_open_overlay_decision_returns_layout() -> None:
    d = _make_decision(did="D15")
    state = _make_state(decisions=[d])
    layout = open_overlay("decision", state, d.id)
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "D15" in rendered


def test_open_overlay_memory_returns_layout() -> None:
    m = _make_memory()
    state = _make_state(memories=[m])
    layout = open_overlay("memory", state, m.id)
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "M01" in rendered


def test_open_overlay_events_renders_with_passed_event_list() -> None:
    state = _make_state()
    layout = open_overlay("events", state, "recent", events=[_make_event()])
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "events" in rendered
    assert "recent" in rendered
    assert "executor" in rendered


def test_open_overlay_events_with_no_event_list_renders_empty_placeholder() -> None:
    state = _make_state()
    layout = open_overlay("events", state, "")
    rendered = _render(layout)
    assert "no events to show" in rendered


def test_open_overlay_dispatch_returns_layout() -> None:
    state = _make_state(waves=[_make_wave("P20-I01-W04")])
    layout = open_overlay("dispatch", state, "P20-I01-W04")
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "P20-I01-W04" in rendered
    # Layout header references the dispatch view.
    assert "dispatch P20-I01-W04" in rendered


# ---------------------------------------------------------------------------
# open_overlay — error paths
# ---------------------------------------------------------------------------


def test_open_overlay_unknown_kind_raises_valueerror() -> None:
    state = _make_state()
    with pytest.raises(ValueError, match="unknown overlay_kind"):
        open_overlay("does-not-exist", state, "anything")  # type: ignore[arg-type]


def test_open_overlay_non_string_target_id_raises_typeerror() -> None:
    state = _make_state()
    with pytest.raises(TypeError, match="target_id must be str"):
        open_overlay("hypothesis", state, 123)  # type: ignore[arg-type]


def test_open_overlay_unknown_hypothesis_raises_keyerror() -> None:
    state = _make_state(hypotheses=[_make_hypothesis(hid="H01-01")])
    with pytest.raises(KeyError, match="unknown hypothesis"):
        open_overlay("hypothesis", state, "H99-99")


def test_open_overlay_unknown_decision_raises_keyerror() -> None:
    state = _make_state(decisions=[_make_decision(did="D01")])
    with pytest.raises(KeyError, match="unknown decision"):
        open_overlay("decision", state, "D99")


def test_open_overlay_unknown_memory_raises_keyerror() -> None:
    state = _make_state(memories=[_make_memory("M01")])
    with pytest.raises(KeyError, match="unknown memory"):
        open_overlay("memory", state, "M99")


def test_open_overlay_unknown_wave_raises_keyerror() -> None:
    state = _make_state(waves=[_make_wave("P20-I01-W01")])
    with pytest.raises(KeyError, match="unknown wave"):
        open_overlay("dispatch", state, "P20-I01-W99")


def test_open_overlay_empty_state_buckets_propagate_keyerror() -> None:
    """Missing optional buckets (``None`` decisions / hypotheses / memory) MUST raise."""
    state = _make_state()  # no decisions / hypotheses / memory
    with pytest.raises(KeyError):
        open_overlay("hypothesis", state, "H01-01")
    with pytest.raises(KeyError):
        open_overlay("memory", state, "M01")


# ---------------------------------------------------------------------------
# Brand chassis lives outside-left of breadcrumb (rule consistency)
# ---------------------------------------------------------------------------


def test_open_overlay_brand_left_of_breadcrumb() -> None:
    h = _make_hypothesis()
    state = _make_state(hypotheses=[h])
    rendered = _render(open_overlay("hypothesis", state, h.id))
    brand_idx = rendered.find("Eä")
    project_idx = rendered.find("EAWF")
    assert brand_idx >= 0
    assert project_idx > brand_idx, "brand must sit outside-left of breadcrumb"
