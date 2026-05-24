"""Unit tests for :class:`eawf.workflow.skills.research.ResearchSkill`.

Pin the Phase 4 W02 acceptance contract for ``/research``:

- Happy path (probe ok + default depth) → ``status=ok`` envelope with a
  populated :class:`ResearchBody`.
- Probe-blocked path → ``status=blocked`` with non-empty
  ``footer.repair_commands``.
- ``--depth quick|normal|deep`` flag honoured: question-slot count
  scales with depth and ``deep`` short-circuits to ``status=needs_user``.
- Body schema fields (``brief_id``, ``questions``, ``options``,
  ``recommendation``) populated.
- Each algorithm step writes one ``EVENT`` row to ``store/event.jsonl``.

The tests use ``EA_STATE`` to redirect the active state path under a
``tmp_path`` so the engine appends events into a sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import orjson
import pytest

from eawf.surfaces.render.envelope import EnvelopeWarning
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.workflow.skills.research import ResearchSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def test_research_happy_path_status_ok(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok", env.body
    assert env.header.skill == "/research"


def test_research_body_populated(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    assert isinstance(env.body, dict)
    body = ResearchBody.model_validate(env.body)
    assert body.brief_id.startswith("BR-")
    assert len(body.brief_id) > 3
    assert len(body.questions) == 2  # default depth=normal → 2 slots
    assert len(body.options) == 2
    assert body.recommendation is not None
    assert body.recommendation.choice == body.options[0].name


def test_research_quick_depth_scales_questions(state_dir: Path) -> None:
    skill = ResearchSkill()
    ctx = _ctx()
    ctx.args = {"depth": "quick"}
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert len(body.questions) == 1


def test_research_deep_depth_returns_needs_user(state_dir: Path) -> None:
    skill = ResearchSkill()
    ctx = _ctx()
    ctx.args = {"depth": "deep"}
    env = run_skill(skill, ctx)
    assert env.header.status == "needs_user"
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    assert 2 <= len(body.user_question.options) <= 4


def test_research_invalid_depth_falls_back_to_normal(state_dir: Path) -> None:
    skill = ResearchSkill()
    ctx = _ctx()
    ctx.args = {"depth": "wat"}
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert len(body.questions) == 2  # normal


def test_research_emits_one_event_per_step(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Algorithm steps that emit: resolve_scope, start_brief,
    # define_questions, synthesize_options, peer_review, recommend → 6
    assert len(lines) == 6, f"expected 6 events, got {len(lines)}: {lines}"
    # The footer's persisted_store_records should mirror the line count.
    assert len(env.footer.persisted_store_records) == 6


def test_research_probe_blocked_when_hard_tool_missing(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the probe to report a blocked outcome and verify the engine
    short-circuits to ``status=blocked`` with non-empty repair commands."""
    from eawf.workflow.skills import research as research_module

    def _blocked_probe(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["brew install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="git absent")],
        )

    monkeypatch.setattr(research_module.ResearchSkill, "probe", _blocked_probe)
    env = run_skill(research_module.ResearchSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["brew install git"]
    # No events written when probe blocks.
    events_path = state_dir / "store" / "event.jsonl"
    assert not events_path.exists() or events_path.read_text(encoding="utf-8") == ""


def test_research_default_next_actions_present(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    assert "eawf prep" in env.footer.next_valid_actions


def test_research_default_does_not_persist_research_brief(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert body.persisted_brief is None
    assert not (state_dir / "store" / "research.jsonl").exists()


def test_research_final_persists_research_brief(state_dir: Path) -> None:
    skill = ResearchSkill()
    ctx = _ctx()
    ctx.args = {"topic": "demo topic", "final": True, "blitz": False}
    env = run_skill(skill, ctx)
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert body.persisted_brief == f"urn:eawf:v1:store:research/{body.brief_id}"
    records = (state_dir / "store" / "research.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = orjson.loads(records[0])
    assert record["id"] == body.brief_id
    assert record["kind"] == "research"
    assert record["payload"]["topic"] == "demo topic"
    assert body.persisted_brief in env.footer.persisted_store_records


def test_research_auto_chains_blitz_for_residual_unknowns(state_dir: Path) -> None:
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    assert "eawf skill run /blitz" in env.footer.next_valid_actions


def test_research_can_disable_blitz_auto_chain(state_dir: Path) -> None:
    skill = ResearchSkill()
    ctx = _ctx()
    ctx.args = {"blitz": False}
    env = run_skill(skill, ctx)
    assert "eawf skill run /blitz" not in env.footer.next_valid_actions


def test_research_propagates_blitz_depth_exhaustion(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "0")
    skill = ResearchSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands


def test_research_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/research")
    assert cls is ResearchSkill
