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


def test_intent_brief_empty_body_still_validates() -> None:
    """A brief with an empty body re-validates so legacy on-disk state loads.

    The model stays permissive (no validator) so a brief written
    before the guard existed -- blank ``priority_rationale``, empty
    ``planned_steps`` / ``risks`` -- still constructs at load / replay.
    """
    brief = IntentBrief.model_validate(
        {
            "problem": "legacy brief with an empty body",
            "desired_outcome": "still loads from on-disk state",
            "priority_rationale": "",
            "planned_steps": [],
            "risks": [],
        }
    )
    assert brief.priority_rationale == ""
    assert brief.planned_steps == []
    assert brief.risks == []


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
    assert versions == {
        "1.0",
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "1.10",
        "1.11",
        "1.12",
        "1.13",
        "1.14",
        "1.15",
        "1.16",
        "1.17",
        "1.18",
        "1.19",
        "1.20",
    }


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


# ---- CLI plumbing -----------------------------------------------------------


# ---- Lifecycle logger keys --------------------------------------------
