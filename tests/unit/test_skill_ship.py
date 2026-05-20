"""Unit tests for :class:`eawf.skills.ship.ShipSkill`.

Pin the Phase 4 W02 acceptance contract for ``/ship``:

- Happy path → ``status=ok`` with a populated :class:`ShipBody`.
- Probe-blocked path → ``status=blocked`` + repair commands.
- ``--commit`` flag toggles ``body.commit_groups`` population.
- ``--push`` flag toggles ``body.push`` population.
- ``--pr <action>`` flag populates ``body.pr.action``.
- Body's :class:`ShipPrGates` always sets ``state_valid``.

Plus the C04a ship-pipeline gates (P26-W09):

- Audit-verdict gate: a recorded ``major`` / missing verdict for the
  shipped phase blocks ship; ``pass`` / ``minor`` clears it.
- Co-author trailer is appended to each commit-group message.
- Merge-method gate: rebase / merge clear; ``squash`` is rejected unless
  ``vcs.squash_allowed`` is set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from eawf.config import layered
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
    # Isolate the global config layer so the merge-method / co-author gates
    # read built-in defaults (+ this repo's .ea/config.yaml only), not the
    # host machine's ~/.eawf config.
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "no-global.yaml")
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def _write_state_with_audit(
    state_dir: Path,
    *,
    audit_scope: str,
    verdict: str | None,
    kind: str = "ship-gate",
) -> None:
    """Write a minimal valid ``state.json`` carrying one audit row.

    The audit is scoped to *audit_scope* (a bare phase id such as ``P00``)
    with the given *verdict* (``pass`` / ``minor`` / ``major`` / ``None``)
    so the ship audit-verdict gate has a row to consult.
    """
    created = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": created,
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": audit_scope,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "audits": {
            "A01-P00": {
                "id": "A01-P00",
                "scope_id": audit_scope,
                "kind": kind,
                "status": "complete",
                "created_at": created,
                "verdict": verdict,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    (state_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_repo_config(state_dir: Path, *, vcs_overrides: dict) -> None:
    """Write a ``.ea/config.yaml`` repo overlay carrying *vcs_overrides*.

    Only the ``vcs`` leaves under test are written; the layered merge fills
    every other leaf from the built-in defaults.
    """
    import yaml

    (state_dir / "config.yaml").write_text(yaml.safe_dump({"vcs": vcs_overrides}), encoding="utf-8")


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


def test_ship_blocks_invalid_artifact_path(state_dir: Path, tmp_path: Path) -> None:
    artifact = tmp_path / "bad.md"
    artifact.write_text("# Bad\n\nNo chassis.\n", encoding="utf-8")
    skill = ShipSkill()
    ctx = _ctx()
    ctx.args = {"artifact_paths": [str(artifact)]}
    env = run_skill(skill, ctx)
    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["fix artifact validation errors and rerun /ship"]


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


# ---- C04a: audit-verdict gate ----------------------------------------------


def test_ship_audit_gate_blocks_on_major_verdict(state_dir: Path) -> None:
    """A recorded ``major`` verdict for the shipped phase blocks ship."""
    _write_state_with_audit(state_dir, audit_scope="P00", verdict="major")
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["/audit P00 --kind ship-gate"]


def test_ship_audit_gate_blocks_on_missing_verdict(state_dir: Path) -> None:
    """An audit row with no verdict yet (``None``) blocks ship."""
    _write_state_with_audit(state_dir, audit_scope="P00", verdict=None)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["/audit P00 --kind ship-gate"]


def test_ship_audit_gate_passes_on_pass_verdict(state_dir: Path) -> None:
    """A ``pass`` verdict clears the gate → ``status=ok``."""
    _write_state_with_audit(state_dir, audit_scope="P00", verdict="pass")
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_audit_gate_passes_on_minor_verdict(state_dir: Path) -> None:
    """A ``minor`` verdict (pass-with-followups analogue) clears the gate."""
    _write_state_with_audit(state_dir, audit_scope="P00", verdict="minor")
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_audit_gate_degrades_open_when_no_state(state_dir: Path) -> None:
    """No state file → no verdict to gate against → degrade open (status=ok)."""
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_audit_gate_ignores_audit_for_other_phase(state_dir: Path) -> None:
    """An audit scoped to a different phase does not gate this ship."""
    _write_state_with_audit(state_dir, audit_scope="P99", verdict="major")
    env = run_skill(ShipSkill(), _ctx())
    # P00 has no audit row → gate degrades open even though P99 failed.
    assert env.header.status == "ok"


# ---- C04a: co-author trailer ------------------------------------------------


def test_ship_commit_group_message_carries_coauthor_trailer(state_dir: Path) -> None:
    """The default runtime trailer is appended to each commit-group message."""
    ctx = _ctx()
    ctx.args = {"commit": True}
    env = run_skill(ShipSkill(), ctx)
    body = ShipBody.model_validate(cast(dict, env.body))
    assert len(body.commit_groups) == 1
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in body.commit_groups[0].message


def test_ship_no_commit_group_when_commit_flag_absent(state_dir: Path) -> None:
    """Trailer wiring does not synthesise a commit group without ``--commit``."""
    env = run_skill(ShipSkill(), _ctx())
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.commit_groups == []


# ---- C04a: merge-method gate ------------------------------------------------


def test_ship_merge_method_default_merge_allowed(state_dir: Path) -> None:
    """The built-in default (``merge``) clears the merge-method gate."""
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_merge_method_rebase_allowed(state_dir: Path) -> None:
    """An explicit ``rebase`` method clears the gate."""
    _write_repo_config(state_dir, vcs_overrides={"pr_merge_method": "rebase"})
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_merge_method_squash_rejected_when_not_allowed(state_dir: Path) -> None:
    """``squash`` is rejected unless ``vcs.squash_allowed`` is set."""
    _write_repo_config(
        state_dir, vcs_overrides={"pr_merge_method": "squash", "squash_allowed": False}
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    assert env.footer.repair_commands == [
        "set vcs.pr_merge_method to rebase (or enable vcs.squash_allowed)"
    ]


def test_ship_merge_method_squash_allowed_when_opted_in(state_dir: Path) -> None:
    """``squash`` clears the gate when ``vcs.squash_allowed`` is true."""
    _write_repo_config(
        state_dir, vcs_overrides={"pr_merge_method": "squash", "squash_allowed": True}
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
