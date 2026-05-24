"""Unit tests for :class:`eawf.workflow.skills.prep.PrepSkill`.

Pin the Phase 4 W02 acceptance contract for ``/prep`` plus the P26-W08
lifecycle gaps:

- Happy path → ``status=ok`` with a populated :class:`PrepBody`.
- Probe-blocked path → ``status=blocked`` with repair commands.
- ``approval=ask`` → ``status=needs_user`` with a typed user_question.
- Real DAG render → ``body.dag`` / ``body.waves`` projected from the
  target phase's PENDING waves in ``state.json`` (no synthetic ``T01``).
- Idempotency → ``/prep`` on an already-ACTIVE phase is a ``no_op``.
- Closed-phase block → ``/prep`` on a CLOSED phase is ``blocked`` with a
  ``eawf phase reopen`` repair command and no ``prep.build_dag`` event.
- Each algorithm step writes one ``EVENT`` row.
- ``-i`` (fix mode) toggles the objective text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from eawf.render.envelope import EnvelopeWarning
from eawf.workflow.skills.bodies.prep import PrepBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.workflow.skills.prep import PrepSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


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
        "title": f"Wave {wave_id}",
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

    The waves list defaults to a single PENDING wave under ``iter_id`` so
    the real-DAG render path produces one task.
    """
    if waves is None:
        waves = [_wave(f"{iter_id}-W01", iter_id, "pending", file_scopes=["src/a.py"])]
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


def _read_events(state_dir: Path) -> list[dict[str, Any]]:
    events_path = state_dir / "store" / "event.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]


def test_prep_happy_path_status_ok(state_dir: Path) -> None:
    skill = PrepSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    assert env.header.skill == "/prep"


def test_prep_body_populated(state_dir: Path) -> None:
    _write_state(state_dir)
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.iter_id == "P03-I02"
    assert len(body.dag) >= 1
    assert len(body.waves) >= 1
    # Every wave's tasks reference the DAG.
    dag_ids = {t.task_id for t in body.dag}
    for wave in body.waves:
        for task in wave.tasks:
            assert task in dag_ids


def test_prep_dag_projects_pending_waves_from_state(state_dir: Path) -> None:
    _write_state(
        state_dir,
        waves=[
            _wave("P03-I02-W01", "P03-I02", "pending", file_scopes=["src/a.py"]),
            _wave("P03-I02-W02", "P03-I02", "pending", deps=["P03-I02-W01"]),
            _wave("P03-I02-W03", "P03-I02", "closed"),
        ],
    )
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    body = PrepBody.model_validate(cast(dict, env.body))
    task_ids = {t.task_id for t in body.dag}
    # PENDING waves projected by canonical wave id; CLOSED wave excluded.
    assert task_ids == {"P03-I02-W01", "P03-I02-W02"}
    # No synthetic placeholder task id.
    assert "T01" not in task_ids
    # Wave record fields carry through onto the DAG task.
    by_id = {t.task_id: t for t in body.dag}
    assert by_id["P03-I02-W01"].file_scope == ["src/a.py"]
    assert by_id["P03-I02-W02"].deps == ["P03-I02-W01"]


def test_prep_noop_on_active_phase(state_dir: Path) -> None:
    _write_state(state_dir, phase_status="active")
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.no_op is True
    # No redundant build_dag event on the no-op path.
    event_types = {e["payload"]["event_type"] for e in _read_events(state_dir)}
    assert "prep.build_dag" not in event_types
    assert "prep.noop_already_active" in event_types


def test_prep_blocked_on_closed_phase(state_dir: Path) -> None:
    _write_state(state_dir, phase_status="closed")
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["eawf phase reopen P03"]
    # No build_dag event on the blocked path.
    event_types = {e["payload"]["event_type"] for e in _read_events(state_dir)}
    assert "prep.build_dag" not in event_types
    assert "prep.blocked_closed_phase" in event_types


def test_prep_approval_ask_returns_needs_user(state_dir: Path) -> None:
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"approval": "ask"}
    env = run_skill(skill, ctx)
    assert env.header.status == "needs_user"
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.approval_required is True
    assert body.user_question is not None
    assert 2 <= len(body.user_question.options) <= 4


def test_prep_fix_mode_alters_objective(state_dir: Path) -> None:
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"fix": True}
    env = run_skill(skill, ctx)
    body = PrepBody.model_validate(cast(dict, env.body))
    assert "fix-list" in body.objective.lower() or "audit" in body.objective.lower()


def test_prep_emits_one_event_per_step(state_dir: Path) -> None:
    skill = PrepSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps emitting events: resolve_mode, load_state, define_objective,
    # build_dag, partition_waves, estimate, allocate_ids, write_plan → 8.
    assert len(lines) == 8
    assert len(env.footer.persisted_store_records) == 8


def test_prep_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.workflow.skills import prep as prep_module

    def _blocked(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"uv": "missing"},
            repair_commands=["curl -LsSf https://astral.sh/uv/install.sh | sh"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="uv absent")],
        )

    monkeypatch.setattr(prep_module.PrepSkill, "probe", _blocked)
    env = run_skill(prep_module.PrepSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["curl -LsSf https://astral.sh/uv/install.sh | sh"]


def test_prep_explicit_iter_id_honoured(state_dir: Path) -> None:
    _write_state(state_dir)
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.iter_id == "P03-I02"
    # Wave grouping id is the iter id.
    assert any(w.wave_id.startswith("P03-I02") for w in body.waves)


def test_prep_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/prep")
    assert cls is PrepSkill
