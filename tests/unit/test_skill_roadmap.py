"""Unit tests for :class:`eawf.workflow.skills.roadmap.RoadmapSkill`.

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

import json
from pathlib import Path
from typing import Any, cast

import pytest

from eawf.workflow.skills.bodies.roadmap import RoadmapBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.roadmap import RoadmapSkill


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
    from eawf.workflow.skills import registry

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


# --- P27-I03-W15: apply prefills the wave DAG ---------------------------------

_TS = "2026-05-20T00:00:00Z"


def _wave(
    wave_id: str,
    iter_id: str,
    status: str,
    *,
    deps: list[str] | None = None,
    file_scopes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": f"feat: wave {wave_id}",
        "status": status,
        "deps": deps or [],
        "file_scopes": file_scopes or [],
        "opened_at": _TS,
    }


def _write_state(
    state_dir: Path,
    *,
    phase_id: str = "P03",
    iter_id: str = "P03-I02",
    phase_status: str = "planned",
    waves: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a minimal valid ``state.json`` with one phase / iter / waves.

    Mirrors the prep-skill test fixture so the roadmap apply-prefill path
    reads a real PENDING wave set from disk.
    """
    if waves is None:
        waves = [
            _wave(f"{iter_id}-W01", iter_id, "pending", file_scopes=["src/a.py"]),
            _wave(
                f"{iter_id}-W02",
                iter_id,
                "pending",
                deps=[f"{iter_id}-W01"],
                file_scopes=["src/b.py"],
            ),
        ]
    state = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:CORE",
        "updated_at": _TS,
        "project": {
            "code": "CORE",
            "slug": "core",
            "title": "Core",
            "description": "",
            "domains": ["core"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:CORE",
        },
        "current": {
            "project_code": "CORE",
            "phase_id": phase_id,
            "iter_id": iter_id,
        },
        "workspace": None,
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "CORE",
                "title": f"Phase {phase_id}",
                "status": phase_status,
                "iter_ids": [iter_id],
                "opened_at": _TS,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "Iter",
                "status": "planned",
                "wave_ids": [w["id"] for w in waves],
                "opened_at": _TS,
            }
        },
        "waves": {w["id"]: w for w in waves},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def test_roadmap_apply_prefills_wave_dag_from_state(state_dir: Path) -> None:
    """A phase with PENDING waves prefills candidates with their deps."""
    _write_state(state_dir)
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P03"}))
    assert env.header.status == "ok", env.body
    body = RoadmapBody.model_validate(cast(dict, env.body))
    # Candidates mirror the two PENDING waves (not the placeholder R-NN ids).
    assert [c.item_id for c in body.candidates] == ["P03-I02-W01", "P03-I02-W02"]
    assert body.chosen_order == ["P03-I02-W01", "P03-I02-W02"]
    # The dep summary surfaces W02's dep on W01.
    w02 = next(c for c in body.candidates if c.item_id == "P03-I02-W02")
    assert "P03-I02-W01" in w02.rationale


def test_roadmap_apply_resolves_phase_from_current_pointer(state_dir: Path) -> None:
    """With no explicit phase arg the current.phase_id drives the prefill."""
    _write_state(state_dir)
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx())
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert [c.item_id for c in body.candidates] == ["P03-I02-W01", "P03-I02-W02"]


def test_roadmap_apply_ask_gates_with_apply_question(state_dir: Path) -> None:
    """approval=ask on a prefilled DAG returns the apply AUQ (needs_user)."""
    _write_state(state_dir)
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P03", "approval": "ask"}))
    assert env.header.status == "needs_user"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    labels = [opt.label for opt in body.user_question.options]
    assert labels == ["approve", "revise", "cancel"]
    assert "P03" in body.user_question.question


def test_roadmap_apply_only_pending_waves_prefilled(state_dir: Path) -> None:
    """CLOSED / CLAIMED waves are excluded from the prefilled DAG."""
    _write_state(
        state_dir,
        waves=[
            _wave("P03-I02-W01", "P03-I02", "closed"),
            _wave("P03-I02-W02", "P03-I02", "pending"),
        ],
    )
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P03"}))
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert [c.item_id for c in body.candidates] == ["P03-I02-W02"]


def test_roadmap_apply_no_pending_waves_falls_back_to_placeholders(state_dir: Path) -> None:
    """A phase with zero PENDING waves degrades to the propose placeholders."""
    _write_state(state_dir, waves=[_wave("P03-I02-W01", "P03-I02", "closed")])
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P03"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    # No PENDING wave → placeholder candidates (medium horizon → 3).
    assert len(body.candidates) == 3
    assert all(c.item_id.startswith("R-") for c in body.candidates)


def test_roadmap_apply_unknown_phase_falls_back_to_placeholders(state_dir: Path) -> None:
    """An explicit phase absent from state degrades to placeholders, not crash."""
    _write_state(state_dir)
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P99"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert all(c.item_id.startswith("R-") for c in body.candidates)


def test_roadmap_apply_malformed_state_falls_back_to_placeholders(state_dir: Path) -> None:
    """Unparseable state.json degrades to placeholders rather than raising."""
    (state_dir / "state.json").write_text("{not json", encoding="utf-8")
    skill = RoadmapSkill()
    env = run_skill(skill, _ctx({"phase": "P03"}))
    assert env.header.status == "ok"
    body = RoadmapBody.model_validate(cast(dict, env.body))
    assert all(c.item_id.startswith("R-") for c in body.candidates)


def test_roadmap_apply_emits_apply_event_when_prefilled(state_dir: Path) -> None:
    """The apply step records an applied event naming the wave count."""
    _write_state(state_dir)
    skill = RoadmapSkill()
    run_skill(skill, _ctx({"phase": "P03"}))
    events_path = state_dir / "store" / "event.jsonl"
    lines = [json.loads(line) for line in events_path.read_text().splitlines()]
    apply_events = [e for e in lines if e["payload"]["event_type"] == "roadmap.apply"]
    assert apply_events
    assert apply_events[-1]["payload"]["applied"] is True
    assert apply_events[-1]["payload"]["wave_count"] == 2
