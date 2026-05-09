"""Unit tests for :class:`eawf.skills.roadmap.RoadmapSkill`.

Pin the Phase 4 W03 acceptance contract for ``/roadmap``:

- Happy path (default horizon=medium) → status=ok envelope with a
  populated :class:`RoadmapBody`.
- ``--horizon short|medium|long`` flag honoured: candidate slot count
  scales with horizon.
- ``approval=ask`` short-circuits to status=needs_user with a typed
  :class:`UserQuestion` populated.
- ``revise=True`` prepends a stale-marker entry to ``chosen_order``.
- The skill registers under the ``/roadmap`` slot.
- Each algorithm step writes one ``EVENT`` row.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.skills.bodies.roadmap import RoadmapBody
from eawf.skills.engine import SkillContext, run_skill
from eawf.skills.roadmap import RoadmapSkill


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


def test_roadmap_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/roadmap")
    assert cls is RoadmapSkill


def test_roadmap_happy_path_status_ok(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok", env.body
    assert env.header.skill == "/roadmap"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert body.horizon == "medium"
    assert len(body.candidates) == 3  # medium → 3 slots
    assert body.chosen_order == [c.item_id for c in body.candidates]


def test_roadmap_short_horizon_scales_candidates(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"horizon": "short"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert len(body.candidates) == 1


def test_roadmap_long_horizon_scales_candidates(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"horizon": "long"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert len(body.candidates) == 5


def test_roadmap_invalid_horizon_falls_back_to_medium(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"horizon": "wat"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert body.horizon == "medium"
    assert len(body.candidates) == 3


def test_roadmap_approval_ask_returns_needs_user(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"approval": "ask"}))
    assert env.header.status == "needs_user"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    assert 2 <= len(body.user_question.options) <= 4


def test_roadmap_revise_marker_in_chosen_order(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"revise": True}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert body.chosen_order[0].startswith("REVISE:")


def test_roadmap_emits_one_event_per_step(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps emit: resolve_scope, load_state, research_context, propose,
    # extend_or_revise, apply → 6.
    assert len(lines) == 6, f"expected 6 events, got {len(lines)}"
    assert len(env.footer.persisted_store_records) == 6


def test_roadmap_default_next_actions_present(state_dir: Path) -> None:
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx())
    assert "eawf prep" in env.footer.next_valid_actions
