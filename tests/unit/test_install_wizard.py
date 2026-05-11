"""Unit tests for the wizard config-yaml shape (P14-W03 / D14)."""

from __future__ import annotations

from pathlib import Path

import yaml

from eawf.config.defaults import CONFIG_SCHEMA_VERSION
from eawf.install.wizard import WizardAnswers, _build_config_yaml, run_wizard_no_input


def _answers(runtime: str = "claude-code") -> WizardAnswers:
    return WizardAnswers(
        state_path=".ea/state.json",
        project_code="DEMO",
        project_title="Demo",
        lifecycle_depth="phase",
        profiles=("core",),
        runtime=runtime,
        plugins=(),
        mcp=(),
    )


def test_config_yaml_emits_adapters_list() -> None:
    body = _build_config_yaml(_answers("claude-code"))
    runtime = body["runtime"]
    assert runtime["adapters"] == ["claude-code"]
    assert runtime["kind"] == "claude-code"


def test_config_yaml_carries_schema_v1_1() -> None:
    body = _build_config_yaml(_answers())
    assert body["schema_version"] == CONFIG_SCHEMA_VERSION
    assert body["schema_version"] == "1.1"


def test_runtime_choices_accept_codex() -> None:
    answers = _answers("codex")
    body = _build_config_yaml(answers)
    assert body["runtime"]["adapters"] == ["codex"]


def test_wizard_writes_disk_config_with_adapters(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    answers = _answers("opencode")
    result = run_wizard_no_input(answers, target, force=False)
    config_text = result.config_path.read_text()
    loaded = yaml.safe_load(config_text)
    assert loaded["runtime"]["adapters"] == ["opencode"]
    assert loaded["runtime"]["kind"] == "opencode"
    assert loaded["schema_version"] == "1.1"
