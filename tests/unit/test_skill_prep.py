"""Unit tests for :class:`eawf.skills.prep.PrepSkill`.

Pin the Phase 4 W02 acceptance contract for ``/prep``:

- Happy path → ``status=ok`` with a populated :class:`PrepBody`.
- Probe-blocked path → ``status=blocked`` with repair commands.
- ``approval=ask`` → ``status=needs_user`` with a typed user_question.
- Body schema (``iter_id``, ``objective``, ``dag``, ``waves``,
  ``acceptance``) populated.
- Each algorithm step writes one ``EVENT`` row.
- ``-i`` (fix mode) toggles the objective text.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.render.envelope import EnvelopeWarning
from eawf.skills.bodies.prep import PrepBody
from eawf.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.skills.prep import PrepSkill


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


def test_prep_happy_path_status_ok(state_dir: Path) -> None:
    skill = PrepSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    assert env.header.skill == "/prep"


def test_prep_body_populated(state_dir: Path) -> None:
    skill = PrepSkill()
    env = run_skill(skill, _ctx())
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.iter_id  # default
    assert len(body.dag) >= 1
    assert len(body.waves) >= 1
    # Every wave's tasks reference the DAG.
    dag_ids = {t.task_id for t in body.dag}
    for wave in body.waves:
        for task in wave.tasks:
            assert task in dag_ids


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
    from eawf.skills import prep as prep_module

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
    skill = PrepSkill()
    ctx = _ctx()
    ctx.args = {"iter_id": "P03-I02"}
    env = run_skill(skill, ctx)
    body = PrepBody.model_validate(cast(dict, env.body))
    assert body.iter_id == "P03-I02"
    # Wave id includes the iter id.
    assert any(w.wave_id.startswith("P03-I02") for w in body.waves)


def test_prep_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/prep")
    assert cls is PrepSkill
