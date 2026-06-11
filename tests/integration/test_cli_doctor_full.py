"""Integration tests for the W08 extended ``eawf doctor`` surface.

These tests focus on the new ``manifest_in_sync`` and
``render_output_roundtrip`` checks added in W08. The W01 baseline (tools,
state-present, config-resolves) is covered in
``tests/integration/test_cli_doctor.py``.

The instrument probe is stubbed at the module boundary so the suite stays
hermetic — a real ``shutil.which("git")`` call would tie the green path to
the developer host's installed tooling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.platform.install.instrument_probe import (
    PROBE_VERSION,
    ProbeReport,
    ProbeResult,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()


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


def _init_core(target: Path) -> None:
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--profile",
            "core",
            "--target",
            str(target),
        ],
    )
    assert res.exit_code == 0, res.output


@pytest.mark.integration
def test_cli_doctor_full_green_after_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A freshly-initialised workspace reports every check as ``ok``."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    _init_core(tmp_path)

    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    names = {c["name"] for c in payload["checks"]}
    assert names == {
        "tools_available",
        "state_present",
        "config_resolves",
        "manifest_in_sync",
        "mcp_drift",
        "state_scale_ceiling",
        "render_output_roundtrip",
        "seal_capable",
        "project_record_present",
        "git_state_drift",
        "plugin_cross_scope_dup",
    }
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["manifest_in_sync"] == "ok"
    assert statuses["render_output_roundtrip"] == "ok"
    assert statuses["mcp_drift"] == "ok"
    # The seal row is purely informational -- always ``ok`` so a headless /
    # non-graphics CI box never reds the overall doctor verdict.
    assert statuses["seal_capable"] == "ok"
    assert statuses["state_scale_ceiling"] == "ok"
    assert statuses["project_record_present"] == "ok"
    assert statuses["git_state_drift"] == "ok"
    assert statuses["plugin_cross_scope_dup"] == "ok"


@pytest.mark.integration
def test_cli_doctor_warns_when_project_record_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy init-only state with ``project=null`` gets a repair hint."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    _init_core(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["project"] = None
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    entry = next(c for c in payload["checks"] if c["name"] == "project_record_present")
    assert entry["status"] == "warn"
    assert "project init --upgrade" in entry["detail"]


@pytest.mark.integration
def test_cli_doctor_detects_manifest_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand-editing AGENTS.md inside a managed region downgrades the check to ``warn``."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    _init_core(tmp_path)

    agents_md = tmp_path / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8")
    # Insert hand-edited content inside the body of the managed region.
    # The region body lives between BEGIN/END marker lines; injecting a
    # token before the END marker preserves the marker shape but mutates
    # the body hash, so :func:`detect_drift` flags it as ``hand-edited``.
    end_marker = "<!-- END EAWF:managed id=non-negotiable-rules -->"
    assert end_marker in text, "test pre-condition: end marker present"
    mutated = text.replace(end_marker, "DRIFT INJECT\n" + end_marker)
    agents_md.write_text(mutated, encoding="utf-8")

    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["manifest_in_sync"] == "warn"
    detail = next(c["detail"] for c in payload["checks"] if c["name"] == "manifest_in_sync")
    assert "non-negotiable-rules" in (detail or "")


@pytest.mark.integration
def test_cli_doctor_envelope_roundtrip_check_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render-output round-trip check appears in the doctor envelope."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    _init_core(tmp_path)

    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    names = [c["name"] for c in payload["checks"]]
    assert "render_output_roundtrip" in names
    entry = next(c for c in payload["checks"] if c["name"] == "render_output_roundtrip")
    assert entry["status"] == "ok"


@pytest.mark.integration
def test_cli_doctor_manifest_absent_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace with no manifest yet still reports ``manifest_in_sync == ok``.

    The check intentionally does not require a manifest — that is the
    ``state_present`` check's job. An uninitialised workspace gets ``warn``
    on ``state_present`` and ``ok`` on ``manifest_in_sync``.
    """
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["manifest_in_sync"] == "ok"


@pytest.mark.integration
def test_cli_doctor_manifest_malformed_is_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt manifest reports ``fail`` (not ``warn``) — that is the spec."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    _init_core(tmp_path)
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")

    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    # Overall status fail → exit 1 (forward-compat path in doctor.py).
    assert res.exit_code == 1, res.output
    payload = json.loads(res.output)
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["manifest_in_sync"] == "fail"
