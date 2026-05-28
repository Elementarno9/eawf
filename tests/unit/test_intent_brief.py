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


# ---- W24-audited additive fields (W59 stage 1 of 3) -------------------------
#
# W59 added five Optional fields alongside the legacy five so consumers can
# migrate one wave at a time (W60). The legacy fields stay required-or-default
# until W61 swaps the canonical set. The tests below pin the additive
# invariant — old-only validates, new-only validates, both validate — plus the
# per-field bounds the renderer + EviBound gate will rely on once W60 wires
# the fields through.


def test_intent_brief_old_fields_only_validates() -> None:
    """Pre-W59 callers (goal + legacy companions only) still validate."""
    brief = IntentBrief(
        goal="ship the wave",
        motivation="planner needs goal threaded to executor",
        success_signal="every wave detail card surfaces a goal row",
        evidence_refs=["src/eawf/kernel/spec/intent.py:1"],
        source_brief_ids=[".ea/local/research/2026-05-26-v04-roadmap.md"],
    )
    assert brief.problem is None
    assert brief.desired_outcome is None
    assert brief.planned_steps == []
    assert brief.risks == []
    assert brief.priority_rationale is None


def test_intent_brief_new_fields_only_validates() -> None:
    """W24-audited fields alone (plus the still-required `goal`) validate."""
    brief = IntentBrief(
        goal="ship the wave",
        problem="executors lack a structured intent surface",
        desired_outcome="every entity carries a typed intent the renderer reads",
        planned_steps=["wire the schema", "wire the renderer", "wire the consumers"],
        risks=["consumers may drift if the migration spans multiple iters"],
        priority_rationale="W24 audit ranked structured intent above the renderer polish backlog",
    )
    assert brief.problem == "executors lack a structured intent surface"
    assert brief.desired_outcome == "every entity carries a typed intent the renderer reads"
    assert brief.planned_steps == [
        "wire the schema",
        "wire the renderer",
        "wire the consumers",
    ]
    assert brief.risks == [
        "consumers may drift if the migration spans multiple iters",
    ]
    assert (
        brief.priority_rationale
        == "W24 audit ranked structured intent above the renderer polish backlog"
    )
    # Legacy fields stay at their defaults so the additive invariant holds.
    assert brief.motivation is None
    assert brief.success_signal is None
    assert brief.evidence_refs == []
    assert brief.source_brief_ids == []


def test_intent_brief_both_field_sets_validate() -> None:
    """Mixed briefs (legacy + W24 fields populated) validate cleanly."""
    brief = IntentBrief(
        goal="ship the wave",
        motivation="planner needs goal threaded to executor",
        success_signal="every wave detail card surfaces a goal row",
        evidence_refs=["src/eawf/kernel/spec/intent.py:1"],
        source_brief_ids=[".ea/local/research/2026-05-26-v04-roadmap.md"],
        problem="executors lack a structured intent surface",
        desired_outcome="every entity carries a typed intent the renderer reads",
        planned_steps=["wire the schema", "wire the renderer"],
        risks=["consumers may drift across iters"],
        priority_rationale="W24 audit ranked structured intent above the polish backlog",
    )
    assert brief.goal == "ship the wave"
    assert brief.problem == "executors lack a structured intent surface"
    assert brief.evidence_refs == ["src/eawf/kernel/spec/intent.py:1"]
    assert brief.planned_steps == ["wire the schema", "wire the renderer"]


def test_intent_brief_rejects_empty_problem() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", problem="")


def test_intent_brief_rejects_over_cap_problem() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", problem="p" * 201)


def test_intent_brief_rejects_empty_desired_outcome() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", desired_outcome="")


def test_intent_brief_rejects_over_cap_desired_outcome() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", desired_outcome="d" * 201)


def test_intent_brief_rejects_empty_planned_step() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", planned_steps=[""])


def test_intent_brief_rejects_over_cap_planned_step() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", planned_steps=["s" * 501])


def test_intent_brief_rejects_over_len_planned_steps_list() -> None:
    """>10 entries on planned_steps fails (max_length=10 on the list)."""
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", planned_steps=[f"step {i}" for i in range(11)])


def test_intent_brief_accepts_planned_steps_at_cap() -> None:
    """Exactly 10 entries on planned_steps validates (boundary case)."""
    brief = IntentBrief(goal="ok", planned_steps=[f"step {i}" for i in range(10)])
    assert len(brief.planned_steps) == 10


def test_intent_brief_rejects_empty_risk() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", risks=[""])


def test_intent_brief_rejects_over_cap_risk() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", risks=["r" * 501])


def test_intent_brief_rejects_over_len_risks_list() -> None:
    """>10 entries on risks fails (max_length=10 on the list)."""
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", risks=[f"risk {i}" for i in range(11)])


def test_intent_brief_accepts_risks_at_cap() -> None:
    """Exactly 10 entries on risks validates (boundary case)."""
    brief = IntentBrief(goal="ok", risks=[f"risk {i}" for i in range(10)])
    assert len(brief.risks) == 10


def test_intent_brief_rejects_over_cap_priority_rationale() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(goal="ok", priority_rationale="p" * 1001)


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


def test_render_intent_line_prefers_problem_and_desired_outcome() -> None:
    """W60 migration: ``problem`` + ``desired_outcome`` outrank ``goal``."""
    brief = IntentBrief(
        goal="legacy goal that should be hidden",
        problem="executors lack a structured intent surface",
        desired_outcome="every entity carries a typed intent the renderer reads",
    )
    assert render_intent_line(brief) == (
        "problem: executors lack a structured intent surface -> "
        "desired_outcome: every entity carries a typed intent the renderer reads"
    )


def test_render_intent_line_problem_only_falls_through() -> None:
    """Only ``problem`` set → emit a single ``problem:`` row, ignoring ``goal``."""
    brief = IntentBrief(
        goal="legacy goal that should be hidden",
        problem="executors lack a structured intent surface",
    )
    assert render_intent_line(brief) == ("problem: executors lack a structured intent surface")


def test_render_intent_line_desired_outcome_only_falls_through() -> None:
    """Only ``desired_outcome`` set → emit a single ``desired_outcome:`` row."""
    brief = IntentBrief(
        goal="legacy goal that should be hidden",
        desired_outcome="every entity carries a typed intent the renderer reads",
    )
    assert render_intent_line(brief) == (
        "desired_outcome: every entity carries a typed intent the renderer reads"
    )


def test_render_intent_line_legacy_only_falls_back_to_goal() -> None:
    """Neither audited field set → fall back to the legacy ``goal:`` row."""
    brief = IntentBrief(goal="ship the wave", motivation="planner needs context")
    assert render_intent_line(brief) == "goal: ship the wave"


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


def test_detail_overlay_wave_card_surfaces_w24_audited_fields() -> None:
    """W60: W24-audited brief surfaces audited rows.

    The legacy ``intent`` row is suppressed when ``problem`` or
    ``desired_outcome`` is set so the renderer does not duplicate the
    information across two rows.
    """
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = IntentBrief(
        goal="legacy goal hidden by audited fields",
        problem="executors lack a structured intent surface",
        desired_outcome="every entity carries a typed intent the renderer reads",
        planned_steps=["wire schema", "wire renderer"],
        risks=["consumers may drift"],
        priority_rationale="W24 audit ranked structured intent above the polish backlog",
    )
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/a.py"],
        effort_bucket=EffortBucket.S,
        intent=brief,
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = dict(card.rows)
    assert labels.get("problem") == "executors lack a structured intent surface"
    assert labels.get("desired outcome") == "every entity carries a typed intent the renderer reads"
    assert labels.get("planned steps") == "wire schema; wire renderer"
    assert labels.get("risks") == "consumers may drift"
    assert (
        labels.get("priority rationale")
        == "W24 audit ranked structured intent above the polish backlog"
    )
    # Legacy intent row is suppressed once an audited field is set so
    # the goal is not double-rendered.
    assert "intent" not in labels


def test_detail_overlay_wave_card_partial_audited_keeps_legacy_intent_hidden() -> None:
    """Setting only ``problem`` still suppresses the legacy ``intent`` row."""
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
        intent=IntentBrief(
            goal="legacy goal",
            problem="executors lack a structured intent surface",
        ),
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = dict(card.rows)
    assert labels.get("problem") == "executors lack a structured intent surface"
    assert "desired outcome" not in labels
    assert "intent" not in labels


def test_detail_overlay_iter_card_surfaces_w24_audited_fields() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="y",
        intent=IntentBrief(
            goal="legacy",
            problem="iter has no structured intent",
            desired_outcome="iter carries a typed brief",
        ),
    )
    card = resolve_detail(state, "P01-I01")
    labels = dict(card.rows)
    assert labels.get("problem") == "iter has no structured intent"
    assert labels.get("desired outcome") == "iter carries a typed brief"
    assert "intent" not in labels


def test_detail_overlay_phase_card_surfaces_w24_audited_fields() -> None:
    state = _empty_state()
    open_phase(
        state,
        phase_id="P01",
        title="x",
        intent=IntentBrief(
            goal="legacy",
            priority_rationale="phase ranked above polish backlog",
        ),
    )
    card = resolve_detail(state, "P01")
    labels = dict(card.rows)
    # Only priority_rationale set: legacy ``intent`` row still renders
    # because neither ``problem`` nor ``desired_outcome`` is populated.
    assert labels.get("intent") == "legacy"
    assert labels.get("priority rationale") == "phase ranked above polish backlog"


def test_detail_overlay_backlog_card_surfaces_w24_audited_fields() -> None:
    state = _empty_state()
    state.backlog = {
        "B001": BacklogItem(
            id="B001",
            scope_id="QR",
            title="t",
            priority=BacklogPriority.P2,
            status=BacklogStatus.OPEN,
            created_at=_now(),
            intent=IntentBrief(
                goal="legacy",
                problem="backlog has no structured intent",
                desired_outcome="backlog carries a typed brief",
                planned_steps=["draft brief", "ratify"],
                risks=["scope creep"],
            ),
        ),
    }
    card = resolve_detail(state, "B001")
    labels = dict(card.rows)
    assert labels.get("problem") == "backlog has no structured intent"
    assert labels.get("desired outcome") == "backlog carries a typed brief"
    assert labels.get("planned steps") == "draft brief; ratify"
    assert labels.get("risks") == "scope creep"
    assert "intent" not in labels


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


def test_cli_build_intent_w24_audited_fields_only() -> None:
    """W60: the 5 new audited flags alone (+ required goal) construct a brief."""
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal="g",
        intent_motivation=None,
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
        intent_problem="executors lack structured intent",
        intent_desired_outcome="every entity carries a typed intent",
        intent_priority_rationale="audit ranked it above polish",
        intent_planned_steps="wire schema,wire renderer,wire consumers",
        intent_risks="consumers may drift,renderer may regress",
    )
    assert isinstance(result, IntentBrief)
    assert result.problem == "executors lack structured intent"
    assert result.desired_outcome == "every entity carries a typed intent"
    assert result.priority_rationale == "audit ranked it above polish"
    assert result.planned_steps == ["wire schema", "wire renderer", "wire consumers"]
    assert result.risks == ["consumers may drift", "renderer may regress"]
    # Legacy fields stay at their defaults.
    assert result.motivation is None
    assert result.success_signal is None


def test_cli_build_intent_mixed_legacy_and_audited() -> None:
    """W60: both legacy and audited flags coexist on a single brief."""
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal="g",
        intent_motivation="m",
        intent_success_signal="s",
        intent_evidence_refs="a,b",
        intent_source_brief_ids="x",
        intent_problem="p",
        intent_desired_outcome="d",
        intent_priority_rationale="pr",
        intent_planned_steps="step1,step2",
        intent_risks="risk1",
    )
    assert isinstance(result, IntentBrief)
    assert result.goal == "g"
    assert result.motivation == "m"
    assert result.problem == "p"
    assert result.desired_outcome == "d"
    assert result.priority_rationale == "pr"
    assert result.planned_steps == ["step1", "step2"]
    assert result.risks == ["risk1"]


def test_cli_build_intent_problem_without_goal_errors() -> None:
    """W60: passing a new flag without --intent-goal still emits the sentinel."""
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal=None,
        intent_motivation=None,
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
        intent_problem="problem statement without a goal",
    )
    assert result is _INTENT_FLAG_ERROR


def test_cli_build_intent_planned_steps_empty_string_yields_empty_list() -> None:
    """Empty string for the comma-separated lists yields an empty list, not [""].

    The any-flag gate fires off ``v is not None`` (so the empty string
    counts as a passed flag), but ``_split_csv`` strips it down to an
    empty list rather than ``[""]``. The brief still validates because
    the model treats an empty list as the default.
    """
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_goal="g",
        intent_motivation=None,
        intent_success_signal=None,
        intent_evidence_refs=None,
        intent_source_brief_ids=None,
        intent_planned_steps="",
        intent_risks="",
    )
    assert isinstance(result, IntentBrief)
    assert result.planned_steps == []
    assert result.risks == []


# ---- Lifecycle logger preference (W60) --------------------------------------


def test_edit_wave_plan_logs_intent_problem_when_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setting ``problem`` populates ``intent_problem=`` and leaves ``intent_goal=None``.

    The log line carries both keys at fixed positions so the
    log-format linter sees a stable ``<funcname> key=value`` shape;
    exactly one of the two values is the repr of a populated field,
    the other stays ``None``.
    """
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
    brief = IntentBrief(goal="legacy", problem="structured intent missing")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.wave"):
        edit_wave_plan(state, wave_id="P01-I01-W01", intent=brief)
    matching = [record for record in caplog.records if "edit_wave_plan" in record.message]
    assert matching, "expected an edit_wave_plan log record"
    assert "intent_problem='structured intent missing'" in matching[-1].message
    assert "intent_goal=None" in matching[-1].message


def test_edit_wave_plan_falls_back_to_intent_goal_log_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy brief populates ``intent_goal=`` and leaves ``intent_problem=None``."""
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
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.wave"):
        edit_wave_plan(state, wave_id="P01-I01-W01", intent=IntentBrief(goal="ship it"))
    matching = [record for record in caplog.records if "edit_wave_plan" in record.message]
    assert matching, "expected an edit_wave_plan log record"
    assert "intent_problem=None" in matching[-1].message
    assert "intent_goal='ship it'" in matching[-1].message


def test_edit_iter_plan_logs_intent_problem_when_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = IntentBrief(goal="legacy", problem="iter intent missing")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.iter_"):
        edit_iter_plan(state, iter_id="P01-I01", intent=brief)
    matching = [record for record in caplog.records if "edit_iter_plan" in record.message]
    assert matching, "expected an edit_iter_plan log record"
    assert "intent_problem='iter intent missing'" in matching[-1].message
    assert "intent_goal=None" in matching[-1].message


def test_edit_iter_plan_falls_back_to_intent_goal_log_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.iter_"):
        edit_iter_plan(state, iter_id="P01-I01", intent=IntentBrief(goal="iter goal"))
    matching = [record for record in caplog.records if "edit_iter_plan" in record.message]
    assert matching, "expected an edit_iter_plan log record"
    assert "intent_problem=None" in matching[-1].message
    assert "intent_goal='iter goal'" in matching[-1].message
