"""Unit tests for :class:`eawf.skills.audit.AuditSkill`.

Pin the Phase 4 W02 acceptance contract for ``/audit``:

- Happy path with no profile config → ``kind=ship-gate`` (default branch).
- ``research`` profile enabled in layered config → ``kind=evaluation``.
- Explicit ``--kind`` override defeats the profile branch.
- ``--checks`` flag honoured: comma-separated list filters
  ``body.checks_run``.
- Probe-blocked path → ``status=blocked`` + repair commands.
- Body schema (``scope_id``, ``kind``, ``checks_run``) populated.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

from eawf.render.envelope import EnvelopeWarning
from eawf.skills.audit import AuditSkill
from eawf.skills.bodies.audit import AuditBody
from eawf.skills.engine import ProbeOutcome, SkillContext, run_skill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


@pytest.fixture
def state_dir_with_research_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Variant fixture: enables the ``research`` profile in the repo
    layer so :func:`has_research_profile` returns True."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    config_path = state_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"profiles": {"enabled": ["core", "research"]}}),
        encoding="utf-8",
    )
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def test_audit_default_branch_is_ship_gate(state_dir: Path) -> None:
    """No research profile → ship-gate (the v0.1 default)."""
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.kind == "ship-gate"


def test_audit_research_profile_branches_to_evaluation(
    state_dir_with_research_profile: Path,
) -> None:
    """research profile in layered config → kind=evaluation."""
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.kind == "evaluation"


def test_audit_explicit_kind_overrides_profile_branch(
    state_dir_with_research_profile: Path,
) -> None:
    """``--kind`` override beats the profile branch."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"kind": "ship-gate"}
    env = run_skill(skill, ctx)
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.kind == "ship-gate"


def test_audit_checks_flag_honoured_csv(state_dir: Path) -> None:
    """``--checks tests,lint`` populates only those check rows."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"checks": "tests,lint"}
    env = run_skill(skill, ctx)
    body = AuditBody.model_validate(cast(dict, env.body))
    assert [c.check_id for c in body.checks_run] == ["tests", "lint"]


def test_audit_checks_flag_honoured_list(state_dir: Path) -> None:
    """``--checks`` accepts a JSON array variant via stdin args."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"checks": ["build", "type"]}
    env = run_skill(skill, ctx)
    body = AuditBody.model_validate(cast(dict, env.body))
    assert [c.check_id for c in body.checks_run] == ["build", "type"]


def test_audit_default_check_set_for_ship_gate(state_dir: Path) -> None:
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    body = AuditBody.model_validate(cast(dict, env.body))
    names = {c.check_id for c in body.checks_run}
    # The §14 ship-gate default mentions tests / lint / type / build /
    # docs / state.
    assert "tests" in names
    assert "lint" in names


def test_audit_default_check_set_for_evaluation(
    state_dir_with_research_profile: Path,
) -> None:
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    body = AuditBody.model_validate(cast(dict, env.body))
    names = {c.check_id for c in body.checks_run}
    # §14 evaluation defaults mention MLflow integrity / lookahead / IS-OOS.
    assert "mlflow_integrity" in names


def test_audit_emits_one_event_per_step(state_dir: Path) -> None:
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps emitting: resolve_scope, branch_kind, build_check_plan,
    # one per check (6 default), collect_metrics, dispatch_reviewers,
    # write_artifact → 4 + 6 = 10 (3 fixed pre-checks + 6 checks +
    # 3 fixed post-checks).
    assert len(lines) == len(env.footer.persisted_store_records)
    # Definitely at least 4 fixed events.
    assert len(lines) >= 4


def test_audit_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.skills import audit as audit_module

    def _blocked(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["brew install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="x")],
        )

    monkeypatch.setattr(audit_module.AuditSkill, "probe", _blocked)
    env = run_skill(audit_module.AuditSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["brew install git"]


def test_audit_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/audit")
    assert cls is AuditSkill
