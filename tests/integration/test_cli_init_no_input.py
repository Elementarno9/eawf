"""Integration tests for ``eawf init --no-input``.

Exercises the pure-pipeline branch of the wizard via the actual Typer app.
Covers the v0.1 acceptance set:

- creates ``.ea/state.json`` and ``.ea/config.yaml``;
- writes ``AGENTS.md`` + ``CLAUDE.md`` with the managed-region markers
  emitted by :mod:`eawf.render.agents_md`;
- rejects bad ``--profile`` / ``--project-code`` inputs with exit-code 3;
- refuses to clobber an existing ``.ea/`` without ``--force``.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


def _invoke_init(target: Path, *extra: str) -> object:
    """Run ``eawf --no-input init --project-code DEMO --target <tmp>`` plus *extra*."""
    args = ["--no-input", "init", "--project-code", "DEMO", "--target", str(target), *extra]
    return runner.invoke(app, args)


def test_cli_init_no_input_creates_state_and_config(tmp_path: Path) -> None:
    """A baseline invocation lays down state.json + config.yaml + AGENTS.md + CLAUDE.md."""
    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 0, res.stdout

    state_path = tmp_path / ".ea" / "state.json"
    config_path = tmp_path / ".ea" / "config.yaml"
    agents_md = tmp_path / "AGENTS.md"
    claude_md = tmp_path / "CLAUDE.md"

    assert state_path.exists(), "state.json must be written"
    assert config_path.exists(), "config.yaml must be written"
    assert agents_md.exists(), "AGENTS.md must be rendered"
    assert claude_md.exists(), "CLAUDE.md shim must be written"

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "1.0"
    assert state["scope_kind"] == "repo"
    assert state["current"]["project_code"] == "DEMO"

    text = agents_md.read_text(encoding="utf-8")
    assert "BEGIN" in text and "END" in text, "AGENTS.md must contain managed-region markers"

    assert claude_md.read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_cli_init_no_input_validates_profile_membership(tmp_path: Path) -> None:
    """``--profile bogus`` exits 3 (InvalidInput) before any file is written."""
    res = _invoke_init(tmp_path, "--profile", "bogus-not-a-real-profile")
    assert res.exit_code == 3, res.stdout
    assert not (tmp_path / ".ea").exists(), ".ea must not be created when profile validation fails"


def test_cli_init_rejects_invalid_project_code(tmp_path: Path) -> None:
    """``--project-code lowercase`` exits 3 — regex enforced by WizardAnswers."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "lowercase",
            "--profile",
            "core",
            "--target",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 3, res.stdout


def test_cli_init_refuses_existing_non_empty_ea_without_force(tmp_path: Path) -> None:
    """Pre-existing .ea/state.json blocks init unless --force is passed."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text("{}", encoding="utf-8")

    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 3, res.stdout

    # With --force, init succeeds.
    res = _invoke_init(tmp_path, "--profile", "core", "--force")
    assert res.exit_code == 0, res.stdout


def test_cli_init_writes_correct_config_yaml(tmp_path: Path) -> None:
    """config.yaml records profiles.enabled, runtime.adapters, acceptance gates.

    P26-W02 (C08 D14): the legacy top-level ``lifecycle`` and ``plugins``
    blocks are no longer emitted, and ``runtime.kind`` is replaced by the
    canonical ``runtime.adapters`` + ``runtime.preference`` pair.
    """
    res = _invoke_init(
        tmp_path,
        "--profile",
        "core",
        "--profile",
        "python",
        "--runtime",
        "claude-code",
        "--lifecycle-depth",
        "wave",
        "--no-acceptance-typecheck",
    )
    assert res.exit_code == 0, res.stdout

    config_text = (tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(config_text)
    assert parsed["profiles"]["enabled"] == ["core", "python"]
    assert parsed["runtime"]["adapters"] == ["claude-code"]
    assert parsed["runtime"]["preference"] == ["claude-code"]
    assert "kind" not in parsed["runtime"]
    assert "lifecycle" not in parsed
    assert "plugins" not in parsed
    assert parsed["acceptance"]["tests"] is True
    assert parsed["acceptance"]["lint"] is True
    assert parsed["acceptance"]["typecheck"] is False


def test_cli_init_renders_managed_regions_for_each_block(tmp_path: Path) -> None:
    """AGENTS.md contains BEGIN/END markers per render block in the composed profile."""
    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 0, res.stdout

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Core profile ships several render blocks; non-negotiable-rules is the
    # first and most stable. Marker shape (per eawf.render.regions):
    # "<!-- BEGIN EAWF:managed id=<id> ... -->".
    assert "BEGIN EAWF:managed id=non-negotiable-rules" in text
    assert "END EAWF:managed id=non-negotiable-rules" in text
    # A representative subset of the additional core blocks must also render.
    assert "BEGIN EAWF:managed id=worktree-discipline" in text
    assert "BEGIN EAWF:managed id=anti-patterns" in text


def test_cli_init_no_input_emits_json_envelope(tmp_path: Path) -> None:
    """``--json`` surfaces the WizardResult payload deterministically."""
    res = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--profile",
            "core",
            "--target",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["project_code"] == "DEMO"
    assert payload["profiles_enabled"] == ["core"]
    assert payload["state_path"].endswith(".ea/state.json")
    assert payload["config_path"].endswith(".ea/config.yaml")
    assert payload["agents_md_path"].endswith("AGENTS.md")
    assert payload["claude_md_path"].endswith("CLAUDE.md")


def test_cli_init_no_input_requires_project_code(tmp_path: Path) -> None:
    """Missing --project-code with --no-input fails fast with exit 3."""
    res = runner.invoke(
        app,
        ["--no-input", "init", "--profile", "core", "--target", str(tmp_path)],
    )
    assert res.exit_code == 3, res.stdout
    assert not (tmp_path / ".ea").exists()
