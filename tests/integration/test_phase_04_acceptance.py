"""Phase 4 acceptance integration test.

Single end-to-end scenario mirroring the W05 plan-row goal: render the
Claude plugin tree, drive ``/research`` headless, run a hook event,
emit a statusline. Each step asserts the surface contract: exit code,
envelope shape, on-disk artifacts.

Marked ``integration`` so the test runs under both the default suite
and ``pytest -m integration``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.render.envelope import OutputEnvelope
from eawf.skills.bodies.research import ResearchBody

runner = CliRunner()


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git invocation to fail with FileNotFoundError so the
    statusline renders ``git:-`` regardless of the surrounding repo."""

    def fake_run(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.mark.integration
def test_phase_04_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    # Step 1 — init temp Eä repo via the wizard's CLI surface (calls
    # run_wizard_no_input under the hood).
    result = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--target",
            str(repo),
            "--profile",
            "core",
        ],
    )
    assert result.exit_code == 0, result.stdout
    state_path = repo / ".ea" / "state.json"
    assert state_path.exists()

    # Step 2 — `eawf plugin install claude` produces the plugin tree.
    result = runner.invoke(
        app,
        ["-w", str(repo), "plugin", "install", "claude"],
    )
    assert result.exit_code == 0, result.stdout
    assert (repo / ".claude" / "skills" / "research" / "SKILL.md").exists()
    assert (repo / ".claude" / "agents" / "executor.md").exists()
    assert (repo / ".claude" / "hooks" / "pre_commit.sh").exists()
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "__eawf_managed" in settings

    # Step 3 — `eawf skill run /research --json` emits a well-formed
    # envelope with status=ok and a populated recommendation.
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv(
        "EA_INSTRUMENT_PROBE",
        str(repo / ".ea" / "instrument-probe.json"),
    )
    result = runner.invoke(
        app,
        ["--json", "skill", "run", "/research"],
        input='{"depth": "normal"}',
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.skill == "/research"
    assert env.header.status == "ok"
    body = ResearchBody.model_validate(env.body)
    assert body.recommendation is not None
    assert body.recommendation.choice == "option-1"

    # Step 4 — `eawf hook run post_commit` returns a canonical envelope
    # with exit 0 (no hooks registered at v1; the surface contract is
    # exercised regardless).
    result = runner.invoke(
        app,
        ["hook", "run", "post_commit", "--scope", "DEMO-P04-W08"],
        input=json.dumps({"branch": "feature/x", "files_changed": ["a.py"]}),
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.status == "ok"
    assert isinstance(env.body, dict)
    assert env.body["event_type"] == "post_commit"

    # Step 5 — `eawf cc statusline` emits a single line on stdout.
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("EAWF_STATUSLINE_THEME", raising=False)
    _stub_no_git(monkeypatch)
    result = runner.invoke(
        app,
        ["cc", "statusline", "--theme", "ascii-fallback"],
        input=json.dumps(
            {
                "session_id": "ses-p04-acceptance",
                "model": "claude-opus-4-7",
                "cwd": str(repo),
            }
        ),
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    assert "\n" not in line
    assert "model:claude-opus-4-7" in line
