"""Integration tests for ``eawf doctor`` driven via :class:`CliRunner`.

The instrument probe is stubbed at the module boundary so the suite never
shells out to ``shutil.which`` / ``subprocess.run``. Each test pins
``EA_INSTRUMENT_PROBE`` (or passes ``-w/--workspace``) so the cache lives in
a tmp dir and never touches the developer's workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.platform.install.instrument_probe import (
    PROBE_VERSION,
    ProbeReport,
    ProbeResult,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _sandbox_host_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host daemon and global config state out of doctor tests."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    report = SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path",
        lambda: tmp_path / "global-config.yaml",
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version",
        lambda: None,
    )


def _green_probe(*_args: object, **_kwargs: object) -> ProbeReport:
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=["core"],
        results=[
            ProbeResult(name="git", kind="hard", status="ok", path="/x/git"),
            ProbeResult(name="python", kind="hard", status="ok", path="/x/python"),
            ProbeResult(name="uv", kind="hard", status="ok", path="/x/uv"),
        ],
    )


def _seed_state(workspace: Path) -> None:
    """Drop a stub ``state.json`` so the ``state_present`` check passes."""
    state = workspace / ".ea" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")


def _repair_plan(workspace: Path) -> Any:
    from eawf.observability.doctor.repair import DoctorRepairAction, DoctorRepairPlan

    digest = f"sha256:{'0' * 64}"
    return DoctorRepairPlan(
        workspace=str(workspace),
        preview_digest=digest,
        actions=[
            DoctorRepairAction(
                action_id="lifecycle.sync",
                scope="repo",
                preview_digest=digest,
                mutation_class="managed_rules",
                record_count=1,
                detail="refresh managed rules",
            )
        ],
        rerun_command=f"eawf --workspace {workspace} doctor --fix --yes",
    )


def test_doctor_green_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda _workspace: _repair_plan(tmp_path),
    )
    monkeypatch.chdir(tmp_path)
    _seed_state(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "doctor"])
    assert result.exit_code == 0, result.output
    assert "tools_available" in result.output
    assert "overall: ok" in result.output
    assert "doctor --fix" in result.output


def test_doctor_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON envelope lists every check the doctor surface emits."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda _workspace: _repair_plan(tmp_path),
    )
    monkeypatch.chdir(tmp_path)
    _seed_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["repair"]["action_count"] >= 1
    assert body["repair"]["preview_command"].endswith("doctor --fix")
    names = {c["name"] for c in body["checks"]}
    assert names == {
        "tools_available",
        "state_present",
        "config_resolves",
        "reserved_config_key",
        "manifest_in_sync",
        "mcp_drift",
        "state_scale_ceiling",
        "active_phase_without_iter",
        "stale_session_count",
        "recent_actuals",
        "iter_audit_links",
        "incident_fold_parity",
        "backlog_fold_parity",
        "cli_daemon_version",
        "parallel_cap_enforcement",
        "launchd_agent",
        "runtime_dir_size",
        "render_output_roundtrip",
        "agents_md_byte_cap",
        "branch_currency",
        "project_record_present",
        "plugin_cross_scope_dup",
        "git_state_drift",
    }


def test_doctor_fix_previews_then_honors_single_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_state(tmp_path)
    plan = _repair_plan(tmp_path)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda _workspace: plan,
    )
    applied = False

    def apply(_plan: Any) -> dict[str, Any]:
        nonlocal applied
        applied = True
        return {"applied_count": 1}

    monkeypatch.setattr("eawf.surfaces.doctor_repair.apply_repair_plan", apply)

    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "doctor", "--fix"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "doctor repair preview: 1 action(s)" in result.output
    assert "Apply this repair plan?" in result.output
    assert "not applied; rerun:" in result.output
    assert applied is False


def test_doctor_fix_json_never_prompts_and_returns_needs_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_state(tmp_path)
    plan = _repair_plan(tmp_path)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda _workspace: plan,
    )

    result = runner.invoke(
        app,
        ["--json", "-w", str(tmp_path), "doctor", "--fix"],
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["status"] == "needs_user"
    assert body["actions"][0]["action_id"] == "lifecycle.sync"
    assert body["rerun_command"].endswith("doctor --fix --yes")


def test_doctor_fix_yes_applies_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_state(tmp_path)
    plan = _repair_plan(tmp_path)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda _workspace: plan,
    )
    monkeypatch.setattr(
        "eawf.surfaces.doctor_repair.apply_repair_plan",
        lambda _plan: {"applied_count": 1, "actions": []},
    )

    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "doctor", "--fix", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "doctor repair applied: 1 action(s)" in result.output
    assert "Apply this repair plan?" not in result.output


def test_doctor_real_yaml_normalizes_legacy_auto_accept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.check_launchd_agent",
        lambda: type("Check", (), {"status": "ok"})(),
    )
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "config.yaml").write_text(
        "flow:\n  auto_accept:\n    prep: true\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    finding = next(check for check in body["checks"] if check["name"] == "reserved_config_key")
    assert finding["status"] == "ok"
    assert body["repair"]["action_count"] >= 1
    assert body["repair"]["preview_command"].endswith("doctor --fix")


def test_doctor_real_yaml_accepts_consumed_repair_cycle_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    _seed_state(tmp_path)
    (tmp_path / ".ea" / "config.yaml").write_text(
        "flow:\n  max_repair_cycles: 2\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    finding = next(check for check in body["checks"] if check["name"] == "reserved_config_key")
    assert finding["status"] == "ok"
    assert "unsupported" not in finding["detail"]


def test_doctor_reprobe_clears_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "instrument-probe.json"
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(cache))
    cache.write_text(json.dumps({"stale": True}), encoding="utf-8")
    assert cache.exists()

    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "doctor", "--reprobe"])
    assert result.exit_code == 0, result.output
    # The CLI removed the cache file before invoking probe; the probe stub
    # does not write a replacement, so the file stays absent.
    assert not cache.exists()


def test_doctor_hard_missing_exits_six(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.surfaces.cli.errors import UserError

    def angry_probe(*_args: Any, **_kwargs: Any) -> ProbeReport:
        raise UserError("git missing", kind="InstrumentMissing")

    monkeypatch.setattr("eawf.observability.doctor.checks.probe", angry_probe)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "doctor"])
    assert result.exit_code == 1, result.output
    assert "git missing" in result.output


def test_doctor_help_lists_flags() -> None:
    """The ``doctor`` callback declares ``--reprobe`` and ``--json`` flags.

    Structural check via Click introspection so the assertion never depends on
    terminal width or Rich help wrapping (which differs across CI runners).
    """
    import typer

    from eawf.surfaces.cli.commands.doctor import doctor_app

    cmd = typer.main.get_command(doctor_app)
    flag_names = {opt for p in cmd.params for opt in p.opts}
    assert "--reprobe" in flag_names
    assert "--json" in flag_names
