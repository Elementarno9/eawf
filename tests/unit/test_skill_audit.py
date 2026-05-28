"""Unit tests for :class:`eawf.workflow.skills.audit.AuditSkill`.

Pin the Phase 4 W02 + P27-I02-W14 contract for ``/audit``:

- Happy path with no profile config → ``kind=ship-gate`` (default branch).
- ``research`` profile enabled in layered config → ``kind=evaluation``.
- Explicit ``--kind`` override defeats the profile branch.
- Probe-blocked path → ``status=blocked`` + repair commands.
- Body schema (``scope_id``, ``kind``, ``checks_run``) populated.

W14 makes the gate run real checks (no ``skipped`` by default):

- A seeded behavioural regression → ``/audit`` returns a non-ok status
  with an :class:`AuditFinding` naming the offending criterion.
- A passing fixture → ``/audit`` returns ``ok`` and no check is
  ``skipped``.
- The fresh-context auditor dispatch directive is always emitted.
- A wave with zero checks audits as a no-op (``ok``, empty findings).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml

from eawf.surfaces.render.envelope import EnvelopeWarning
from eawf.workflow.skills.audit import AuditSkill
from eawf.workflow.skills.bodies.audit import AuditBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill


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
    assert body.audit_artifact_urn is not None
    assert body.audit_artifact_urn in env.footer.evidence_refs


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


def test_audit_no_wave_runs_zero_checks_and_stays_ok(state_dir: Path) -> None:
    """No target wave → no criterion checks → ok with empty findings.

    The default profile-name list no longer fabricates ``skipped`` check
    rows; without a wave to read criteria from, the gate audits as a
    no-op rather than emitting fake passes.
    """
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.checks_run == []
    assert body.findings == []


def test_audit_no_wave_skips_auditor_dispatch(state_dir: Path) -> None:
    """No resolvable wave → no auditor dispatch directive (not a malformed one).

    When ``wave_id`` is absent the wave does not resolve, so the skill must
    NOT emit an ``AuditorDispatch`` carrying the phase-scope URN fallback and
    zero criteria — that would render a "spawn an auditor for urn:...:QR/P00
    with 0 criteria" directive. The directive is omitted entirely instead.
    """
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.auditor_dispatch is None


def test_audit_no_check_status_is_skipped_by_default(state_dir: Path) -> None:
    """W14 success criterion: no check returns ``skipped`` by default."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {
        "wave_id": "P00-I01-W01",
        "criterion_checks": [
            {"criterion": "smoke passes", "argv": [sys.executable, "-c", "raise SystemExit(0)"]},
        ],
    }
    env = run_skill(skill, ctx)
    body = AuditBody.model_validate(cast(dict, env.body))
    statuses = {c.status for c in body.checks_run}
    assert "skipped" not in statuses
    assert statuses == {"pass"}


def test_audit_emits_one_event_per_step(state_dir: Path) -> None:
    skill = AuditSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Fixed steps (no target wave → zero criterion checks): resolve_scope,
    # branch_kind, build_check_plan, collect_metrics, dispatch_auditor,
    # write_artifact → 6. One run_check event is appended per criterion
    # check on top.
    assert len(lines) == len(env.footer.persisted_store_records)
    assert len(lines) == 6


def test_audit_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.workflow.skills import audit as audit_module

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
    from eawf.workflow.skills import registry

    cls = registry.lookup("/audit")
    assert cls is AuditSkill


# ---- W14: real checks + fresh-context auditor dispatch ----------------------


def _seed_state_with_wave(
    state_dir: Path,
    *,
    wave_id: str,
    iter_id: str,
    phase_id: str,
    file_scopes: list[str],
    success_criteria: list[str],
) -> None:
    """Write a minimal state.json carrying one wave with criteria + scopes."""
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import ProjectStatus, ScopeKind
    from eawf.kernel.state.models import CurrentPointers, Project, State
    from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave

    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    open_phase(state, phase_id=phase_id, title="Phase")
    open_iter(state, iter_id=iter_id, phase_id=phase_id, title="Iter")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id=iter_id,
        title="Wave under audit",
        file_scopes=file_scopes,
        success_criteria=success_criteria,
        effort_bucket="M",
    )
    (state_dir / "state.json").write_text(
        json.dumps(state.model_dump(mode="json")), encoding="utf-8"
    )


def test_audit_seeded_behavioral_regression_fails_with_offending_criterion(
    state_dir: Path,
) -> None:
    """A failing behavioural smoke check → non-ok status naming the criterion."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {
        "wave_id": "P00-I01-W01",
        "criterion_checks": [
            {
                "criterion": "the changed surface still behaves",
                # SystemExit(1) models a seeded regression in the surface.
                "argv": [sys.executable, "-c", "raise SystemExit(1)"],
            },
        ],
    }
    env = run_skill(skill, ctx)
    assert env.header.status != "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert len(body.findings) == 1
    assert body.findings[0].location == "the changed surface still behaves"
    # The failing check itself is recorded with a fail status (not skipped).
    assert [c.status for c in body.checks_run] == ["fail"]


def test_audit_passing_fixture_returns_ok_no_skipped(state_dir: Path) -> None:
    """A passing behavioural smoke check → ok and no check is skipped."""
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {
        "wave_id": "P00-I01-W01",
        "criterion_checks": [
            {
                "criterion": "the changed surface still behaves",
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            },
        ],
    }
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.findings == []
    assert [c.status for c in body.checks_run] == ["pass"]


def test_audit_emits_fresh_context_auditor_dispatch(state_dir: Path) -> None:
    """The fresh-context auditor dispatch directive is always emitted."""
    _seed_state_with_wave(
        state_dir,
        wave_id="P00-I01-W01",
        iter_id="P00-I01",
        phase_id="P00",
        file_scopes=["src/eawf/"],
        success_criteria=["criterion alpha", "criterion beta"],
    )
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"wave_id": "P00-I01-W01", "diff_base": "origin/main"}
    env = run_skill(skill, ctx)
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.auditor_dispatch is not None
    # Fresh-context mandate: the auditor runs in a fresh session.
    assert body.auditor_dispatch.session_policy == "fresh"
    assert body.auditor_dispatch.wave_id == "P00-I01-W01"
    assert body.auditor_dispatch.diff_base == "origin/main"
    # The auditor's only inputs are the diff base + the wave's criteria.
    assert body.auditor_dispatch.criteria == ["criterion alpha", "criterion beta"]
    # The dispatch is also persisted as an event so it is auditable.
    events = (state_dir / "store" / "event.jsonl").read_text(encoding="utf-8")
    assert "audit.dispatch_auditor" in events


def test_audit_criterion_in_diff_fails_from_wave_state(state_dir: Path) -> None:
    """A criterion whose pattern is absent from its file_scopes → fail.

    Exercises the criterion-vs-diff path driven entirely by the wave
    record in state (no explicit ``criterion_checks`` directive).
    """
    src_dir = state_dir.parent / "src" / "eawf"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
    _seed_state_with_wave(
        state_dir,
        wave_id="P00-I01-W01",
        iter_id="P00-I01",
        phase_id="P00",
        file_scopes=["src/eawf/mod.py"],
        # This literal substring is NOT present in mod.py → unmet.
        success_criteria=["def gamma is implemented"],
    )
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"wave_id": "P00-I01-W01"}
    env = run_skill(skill, ctx)
    assert env.header.status != "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.findings[0].location == "def gamma is implemented"


def test_audit_criterion_in_diff_passes_from_wave_state(state_dir: Path) -> None:
    """A criterion whose literal text appears in its file_scopes → pass."""
    src_dir = state_dir.parent / "src" / "eawf"
    src_dir.mkdir(parents=True, exist_ok=True)
    # The criterion's literal substring is embedded in the source.
    (src_dir / "mod.py").write_text(
        "# implements: alpha guard kept\ndef alpha() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    _seed_state_with_wave(
        state_dir,
        wave_id="P00-I01-W01",
        iter_id="P00-I01",
        phase_id="P00",
        file_scopes=["src/eawf/mod.py"],
        success_criteria=["alpha guard kept"],
    )
    skill = AuditSkill()
    ctx = _ctx()
    ctx.args = {"wave_id": "P00-I01-W01"}
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    body = AuditBody.model_validate(cast(dict, env.body))
    assert body.findings == []
    assert [c.status for c in body.checks_run] == ["pass"]
