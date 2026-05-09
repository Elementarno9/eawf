"""Unit tests for :class:`eawf.skills.polish.PolishSkill`.

Pin the Phase 4 W02 acceptance contract for ``/polish``:

- Happy path → ``status=ok`` with a populated :class:`PolishBody`.
- Probe-blocked path → ``status=blocked`` + repair commands.
- Default ``report_only=True`` (non-destructive).
- ``-y`` flag (``y=True``) toggles ``report_only`` off.
- Body schema (``groups``, ``memory_pass``, ``report_only``) populated.
- Each step emits one ``EVENT`` row.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.render.envelope import EnvelopeWarning
from eawf.skills.bodies.polish import PolishBody
from eawf.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.skills.polish import PolishSkill


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


def test_polish_default_report_only_true(state_dir: Path) -> None:
    skill = PolishSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = PolishBody.model_validate(cast(dict, env.body))
    assert body.report_only is True


def test_polish_y_flag_disables_report_only(state_dir: Path) -> None:
    """``-y`` toggles report_only off (the v0.1 stub still doesn't
    apply, but the body field reflects the intent)."""
    skill = PolishSkill()
    ctx = _ctx()
    ctx.args = {"y": True}
    env = run_skill(skill, ctx)
    body = PolishBody.model_validate(cast(dict, env.body))
    assert body.report_only is False


def test_polish_explicit_report_only_false(state_dir: Path) -> None:
    skill = PolishSkill()
    ctx = _ctx()
    ctx.args = {"report_only": False}
    env = run_skill(skill, ctx)
    body = PolishBody.model_validate(cast(dict, env.body))
    assert body.report_only is False


def test_polish_groups_populated(state_dir: Path) -> None:
    skill = PolishSkill()
    env = run_skill(skill, _ctx())
    body = PolishBody.model_validate(cast(dict, env.body))
    assert len(body.groups) >= 1
    # Each group has at least one item.
    for g in body.groups:
        assert len(g.items) >= 1


def test_polish_memory_pass_present(state_dir: Path) -> None:
    skill = PolishSkill()
    env = run_skill(skill, _ctx())
    body = PolishBody.model_validate(cast(dict, env.body))
    assert body.memory_pass is not None
    assert body.memory_pass.promotions == 0


def test_polish_emits_one_event_per_step(state_dir: Path) -> None:
    skill = PolishSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps: snapshot, fanout, find_inconsistencies, memory_pass,
    # apply_gate, write_report → 6.
    assert len(lines) == 6
    assert len(env.footer.persisted_store_records) == 6


def test_polish_string_truthy_y_flag(state_dir: Path) -> None:
    skill = PolishSkill()
    ctx = _ctx()
    ctx.args = {"y": "yes"}
    env = run_skill(skill, ctx)
    body = PolishBody.model_validate(cast(dict, env.body))
    assert body.report_only is False


def test_polish_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.skills import polish as polish_module

    def _blocked(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="x")],
        )

    monkeypatch.setattr(polish_module.PolishSkill, "probe", _blocked)
    env = run_skill(polish_module.PolishSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["install git"]


def test_polish_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/polish")
    assert cls is PolishSkill
