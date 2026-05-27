"""Unit tests for :class:`eawf.kernel.spec.intent.IntentBrief`.

Covers (1) field bounds + extra-forbid on the standalone brief, (2)
nullable + replay-safe attachment on Wave / Iter / Phase / BacklogItem,
(3) intent param plumb-through on the wave / iter plan-edit
transitions, (4) intent payload plumb-through on the daemon
``state.mutate`` ROADMAP_REVISE / PHASE_OPEN / ITER_OPEN paths, and
(5) intent rendering on :func:`render_intent_line` + the TUI detail
overlay's wave / iter / phase / backlog cards.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    BacklogItem,
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.surfaces.render.agents_md import render_intent_line
from eawf.surfaces.tui.screens.overlays.detail import resolve_detail
from eawf.workflow.lifecycle.iter_ import edit_iter_plan, open_iter, plan_iter
from eawf.workflow.lifecycle.phase import open_phase, plan_phase
from eawf.workflow.lifecycle.wave import edit_wave_plan, plan_wave

# ---- Model bounds -----------------------------------------------------------


def test_intent_brief_minimal_goal_only() -> None:
    """A goal alone is enough; the other fields default to None / empty list."""
    brief = IntentBrief(goal="add intent to lifecycle entities")
    assert brief.goal == "add intent to lifecycle entities"
    assert brief.motivation is None
    assert brief.success_signal is None
    assert brief.evidence_refs == []
    assert brief.source_brief_ids == []


def test_intent_brief_full() -> None:
    brief = IntentBrief(
        goal="ship intent on entities",
        motivation="planner needs goal threaded to executor",
        success_signal="every wave detail card surfaces a goal row",
        evidence_refs=["src/eawf/kernel/spec/intent.py:1"],
        source_brief_ids=[".ea/local/research/2026-05-26-v04-roadmap.md"],
    )
    assert brief.motivation == "planner needs goal threaded to executor"
    assert brief.success_signal == "every wave detail card surfaces a goal row"
    assert brief.evidence_refs == ["src/eawf/kernel/spec/intent.py:1"]


def test_intent_brief_rejects_empty_goal() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="")


def test_intent_brief_rejects_over_cap_goal() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="g" * 201)


def test_intent_brief_rejects_over_cap_motivation() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", motivation="m" * 1001)


def test_intent_brief_rejects_over_cap_success_signal() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", success_signal="s" * 501)


def test_intent_brief_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        IntentBrief.model_validate({"goal": "ok", "bogus": "no"})


def test_intent_brief_rejects_empty_evidence_ref() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", evidence_refs=[""])


# ---- Nullable attachment on entities ----------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def test_wave_intent_default_none() -> None:
    wave = Wave(
        id="P01-I01-W01",
        iter_id="P01-I01",
        title="t",
        status=WaveStatus.PENDING,
        opened_at=_now(),
    )
    assert wave.intent is None


def test_wave_accepts_intent() -> None:
    wave = Wave(
        id="P01-I01-W01",
        iter_id="P01-I01",
        title="t",
        status=WaveStatus.PENDING,
        opened_at=_now(),
        intent=IntentBrief(goal="ship the wave"),
    )
    assert wave.intent is not None
    assert wave.intent.goal == "ship the wave"


def test_iter_intent_default_none() -> None:
    it = Iter(
        id="P01-I01",
        phase_id="P01",
        title="t",
        status=IterStatus.PLANNED,
        opened_at=_now(),
    )
    assert it.intent is None


def test_phase_intent_default_none() -> None:
    phase = Phase(
        id="P01",
        scope_id="QR",
        title="t",
        status=PhaseStatus.PLANNED,
        opened_at=_now(),
    )
    assert phase.intent is None


def test_backlog_intent_default_none() -> None:
    item = BacklogItem(
        id="B001",
        scope_id="QR",
        title="t",
        priority=BacklogPriority.P2,
        status=BacklogStatus.OPEN,
        created_at=_now(),
    )
    assert item.intent is None


# ---- Replay-safe: existing on-disk rows without `intent` re-validate --------


def test_state_validates_without_intent_field_on_entities() -> None:
    """A payload mirroring the pre-W11 wire format (no `intent` keys) loads."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": Project(
            code="QR",
            slug="qr",
            title="QR",
            description=None,
            domains=["x"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:QR",
        ).model_dump(mode="json"),
        "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
        "workspace": None,
        "phases": {
            "P01": Phase(
                id="P01",
                scope_id="QR",
                title="t",
                status=PhaseStatus.ACTIVE,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "iters": {
            "P01-I01": Iter(
                id="P01-I01",
                phase_id="P01",
                title="t",
                status=IterStatus.ACTIVE,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "waves": {
            "P01-I01-W01": Wave(
                id="P01-I01-W01",
                iter_id="P01-I01",
                title="t",
                status=WaveStatus.PENDING,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "backlog": {
            "B001": BacklogItem(
                id="B001",
                scope_id="QR",
                title="t",
                priority=BacklogPriority.P2,
                status=BacklogStatus.OPEN,
                created_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
    }
    state = State.model_validate(payload)
    assert state.waves["P01-I01-W01"].intent is None
    assert state.iters["P01-I01"].intent is None
    assert state.phases["P01"].intent is None
    assert state.backlog is not None
    assert state.backlog["B001"].intent is None


def test_state_schema_version_unchanged() -> None:
    """The intent field is additive — schema_version still accepts only 1.0/1.1."""
    from typing import get_args

    from eawf.kernel.state.models import State as StateModel

    field = StateModel.model_fields["schema_version"]
    versions = set(get_args(field.annotation))
    assert versions == {"1.0", "1.1"}


# ---- Transition plumb-through -----------------------------------------------


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _now().isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_plan_wave_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = IntentBrief(goal="implement the deliverable")
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
        intent=brief,
    )
    assert wave.intent is not None
    assert wave.intent.goal == "implement the deliverable"


def test_edit_wave_plan_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
    )
    assert state.waves["P01-I01-W01"].intent is None
    brief = IntentBrief(goal="patched intent")
    wave = edit_wave_plan(state, wave_id="P01-I01-W01", intent=brief)
    assert wave.intent is not None
    assert wave.intent.goal == "patched intent"


def test_plan_iter_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    brief = IntentBrief(goal="iter intent")
    it = plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="y",
        intent=brief,
    )
    assert it.intent is not None
    assert it.intent.goal == "iter intent"


def test_edit_iter_plan_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = IntentBrief(goal="patched iter intent")
    it = edit_iter_plan(state, iter_id="P01-I01", intent=brief)
    assert it.intent is not None
    assert it.intent.goal == "patched iter intent"


def test_open_phase_accepts_intent() -> None:
    state = _empty_state()
    brief = IntentBrief(goal="phase intent")
    phase = open_phase(state, phase_id="P01", title="x", intent=brief)
    assert phase.intent is not None
    assert phase.intent.goal == "phase intent"


def test_plan_phase_accepts_intent() -> None:
    state = _empty_state()
    brief = IntentBrief(goal="planned phase intent")
    phase = plan_phase(state, phase_id="P02", title="x", intent=brief)
    assert phase.intent is not None
    assert phase.intent.goal == "planned phase intent"


# ---- Daemon ROADMAP_REVISE / PHASE_OPEN / ITER_OPEN payload validation ------


def test_apply_roadmap_revise_add_wave_with_intent() -> None:
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_roadmap_revise

    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P01",
        mutation_id="m-1",
        params={
            "op": "add_wave",
            "wave_id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "w",
            "file_scopes": ["src/a.py"],
            "effort_bucket": "S",
            "intent": {"goal": "wave goal from daemon"},
        },
    )
    _apply_roadmap_revise(state, mutation)
    assert state.waves["P01-I01-W01"].intent is not None
    assert state.waves["P01-I01-W01"].intent.goal == "wave goal from daemon"


def test_apply_roadmap_revise_retitle_with_intent() -> None:
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_roadmap_revise

    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
    )
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P01",
        mutation_id="m-2",
        params={
            "op": "retitle",
            "wave_id": "P01-I01-W01",
            "intent": {"goal": "retitle-time wave intent"},
        },
    )
    _apply_roadmap_revise(state, mutation)
    assert state.waves["P01-I01-W01"].intent is not None
    assert state.waves["P01-I01-W01"].intent.goal == "retitle-time wave intent"


def test_apply_roadmap_revise_rejects_bogus_intent() -> None:
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_roadmap_revise

    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P01",
        mutation_id="m-3",
        params={
            "op": "add_wave",
            "wave_id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "w",
            "file_scopes": ["src/a.py"],
            "effort_bucket": "S",
            "intent": {"bogus": "field"},
        },
    )
    with pytest.raises(ValidationError):
        _apply_roadmap_revise(state, mutation)


def test_apply_phase_open_with_intent() -> None:
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_phase_open

    state = _empty_state()
    mutation = Mutation(
        kind=MutationKind.PHASE_OPEN,
        scope_id="P01",
        mutation_id="m-p",
        params={
            "phase_id": "P01",
            "title": "x",
            "intent": {"goal": "phase opened with intent"},
        },
    )
    _apply_phase_open(state, mutation)
    assert state.phases["P01"].intent is not None
    assert state.phases["P01"].intent.goal == "phase opened with intent"


def test_apply_iter_open_with_intent() -> None:
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_iter_open

    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    mutation = Mutation(
        kind=MutationKind.ITER_OPEN,
        scope_id="P01-I01",
        mutation_id="m-i",
        params={
            "iter_id": "P01-I01",
            "phase_id": "P01",
            "title": "y",
            "intent": {"goal": "iter opened with intent"},
        },
    )
    _apply_iter_open(state, mutation)
    assert state.iters["P01-I01"].intent is not None
    assert state.iters["P01-I01"].intent.goal == "iter opened with intent"


# ---- Render + TUI -----------------------------------------------------------


def test_render_intent_line_none_returns_empty() -> None:
    assert render_intent_line(None) == ""


def test_render_intent_line_emits_goal_row() -> None:
    brief = IntentBrief(goal="ship intent on entities")
    assert render_intent_line(brief) == "goal: ship intent on entities"


def test_detail_overlay_wave_card_surfaces_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
        intent=IntentBrief(goal="ship the wave"),
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = dict(card.rows)
    assert labels.get("intent") == "ship the wave"


def test_detail_overlay_wave_card_omits_intent_when_absent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = [label for label, _ in card.rows]
    assert "intent" not in labels


def test_detail_overlay_iter_card_surfaces_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="y",
        intent=IntentBrief(goal="iter goal"),
    )
    card = resolve_detail(state, "P01-I01")
    labels = dict(card.rows)
    assert labels.get("intent") == "iter goal"


def test_detail_overlay_phase_card_surfaces_intent() -> None:
    state = _empty_state()
    open_phase(
        state,
        phase_id="P01",
        title="x",
        intent=IntentBrief(goal="phase goal"),
    )
    card = resolve_detail(state, "P01")
    labels = dict(card.rows)
    assert labels.get("intent") == "phase goal"


def test_detail_overlay_backlog_card_surfaces_intent() -> None:
    state = _empty_state()
    state.backlog = {
        "B001": BacklogItem(
            id="B001",
            scope_id="QR",
            title="t",
            priority=BacklogPriority.P2,
            status=BacklogStatus.OPEN,
            created_at=_now(),
            intent=IntentBrief(goal="backlog goal"),
        ),
    }
    card = resolve_detail(state, "B001")
    labels = dict(card.rows)
    assert labels.get("intent") == "backlog goal"


# ---- CLI plumbing -----------------------------------------------------------


def test_cli_build_intent_no_flags_returns_none() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal=None,
        intent_motivation=None,
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
    )
    assert result is None


def test_cli_build_intent_motivation_without_goal_errors() -> None:
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal=None,
        intent_motivation="just because",
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
    )
    assert result is _INTENT_FLAG_ERROR


def test_cli_build_intent_minimal_goal() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal="ship the wave",
        intent_motivation=None,
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
    )
    assert isinstance(result, IntentBrief)
    assert result.goal == "ship the wave"


def test_cli_build_intent_full_payload() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal="g",
        intent_motivation="m",
        intent_success_signal="s",
        intent_evidence_refs="a,b,c",
        intent_source_brief_ids="x,y",
    )
    assert isinstance(result, IntentBrief)
    assert result.motivation == "m"
    assert result.success_signal == "s"
    assert result.evidence_refs == ["a", "b", "c"]
    assert result.source_brief_ids == ["x", "y"]
