"""Unit tests for :class:`eawf.skills.differentiate.DifferentiateSkill`.

Pin the Phase 4 W03 acceptance contract for ``/differentiate``:

- Happy path (default preset=adaptive) → status=ok envelope with a
  populated :class:`DifferentiateBody`.
- ``--preset minimal|adaptive|full`` flag honoured: axes count scales
  with preset.
- ``approval=ask`` short-circuits to status=needs_user with a typed
  :class:`UserQuestion`.
- The skill registers under the ``/differentiate`` slot.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.skills.bodies.differentiate import DifferentiateBody
from eawf.skills.differentiate import DifferentiateSkill
from eawf.skills.engine import SkillContext, run_skill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def _ctx(args: dict[str, object] | None = None) -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args=dict(args or {}),
    )


def test_differentiate_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/differentiate")
    assert cls is DifferentiateSkill


def test_differentiate_happy_path_status_ok(state_dir: Path) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok", env.body
    assert env.header.skill == "/differentiate"
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert len(body.axes) == 3  # adaptive → 3 axes
    assert body.target_scope == "urn:eawf:v1:state:QR/P00"


def test_differentiate_minimal_preset_scales_axes(state_dir: Path) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx({"preset": "minimal"}))
    assert env.header.status == "ok"
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert len(body.axes) == 1


def test_differentiate_full_preset_scales_axes(state_dir: Path) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx({"preset": "full"}))
    assert env.header.status == "ok"
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert len(body.axes) == 5


def test_differentiate_invalid_preset_falls_back_to_adaptive(
    state_dir: Path,
) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx({"preset": "wat"}))
    assert env.header.status == "ok"
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert len(body.axes) == 3


def test_differentiate_approval_ask_returns_needs_user(state_dir: Path) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx({"approval": "ask"}))
    assert env.header.status == "needs_user"
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    assert 2 <= len(body.user_question.options) <= 4


def test_differentiate_emits_events_for_steps(state_dir: Path) -> None:
    """Skill writes one EVENT row per algorithm step.

    Happy path emits: resolve_scope, inspect_agents, propose_agent_set,
    draft_agents, render → 5.
    """
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5, f"expected 5 events, got {len(lines)}"
    assert len(env.footer.persisted_store_records) == 5


def test_differentiate_conclusions_carry_preset(state_dir: Path) -> None:
    skill = DifferentiateSkill()
    env = run_skill(skill, _ctx({"preset": "full"}))
    body = DifferentiateBody.model_validate(cast(dict, env.body))
    assert body.conclusions
    assert "full" in body.conclusions[0]
