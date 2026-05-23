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

from eawf.cli.app import app
from eawf.install.instrument_probe import (
    PROBE_VERSION,
    ProbeReport,
    ProbeResult,
)

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


def _seed_state(workspace: Path) -> None:
    """Drop a stub ``state.json`` so the ``state_present`` check passes."""
    state = workspace / ".ea" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")


def test_doctor_green_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    _seed_state(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "doctor"])
    assert result.exit_code == 0, result.output
    assert "tools_available" in result.output
    assert "overall: ok" in result.output


def test_doctor_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON envelope lists every check ``run_all`` produces (W08: five)."""
    monkeypatch.setattr("eawf.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    _seed_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["ok"] is True
    assert body["status"] == "ok"
    names = {c["name"] for c in body["checks"]}
    assert names == {
        "tools_available",
        "state_present",
        "config_resolves",
        "manifest_in_sync",
        "mcp_drift",
        "render_output_roundtrip",
    }


def test_doctor_reprobe_clears_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "instrument-probe.json"
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(cache))
    cache.write_text(json.dumps({"stale": True}), encoding="utf-8")
    assert cache.exists()

    monkeypatch.setattr("eawf.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "doctor", "--reprobe"])
    assert result.exit_code == 0, result.output
    # The CLI removed the cache file before invoking probe; the probe stub
    # does not write a replacement, so the file stays absent.
    assert not cache.exists()


def test_doctor_hard_missing_exits_six(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.cli.errors import UserError

    def angry_probe(*_args: Any, **_kwargs: Any) -> ProbeReport:
        raise UserError("git missing", kind="InstrumentMissing")

    monkeypatch.setattr("eawf.doctor.checks.probe", angry_probe)
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

    from eawf.cli.commands.doctor import doctor_app

    cmd = typer.main.get_command(doctor_app)
    flag_names = {opt for p in cmd.params for opt in p.opts}
    assert "--reprobe" in flag_names
    assert "--json" in flag_names
