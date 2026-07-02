"""Unit tests for :class:`eawf.workflow.skills.ship.ShipSkill`.

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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from eawf.kernel.config import layered
from eawf.surfaces.render.envelope import EnvelopeWarning
from eawf.workflow.skills.bodies.ship import ShipBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.workflow.skills.ship import ShipSkill, validate_release_subject

if TYPE_CHECKING:
    from eawf.workflow.skills.ship import _GateResult


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
    include_phase: bool = False,
    phase_id: str | None = None,
    phase_structure: str = "closed",
) -> None:
    """Write a minimal valid ``state.json`` carrying one audit row.

    The audit is scoped to *audit_scope* (a bare phase id such as ``P00``)
    with the given *verdict* (``pass`` / ``minor`` / ``major`` / ``None``)
    so the ship audit-verdict gate has a row to consult.
    """
    created = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    phase_key = phase_id or audit_scope
    iter_id = f"{phase_key}-I01"
    closed_wave_1 = f"{iter_id}-W01"
    closed_wave_2 = f"{iter_id}-W02"
    open_wave = f"{iter_id}-W03"
    iter_status = "active" if phase_structure == "open_iter" else "closed"
    wave_ids = [closed_wave_1, closed_wave_2]
    waves = {
        closed_wave_1: {
            "id": closed_wave_1,
            "iter_id": iter_id,
            "title": "closed wave one",
            "description": None,
            "status": "closed",
            "deps": [],
            "blocks": [],
            "file_scopes": ["src/"],
            "success_criteria": [],
            "agent_role": None,
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": "ok",
            "commit": None,
            "opened_at": created,
            "closed_at": created,
            "sessions": {},
            "runtime_preference": None,
            "dispatch_history": [],
            "intent": None,
        },
        closed_wave_2: {
            "id": closed_wave_2,
            "iter_id": iter_id,
            "title": "closed wave two",
            "description": None,
            "status": "closed",
            "deps": [],
            "blocks": [],
            "file_scopes": ["src/"],
            "success_criteria": [],
            "agent_role": None,
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": "ok",
            "commit": None,
            "opened_at": created,
            "closed_at": created,
            "sessions": {},
            "runtime_preference": None,
            "dispatch_history": [],
            "intent": None,
        },
    }
    if phase_structure == "open_wave":
        wave_ids.append(open_wave)
        waves[open_wave] = {
            "id": open_wave,
            "iter_id": iter_id,
            "title": "open wave",
            "description": None,
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": ["src/"],
            "success_criteria": [],
            "agent_role": None,
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "commit": None,
            "opened_at": created,
            "closed_at": None,
            "sessions": {},
            "runtime_preference": None,
            "dispatch_history": [],
            "intent": None,
        }
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
            "track_id": None,
            "phase_id": audit_scope,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            phase_key: {
                "id": phase_key,
                "scope_id": "QR",
                "track_id": None,
                "title": "Phase",
                "description": None,
                "status": "active",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "depends_on": [],
                "source_brief_ids": [],
                "opened_at": created,
                "closed_at": None,
                "audit_id": None,
                "intent": None,
            }
        }
        if include_phase
        else {},
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_key,
                "title": "iter",
                "description": None,
                "status": iter_status,
                "wave_ids": wave_ids,
                "estimate_id": None,
                "audit_id": "AUD-ITER",
                "opened_at": created,
                "closed_at": None if iter_status == "active" else created,
                "intent": None,
            }
        }
        if include_phase
        else {},
        "waves": waves if include_phase else {},
        "audits": {
            "A01-P00": {
                "id": "A01-P00",
                "scope_id": audit_scope,
                "kind": kind,
                "status": "complete",
                "created_at": created,
                "verdict": verdict,
                "report_artifact_id": None,
                "check_results": [
                    {"name": "tests", "passed": True, "details": "focused tests passed"}
                ],
                "integrity_results": [],
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
    from eawf.workflow.skills import ship as ship_module

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
    from eawf.workflow.skills import registry

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
    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["/audit P00 --kind ship-gate"]


def test_ship_phase_close_readiness_requires_matching_audit(state_dir: Path) -> None:
    """When the phase exists, ship uses phase-close readiness for audit evidence."""
    _write_state_with_audit(
        state_dir,
        audit_scope="P99",
        verdict="pass",
        include_phase=True,
        phase_id="P00",
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert "close audit required" in str(body.rollback_notes)


def test_ship_phase_close_readiness_accepts_pass_audit(state_dir: Path) -> None:
    """A complete pass ship-gate audit clears ship's phase-close readiness gate."""
    _write_state_with_audit(
        state_dir,
        audit_scope="P00",
        verdict="pass",
        include_phase=True,
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"


def test_ship_phase_close_readiness_blocks_open_iter(state_dir: Path) -> None:
    """Ship cannot pass while the phase still has an open iter."""
    _write_state_with_audit(
        state_dir,
        audit_scope="P00",
        verdict="pass",
        include_phase=True,
        phase_structure="open_iter",
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert "open iters: P00-I01" in str(body.rollback_notes)


def test_ship_phase_close_readiness_blocks_open_wave(state_dir: Path) -> None:
    """Ship cannot pass while the phase still has an open wave."""
    _write_state_with_audit(
        state_dir,
        audit_scope="P00",
        verdict="pass",
        include_phase=True,
        phase_structure="open_wave",
    )
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert "open waves: P00-I01-W03" in str(body.rollback_notes)


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


# ---- P27-I02-W15: gauntlet gate ---------------------------------------------


def _write_acceptance_config(state_dir: Path, *, acceptance_overrides: dict) -> None:
    """Write a ``.ea/config.yaml`` repo overlay carrying *acceptance_overrides*.

    Only the ``acceptance`` leaves under test are written; the layered merge
    fills every other leaf from the built-in defaults.
    """
    import yaml

    (state_dir / "config.yaml").write_text(
        yaml.safe_dump({"acceptance": acceptance_overrides}), encoding="utf-8"
    )


def _stub_gate_runner(
    *, fail: set[str] | None = None
) -> tuple[list[str], Callable[[str, str, Path], _GateResult]]:
    """Return ``(calls, runner)`` where *runner* records each gate it runs.

    The returned runner mimics ``_run_gate_command`` without spawning a
    subprocess: every gate "passes" (returncode 0) unless its name is in
    *fail*, in which case it returns a red :class:`_GateResult`. ``calls``
    accumulates the gate names in execution order so tests can assert which
    gates ran and in what order.
    """
    from eawf.workflow.skills.ship import _GateResult

    failing = fail or set()
    calls: list[str] = []

    def _runner(name: str, command: str, cwd: Path) -> _GateResult:
        calls.append(name)
        is_red = name in failing
        return _GateResult(
            name=name,
            command=command,
            passed=not is_red,
            returncode=1 if is_red else 0,
            output=f"{name} failed\n" if is_red else "",
        )

    return calls, _runner


def test_run_gauntlet_runs_no_gate_by_default(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``required_before_ship`` (``["state"]``) runs no external gate."""
    from eawf.workflow.skills import ship as ship_module

    calls, runner = _stub_gate_runner()
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    # ``state`` is not a runnable gauntlet gate → no subprocess seam hit.
    assert calls == []


def test_action_gauntlet_all_green_proceeds(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All gates green → ship proceeds past the gauntlet (status=ok)."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={"required_before_ship": ["pre-commit", "lint", "typecheck", "tests"]},
    )
    calls, runner = _stub_gate_runner()
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    # Every requested gate ran, in canonical order.
    assert calls == ["pre-commit", "lint", "typecheck", "tests"]


def test_action_gauntlet_aborts_on_red_pytest(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected red ``tests`` gate aborts ship and is reported."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={"required_before_ship": ["tests"]},
    )
    _calls, runner = _stub_gate_runner(fail={"tests"})
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.rollback_notes == "gauntlet gate failed: tests"
    assert env.footer.repair_commands == ["uv run pytest"]


def test_action_gauntlet_each_gate_independently_reported(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each gate failing independently is independently surfaced."""
    from eawf.workflow.skills import ship as ship_module

    for gate, command in (
        ("pre-commit", "uv run pre-commit run --all-files"),
        ("lint", "uv run ruff check ."),
        ("typecheck", "uv run mypy ."),
        ("tests", "uv run pytest"),
    ):
        _write_acceptance_config(
            state_dir,
            acceptance_overrides={"required_before_ship": [gate]},
        )
        _calls, runner = _stub_gate_runner(fail={gate})
        monkeypatch.setattr(ship_module, "_run_gate_command", runner)
        env = run_skill(ShipSkill(), _ctx())
        assert env.header.status == "failed", gate
        body = ShipBody.model_validate(cast(dict, env.body))
        assert body.rollback_notes == f"gauntlet gate failed: {gate}", gate
        assert env.footer.repair_commands == [command], gate


def test_action_gauntlet_red_gate_emits_gate_failure_payload(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red gate emits a ``ship.gauntlet_gate`` event with the gate shape."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={"required_before_ship": ["lint", "tests"]},
    )
    _calls, runner = _stub_gate_runner(fail={"tests"})
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    events_path = state_dir / "store" / "event.jsonl"
    envelopes = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    gate_events = [e for e in envelopes if e["payload"].get("event_type") == "ship.gauntlet_gate"]
    assert len(gate_events) == 1
    payload = gate_events[0]["payload"]
    assert payload["passed"] is False
    # The lint gate passed; only the failing tests gate is reported.
    failed = payload["gates"]
    assert [g["gate"] for g in failed] == ["tests"]
    assert failed[0]["command"] == "uv run pytest"
    assert failed[0]["returncode"] == 1
    assert "tests failed" in failed[0]["output"]


def test_action_gauntlet_uses_configured_command_override(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured ``acceptance.commands.tests`` override is the run command."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={
            "commands": {"tests": "uv run pytest tests/fast -q"},
            "required_before_ship": ["tests"],
        },
    )
    seen: list[str] = []

    def _runner(name: str, command: str, cwd: Path) -> object:
        from eawf.workflow.skills.ship import _GateResult

        seen.append(command)
        return _GateResult(name=name, command=command, passed=True, returncode=0, output="")

    monkeypatch.setattr(ship_module, "_run_gate_command", _runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    assert seen == ["uv run pytest tests/fast -q"]


# ---- P27-I02-W35: configured build gate runs ---------------------------------


def test_run_gauntlet_runs_configured_build_gate(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``build`` gate (configured via ``acceptance.commands.build``) runs.

    ``build`` is not a key in ``_DEFAULT_GATE_COMMANDS`` but is a real config
    leaf; once required + resolvable it must be executed, not silently dropped.
    """
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={
            "commands": {"build": "uv run python -m build"},
            "required_before_ship": ["build"],
        },
    )
    calls, runner = _stub_gate_runner()
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    assert calls == ["build"]


def test_run_gauntlet_build_gate_uses_configured_command(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved ``build`` command is the one passed to the runner."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={
            "commands": {"build": "uv run python -m build --sdist"},
            "required_before_ship": ["build"],
        },
    )
    seen: list[str] = []

    def _runner(name: str, command: str, cwd: Path) -> object:
        from eawf.workflow.skills.ship import _GateResult

        seen.append(command)
        return _GateResult(name=name, command=command, passed=True, returncode=0, output="")

    monkeypatch.setattr(ship_module, "_run_gate_command", _runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    assert seen == ["uv run python -m build --sdist"]


def test_action_gauntlet_aborts_on_red_build_gate(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``build`` gate aborts the ship and surfaces its command."""
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={
            "commands": {"build": "uv run python -m build"},
            "required_before_ship": ["build"],
        },
    )
    _calls, runner = _stub_gate_runner(fail={"build"})
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "failed"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert body.rollback_notes == "gauntlet gate failed: build"
    assert env.footer.repair_commands == ["uv run python -m build"]


def test_run_gauntlet_unconfigured_build_gate_is_dropped(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A required ``build`` gate with no command leaf stays non-runnable.

    ``build`` has no ``_DEFAULT_GATE_COMMANDS`` entry, so without an
    ``acceptance.commands.build`` override it resolves to ``None`` and is
    skipped (mirrors how ``state`` is skipped) rather than crashing the ship.
    """
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={"required_before_ship": ["build"]},
    )
    calls, runner = _stub_gate_runner()
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    assert calls == []


def test_run_gauntlet_defaults_lead_then_build(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default gates run in canonical order, then the extra ``build`` gate.

    Even when ``build`` is listed first, the canonical default gates lead and
    the extra configured gate follows, so the run order is deterministic.
    """
    from eawf.workflow.skills import ship as ship_module

    _write_acceptance_config(
        state_dir,
        acceptance_overrides={
            "commands": {"build": "uv run python -m build"},
            "required_before_ship": ["build", "tests", "lint"],
        },
    )
    calls, runner = _stub_gate_runner()
    monkeypatch.setattr(ship_module, "_run_gate_command", runner)
    env = run_skill(ShipSkill(), _ctx())
    assert env.header.status == "ok"
    assert calls == ["lint", "tests", "build"]


def test_run_gate_command_missing_binary_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-existent binary collapses to a failed gate, not an exception.

    The P28-I01-W05 argv-policy wrap rejects un-allowlisted heads before
    they can reach :func:`subprocess.run`; this test stubs the validator
    to a pass so the FileNotFoundError branch under test still runs.
    """
    from eawf.workflow.skills import ship as ship_module
    from eawf.workflow.skills.ship import _run_gate_command

    monkeypatch.setattr(
        ship_module,
        "validate_gate_argv",
        lambda argv, *, allowlist: argv,
    )
    result = _run_gate_command("tests", "this-binary-does-not-exist-eawf --x", tmp_path)
    assert result.passed is False
    assert result.returncode is None
    assert "command not found" in result.output


def test_run_gate_command_nonzero_exit_is_red(tmp_path: Path) -> None:
    """A real subprocess exiting non-zero is reported as a red gate.

    Uses ``uv run pytest --collect-only -q -p no:cacheprovider`` against
    an empty tmp_path so pytest itself exits non-zero (no tests found is
    exit 5). ``pytest`` is in the L0 gauntlet allowlist and the argv
    carries no shell metacharacters.
    """
    from eawf.workflow.skills.ship import _run_gate_command

    result = _run_gate_command(
        "tests",
        "uv run pytest --collect-only -q -p no:cacheprovider",
        tmp_path,
    )
    assert result.passed is False
    # exit 5 == pytest's "no tests collected"; any non-zero is acceptable.
    assert result.returncode is not None and result.returncode != 0


def test_run_gate_command_zero_exit_is_green(tmp_path: Path) -> None:
    """A real subprocess exiting zero is reported as a green gate."""
    from eawf.workflow.skills.ship import _run_gate_command

    result = _run_gate_command("tests", "pytest --version", tmp_path)
    assert result.passed is True
    assert result.returncode == 0


# ---- P30-I23-W45: --release subject validation ----------------------------


def test_validate_release_subject_accepts_standalone_group() -> None:
    """A standalone ``(release=vX.Y.Z)`` group matches the extraction regex."""
    validate_release_subject("[P30] state: close iter + phase (release=v0.6.0)")


def test_validate_release_subject_accepts_separate_audit_and_release_groups() -> None:
    """An audit group and a release group side-by-side each stand alone → accepted."""
    validate_release_subject("[P30] state: close iter + phase (audit=A10) (release=v0.6.0)")


def test_validate_release_subject_accepts_prerelease_suffix() -> None:
    """The extraction regex admits an ``rc``/``a``/``b`` pre-release suffix."""
    validate_release_subject("[P30] state: close iter + phase (release=v1.0.0rc1)")


def test_validate_release_subject_rejects_fused_annotation() -> None:
    """The fused ``(audit=..., release=v0.6.0)`` shape is refused (A10-H7 blind spot)."""
    with pytest.raises(ValueError, match="standalone"):
        validate_release_subject("[P30] state: close iter + phase (audit=A10-H7, release=v0.6.0)")


def test_validate_release_subject_rejects_bare_release_token() -> None:
    """A bare ``release=vX.Y.Z`` not inside any paren group is refused."""
    with pytest.raises(ValueError):
        validate_release_subject("[P30] state: close release=v0.6.0")


def test_validate_release_subject_rejects_malformed_version() -> None:
    """A ``(release=...)`` group whose version fails the regex is refused."""
    with pytest.raises(ValueError):
        validate_release_subject("[P30] state: close iter + phase (release=v0.6)")


# ---- P30-I23-W45: /ship --gauntlet + --release + --skip-pr-pass -----------


def _ship_record_payload(state_dir: Path) -> dict:
    events_path = state_dir / "store" / "event.jsonl"
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    return next(r["payload"] for r in records if r["payload"].get("event_type") == "ship.record")


def test_ship_release_valid_version_carries_standalone_annotation(state_dir: Path) -> None:
    """``--release v0.6.0`` emits a phase-close commit group with a standalone group."""
    ctx = _ctx()
    ctx.args = {"release": "v0.6.0"}
    env = run_skill(ShipSkill(), ctx)
    assert env.header.status == "ok"
    body = ShipBody.model_validate(cast(dict, env.body))
    assert len(body.commit_groups) == 1
    assert "(release=v0.6.0)" in body.commit_groups[0].message


def test_ship_release_invalid_version_refused(state_dir: Path) -> None:
    """``--release`` with a non-version token is a typed refusal (status=failed)."""
    ctx = _ctx()
    ctx.args = {"release": "banana"}
    env = run_skill(ShipSkill(), ctx)
    assert env.header.status == "failed"
    assert env.footer.repair_commands


def test_ship_gauntlet_default_full_recorded(state_dir: Path) -> None:
    """No ``--gauntlet`` → the built-in ``ship.gauntlet`` default (full) is recorded."""
    run_skill(ShipSkill(), _ctx())
    assert _ship_record_payload(state_dir)["gauntlet"] == "full"


def test_ship_gauntlet_scoped_flag_honoured(state_dir: Path) -> None:
    """``--gauntlet scoped`` is recorded on the ship-record event."""
    ctx = _ctx()
    ctx.args = {"gauntlet": "scoped"}
    run_skill(ShipSkill(), ctx)
    assert _ship_record_payload(state_dir)["gauntlet"] == "scoped"


def test_ship_gauntlet_from_config_leaf(state_dir: Path) -> None:
    """The ``ship.gauntlet`` config leaf drives the mode when no flag is set."""
    import yaml

    (state_dir / "config.yaml").write_text(
        yaml.safe_dump({"ship": {"gauntlet": "scoped"}}), encoding="utf-8"
    )
    run_skill(ShipSkill(), _ctx())
    assert _ship_record_payload(state_dir)["gauntlet"] == "scoped"


def test_ship_gauntlet_unknown_records_warning_and_falls_back(state_dir: Path) -> None:
    """An out-of-set ``--gauntlet`` records an advisory warning and falls back to full."""
    ctx = _ctx()
    ctx.args = {"gauntlet": "turbo"}
    env = run_skill(ShipSkill(), ctx)
    assert "unknown_gauntlet" in {w.code for w in env.footer.warnings}
    assert _ship_record_payload(state_dir)["gauntlet"] == "full"


def test_ship_skip_pr_pass_default_false(state_dir: Path) -> None:
    """Default (no ``--skip-pr-pass``) records ``skip_pr_pass=False``."""
    run_skill(ShipSkill(), _ctx())
    assert _ship_record_payload(state_dir)["skip_pr_pass"] is False


def test_ship_skip_pr_pass_flag_recorded(state_dir: Path) -> None:
    """``--skip-pr-pass`` is coerced and recorded on the ship-record event."""
    ctx = _ctx()
    ctx.args = {"skip_pr_pass": True}
    run_skill(ShipSkill(), ctx)
    assert _ship_record_payload(state_dir)["skip_pr_pass"] is True
