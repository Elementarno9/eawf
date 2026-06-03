"""Unit tests for :class:`eawf.kernel.spec.intent.IntentBrief`.

Covers (1) field bounds + extra-forbid on the W24-audited brief, (2)
nullable + replay-safe attachment on Wave / Iter / Phase / BacklogItem,
(3) intent param plumb-through on the wave / iter plan-edit
transitions, (4) intent payload plumb-through on the daemon
``state.mutate`` ROADMAP_REVISE / PHASE_OPEN / ITER_OPEN paths, and
(5) intent rendering on :func:`render_intent_line` + the TUI detail
overlay's wave / iter / phase / backlog cards.

Post-W61 (third stage of the W52 split) the brief carries the seven
W24-audited fields and the prior legacy triad (``goal`` /
``motivation`` / ``success_signal``) is removed; consumers read the
canonical ``problem`` / ``desired_outcome`` pair directly with no
back-compat fallback.
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


def _minimal_brief(**overrides: object) -> IntentBrief:
    """Build an :class:`IntentBrief` with the two required fields set.

    The W61-ratified shape requires ``problem`` + ``desired_outcome``;
    callers override only the fields they want to exercise.
    """
    payload: dict[str, object] = {
        "problem": "executors lack structured intent",
        "desired_outcome": "every entity carries a typed intent",
    }
    payload.update(overrides)
    return IntentBrief.model_validate(payload)


# ---- Model bounds -----------------------------------------------------------


def test_intent_brief_minimal_required_pair_only() -> None:
    """The two required fields alone validate; optional fields default empty/None."""
    brief = _minimal_brief()
    assert brief.problem == "executors lack structured intent"
    assert brief.desired_outcome == "every entity carries a typed intent"
    assert brief.planned_steps == []
    assert brief.risks == []
    assert brief.priority_rationale is None
    assert brief.evidence_refs == []
    assert brief.source_brief_ids == []


def test_intent_brief_full() -> None:
    brief = IntentBrief(
        problem="executors lack structured intent",
        desired_outcome="every entity carries a typed intent",
        planned_steps=["wire schema", "wire renderer", "wire consumers"],
        risks=["consumers may drift across iters"],
        priority_rationale="W24 audit ranked structured intent above polish",
        evidence_refs=["src/eawf/kernel/spec/intent.py:1"],
        source_brief_ids=[".ea/local/research/2026-05-26-v04-roadmap.md"],
    )
    assert brief.priority_rationale == "W24 audit ranked structured intent above polish"
    assert brief.planned_steps == ["wire schema", "wire renderer", "wire consumers"]
    assert brief.risks == ["consumers may drift across iters"]
    assert brief.evidence_refs == ["src/eawf/kernel/spec/intent.py:1"]


def test_intent_brief_rejects_missing_problem() -> None:
    with pytest.raises(ValidationError):
        IntentBrief.model_validate({"desired_outcome": "ok"})


def test_intent_brief_rejects_missing_desired_outcome() -> None:
    with pytest.raises(ValidationError):
        IntentBrief.model_validate({"problem": "ok"})


def test_intent_brief_rejects_empty_problem() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="", desired_outcome="ok")


def test_intent_brief_rejects_over_cap_problem() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="p" * 201, desired_outcome="ok")


def test_intent_brief_rejects_empty_desired_outcome() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="")


def test_intent_brief_rejects_over_cap_desired_outcome() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="d" * 201)


def test_intent_brief_rejects_extra_field() -> None:
    """Legacy keys (``goal`` / ``motivation`` / ``success_signal``) are extra now."""
    with pytest.raises(ValidationError):
        IntentBrief.model_validate(
            {"problem": "ok", "desired_outcome": "ok", "goal": "no longer allowed"}
        )


def test_intent_brief_rejects_unknown_extra_field() -> None:
    with pytest.raises(ValidationError):
        IntentBrief.model_validate({"problem": "ok", "desired_outcome": "ok", "bogus": "no"})


def test_intent_brief_rejects_empty_evidence_ref() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", evidence_refs=[""])


def test_intent_brief_rejects_empty_planned_step() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", planned_steps=[""])


def test_intent_brief_rejects_over_cap_planned_step() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", planned_steps=["s" * 501])


def test_intent_brief_rejects_over_len_planned_steps_list() -> None:
    """>10 entries on planned_steps fails (max_length=10 on the list)."""
    with pytest.raises(ValidationError):
        IntentBrief(
            problem="ok",
            desired_outcome="ok",
            planned_steps=[f"step {i}" for i in range(11)],
        )


def test_intent_brief_accepts_planned_steps_at_cap() -> None:
    """Exactly 10 entries on planned_steps validates (boundary case)."""
    brief = IntentBrief(
        problem="ok",
        desired_outcome="ok",
        planned_steps=[f"step {i}" for i in range(10)],
    )
    assert len(brief.planned_steps) == 10


def test_intent_brief_rejects_empty_risk() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", risks=[""])


def test_intent_brief_rejects_over_cap_risk() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", risks=["r" * 501])


def test_intent_brief_rejects_over_len_risks_list() -> None:
    """>10 entries on risks fails (max_length=10 on the list)."""
    with pytest.raises(ValidationError):
        IntentBrief(
            problem="ok",
            desired_outcome="ok",
            risks=[f"risk {i}" for i in range(11)],
        )


def test_intent_brief_accepts_risks_at_cap() -> None:
    """Exactly 10 entries on risks validates (boundary case)."""
    brief = IntentBrief(
        problem="ok",
        desired_outcome="ok",
        risks=[f"risk {i}" for i in range(10)],
    )
    assert len(brief.risks) == 10


def test_intent_brief_rejects_over_cap_priority_rationale() -> None:
    with pytest.raises(ValidationError):
        IntentBrief(problem="ok", desired_outcome="ok", priority_rationale="p" * 1001)


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
        intent=_minimal_brief(),
    )
    assert wave.intent is not None
    assert wave.intent.problem == "executors lack structured intent"


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


def test_state_schema_version_accepts_supported_set() -> None:
    """The accepted schema-version set is the migrate-chain supported range."""
    from typing import get_args

    from eawf.kernel.state.models import State as StateModel

    field = StateModel.model_fields["schema_version"]
    versions = set(get_args(field.annotation))
    assert versions == {"1.0", "1.1", "1.2", "1.3", "1.4"}


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
    brief = _minimal_brief(problem="implement the deliverable")
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
    assert wave.intent.problem == "implement the deliverable"


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
    brief = _minimal_brief(problem="patched problem")
    wave = edit_wave_plan(state, wave_id="P01-I01-W01", intent=brief)
    assert wave.intent is not None
    assert wave.intent.problem == "patched problem"


def test_plan_iter_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    brief = _minimal_brief(problem="iter problem")
    it = plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="y",
        intent=brief,
    )
    assert it.intent is not None
    assert it.intent.problem == "iter problem"


def test_edit_iter_plan_accepts_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = _minimal_brief(problem="patched iter problem")
    it = edit_iter_plan(state, iter_id="P01-I01", intent=brief)
    assert it.intent is not None
    assert it.intent.problem == "patched iter problem"


def test_open_phase_accepts_intent() -> None:
    state = _empty_state()
    brief = _minimal_brief(problem="phase problem")
    phase = open_phase(state, phase_id="P01", title="x", intent=brief)
    assert phase.intent is not None
    assert phase.intent.problem == "phase problem"


def test_plan_phase_accepts_intent() -> None:
    state = _empty_state()
    brief = _minimal_brief(problem="planned phase problem")
    phase = plan_phase(state, phase_id="P02", title="x", intent=brief)
    assert phase.intent is not None
    assert phase.intent.problem == "planned phase problem"


# ---- Daemon ROADMAP_REVISE / PHASE_OPEN / ITER_OPEN payload validation ------


_DAEMON_INTENT_PAYLOAD: dict[str, str] = {
    "problem": "daemon-side problem",
    "desired_outcome": "daemon-side desired outcome",
}


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
            "intent": dict(_DAEMON_INTENT_PAYLOAD),
        },
    )
    _apply_roadmap_revise(state, mutation)
    assert state.waves["P01-I01-W01"].intent is not None
    assert state.waves["P01-I01-W01"].intent.problem == "daemon-side problem"


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
            "intent": dict(_DAEMON_INTENT_PAYLOAD),
        },
    )
    _apply_roadmap_revise(state, mutation)
    assert state.waves["P01-I01-W01"].intent is not None
    assert state.waves["P01-I01-W01"].intent.problem == "daemon-side problem"


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
            "intent": dict(_DAEMON_INTENT_PAYLOAD),
        },
    )
    _apply_phase_open(state, mutation)
    assert state.phases["P01"].intent is not None
    assert state.phases["P01"].intent.problem == "daemon-side problem"


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
            "intent": dict(_DAEMON_INTENT_PAYLOAD),
        },
    )
    _apply_iter_open(state, mutation)
    assert state.iters["P01-I01"].intent is not None
    assert state.iters["P01-I01"].intent.problem == "daemon-side problem"


# ---- Render + TUI -----------------------------------------------------------


def test_render_intent_line_none_returns_empty() -> None:
    assert render_intent_line(None) == ""


def test_render_intent_line_emits_problem_and_desired_outcome() -> None:
    brief = IntentBrief(
        problem="executors lack a structured intent surface",
        desired_outcome="every entity carries a typed intent the renderer reads",
    )
    assert render_intent_line(brief) == (
        "problem: executors lack a structured intent surface -> "
        "desired_outcome: every entity carries a typed intent the renderer reads"
    )


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
        intent=_minimal_brief(),
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = dict(card.rows)
    assert labels.get("problem") == "executors lack structured intent"
    assert labels.get("desired outcome") == "every entity carries a typed intent"
    # The legacy ``intent`` row is gone; the W24-audited rows are
    # surfaced directly.
    assert "intent" not in labels


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
    assert "problem" not in labels
    assert "desired outcome" not in labels


def test_detail_overlay_iter_card_surfaces_intent() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="y",
        intent=_minimal_brief(problem="iter problem"),
    )
    card = resolve_detail(state, "P01-I01")
    labels = dict(card.rows)
    assert labels.get("problem") == "iter problem"


def test_detail_overlay_phase_card_surfaces_intent() -> None:
    state = _empty_state()
    open_phase(
        state,
        phase_id="P01",
        title="x",
        intent=_minimal_brief(problem="phase problem"),
    )
    card = resolve_detail(state, "P01")
    labels = dict(card.rows)
    assert labels.get("problem") == "phase problem"


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
            intent=_minimal_brief(problem="backlog problem"),
        ),
    }
    card = resolve_detail(state, "B001")
    labels = dict(card.rows)
    assert labels.get("problem") == "backlog problem"


def test_detail_overlay_wave_card_surfaces_optional_rows_when_set() -> None:
    """Optional rows (planned_steps / risks / priority_rationale) render when set."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = IntentBrief(
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
    assert labels.get("planned steps") == "wire schema; wire renderer"
    assert labels.get("risks") == "consumers may drift"
    assert (
        labels.get("priority rationale")
        == "W24 audit ranked structured intent above the polish backlog"
    )


def test_detail_overlay_wave_card_omits_unset_optional_rows() -> None:
    """Optional rows are skipped when their field is at the default."""
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
        intent=_minimal_brief(),
    )
    card = resolve_detail(state, "P01-I01-W01")
    labels = [label for label, _ in card.rows]
    assert "planned steps" not in labels
    assert "risks" not in labels
    assert "priority rationale" not in labels


# ---- CLI plumbing -----------------------------------------------------------


def test_cli_build_intent_no_flags_returns_none() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem=None,
        intent_desired_outcome=None,
    )
    assert result is None


def test_cli_build_intent_missing_desired_outcome_errors() -> None:
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem="problem stated alone",
        intent_desired_outcome=None,
    )
    assert result is _INTENT_FLAG_ERROR


def test_cli_build_intent_missing_problem_errors() -> None:
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem=None,
        intent_desired_outcome="outcome stated alone",
    )
    assert result is _INTENT_FLAG_ERROR


def test_cli_build_intent_optional_flag_without_required_pair_errors() -> None:
    """An optional --intent-* flag alone still requires both required flags."""
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem=None,
        intent_desired_outcome=None,
        intent_priority_rationale="rationale without the required pair",
    )
    assert result is _INTENT_FLAG_ERROR


def test_cli_build_intent_minimal_required_pair() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem="problem statement",
        intent_desired_outcome="outcome statement",
    )
    assert isinstance(result, IntentBrief)
    assert result.problem == "problem statement"
    assert result.desired_outcome == "outcome statement"


def test_cli_build_intent_full_payload() -> None:
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem="executors lack structured intent",
        intent_desired_outcome="every entity carries a typed intent",
        intent_priority_rationale="audit ranked it above polish",
        intent_planned_steps="wire schema,wire renderer,wire consumers",
        intent_risks="consumers may drift,renderer may regress",
        intent_evidence_refs="a,b,c",
        intent_source_brief_ids="x,y",
    )
    assert isinstance(result, IntentBrief)
    assert result.priority_rationale == "audit ranked it above polish"
    assert result.planned_steps == ["wire schema", "wire renderer", "wire consumers"]
    assert result.risks == ["consumers may drift", "renderer may regress"]
    assert result.evidence_refs == ["a", "b", "c"]
    assert result.source_brief_ids == ["x", "y"]


def test_cli_build_intent_planned_steps_empty_string_yields_empty_list() -> None:
    """Empty string for the comma-separated lists yields an empty list, not [""].

    The any-flag gate fires off ``v is not None`` (so the empty string
    counts as a passed flag), but ``_split_csv`` strips it down to an
    empty list rather than ``[""]``. The brief still validates because
    the model treats an empty list as the default.
    """
    from eawf.surfaces.cli.commands.roadmap import _build_intent_from_flags

    result = _build_intent_from_flags(
        intent_problem="problem statement",
        intent_desired_outcome="outcome statement",
        intent_planned_steps="",
        intent_risks="",
    )
    assert isinstance(result, IntentBrief)
    assert result.planned_steps == []
    assert result.risks == []


# ---- Lifecycle logger keys (W61) --------------------------------------------


def test_edit_wave_plan_logs_intent_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The edit log line emits the canonical ``intent_problem=`` key.

    Post-W61 there is no fallback to a legacy goal key — the log
    line carries exactly one intent key whose value is the repr of
    the brief's ``problem`` when an intent is attached.
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
    brief = _minimal_brief(problem="structured intent missing")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.wave"):
        edit_wave_plan(state, wave_id="P01-I01-W01", intent=brief)
    matching = [record for record in caplog.records if "edit_wave_plan" in record.message]
    assert matching, "expected an edit_wave_plan log record"
    assert "intent_problem='structured intent missing'" in matching[-1].message


def test_edit_wave_plan_logs_intent_problem_none_without_intent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An edit without an intent leaves ``intent_problem=None`` on the log line."""
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
        edit_wave_plan(state, wave_id="P01-I01-W01", title="renamed")
    matching = [record for record in caplog.records if "edit_wave_plan" in record.message]
    assert matching, "expected an edit_wave_plan log record"
    assert "intent_problem=None" in matching[-1].message


def test_edit_iter_plan_logs_intent_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    brief = _minimal_brief(problem="iter intent missing")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.iter_"):
        edit_iter_plan(state, iter_id="P01-I01", intent=brief)
    matching = [record for record in caplog.records if "edit_iter_plan" in record.message]
    assert matching, "expected an edit_iter_plan log record"
    assert "intent_problem='iter intent missing'" in matching[-1].message


def test_edit_iter_plan_logs_intent_problem_none_without_intent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    with caplog.at_level("INFO", logger="eawf.workflow.lifecycle.iter_"):
        edit_iter_plan(state, iter_id="P01-I01", title="renamed")
    matching = [record for record in caplog.records if "edit_iter_plan" in record.message]
    assert matching, "expected an edit_iter_plan log record"
    assert "intent_problem=None" in matching[-1].message
