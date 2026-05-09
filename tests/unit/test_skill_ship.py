"""Unit tests for :class:`eawf.skills.ship.ShipSkill`.

Pin the Phase 4 W02 acceptance contract for ``/ship``:

- Happy path → ``status=ok`` with a populated :class:`ShipBody`.
- Probe-blocked path → ``status=blocked`` + repair commands.
- ``--commit`` flag toggles ``body.commit_groups`` population.
- ``--push`` flag toggles ``body.push`` population.
- ``--pr <action>`` flag populates ``body.pr.action``.
- Body's :class:`ShipPrGates` always sets ``state_valid``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.render.envelope import EnvelopeWarning
from eawf.skills.bodies.ship import ShipBody
from eawf.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.skills.ship import ShipSkill


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


def test_ship_default_no_commit_no_push_no_pr(state_dir: Path) -> None:
    """Default args → no commit groups, no push, no PR."""
    skill = ShipSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.commit_groups == []
    assert body.push is None
    assert body.pr is None


def test_ship_commit_flag_populates_commit_groups(state_dir: Path) -> None:
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"commit": True}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert len(body.commit_groups) == 1
    assert body.commit_groups[0].message


def test_ship_push_flag_populates_push(state_dir: Path) -> None:
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"push": True}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.push is not None
    assert body.push.status == "planned"
    assert body.push.ref == "HEAD"


def test_ship_pr_flag_populates_pr(state_dir: Path) -> None:
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"pr": "open"}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.pr is not None
    assert body.pr.action == "open"
    assert body.pr.gates.state_valid is True


def test_ship_pr_action_normalised_to_open_for_truthy(state_dir: Path) -> None:
    """`--pr true` (or `1`) defaults to ``open``."""
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"pr": True}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.pr is not None
    assert body.pr.action == "open"


def test_ship_pr_unknown_action_drops_pr(state_dir: Path) -> None:
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"pr": "bogus-action"}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.pr is None


def test_ship_all_flags_combined(state_dir: Path) -> None:
    """``--commit`` + ``--push`` + ``--pr ready`` populates every block."""
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"commit": True, "push": True, "pr": "ready"}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert len(body.commit_groups) == 1
    assert body.push is not None
    assert body.pr is not None
    assert body.pr.action == "ready"


def test_ship_string_truthy_flag_accepted(state_dir: Path) -> None:
    """JSON-piped ``"yes"``/``"true"`` should toggle the flag on."""
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"commit": "yes", "push": "true"}
    env = run_skill(skill, ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert len(body.commit_groups) == 1
    assert body.push is not None


def test_ship_emits_one_event_per_step(state_dir: Path) -> None:
    skill = ShipSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps: audit_gate, inspect_git, memory_review, build_pending,
    # commit, push, pr, record → 8.
    assert len(lines) == 8
    assert len(env.footer.persisted_store_records) == 8


def test_ship_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.skills import ship as ship_module

    def _blocked(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="x")],
        )

    monkeypatch.setattr(ship_module.ShipSkill, "probe", _blocked)
    env = run_skill(ship_module.ShipSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["install git"]


def test_ship_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/ship")
    assert cls is ShipSkill
