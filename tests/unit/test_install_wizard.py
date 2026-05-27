"""Unit tests for the wizard config-yaml shape (P14-W03 / D14 / P26-W02)."""

from __future__ import annotations

from pathlib import Path

import yaml

from eawf.kernel.config.defaults import CONFIG_SCHEMA_VERSION
from eawf.platform.install.wizard import WizardAnswers, _build_config_yaml, run_wizard_no_input
from eawf.workflow.estimation.buckets import BUCKET_EU


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
    # ``runtime.kind`` is dropped per C08 / D14 — the warning shim was
    # firing on every CLI invocation while it was emitted.
    assert "kind" not in runtime


def test_config_yaml_emits_preference_ladder() -> None:
    """P26-W02: ``runtime.preference`` mirrors the adapters list at init."""
    body = _build_config_yaml(_answers("claude-code"))
    assert body["runtime"]["preference"] == ["claude-code"]


def test_config_yaml_carries_schema_marker() -> None:
    """P25-W14 (C08) bumps the canonical schema marker to ``1.0``."""
    body = _build_config_yaml(_answers())
    assert body["schema_version"] == CONFIG_SCHEMA_VERSION
    assert body["schema_version"] == "1.0"


def test_config_yaml_drops_legacy_lifecycle_and_plugins() -> None:
    """P26-W02: legacy top-level keys are no longer emitted to YAML."""
    body = _build_config_yaml(_answers())
    assert "lifecycle" not in body
    assert "plugins" not in body


def test_config_yaml_seeds_project_goals_after_template_merge() -> None:
    """Empty template goals cannot wipe the bootstrap project intent."""
    answers = _answers().model_copy(update={"template_extras": {"project": {"goals": []}}})
    body = _build_config_yaml(answers)
    assert body["project"]["goals"] == ["Establish Demo project intent"]


def test_config_yaml_seeds_bucket_overrides_after_template_merge() -> None:
    """Empty template bucket overrides cannot wipe canonical EU defaults."""
    answers = _answers().model_copy(
        update={"template_extras": {"estimation": {"buckets": {"overrides": {}}}}}
    )
    body = _build_config_yaml(answers)
    assert body["estimation"]["buckets"]["overrides"] == {
        bucket.value: {"expected_eu": expected_eu} for bucket, expected_eu in BUCKET_EU.items()
    }


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
    assert loaded["runtime"]["preference"] == ["opencode"]
    assert "kind" not in loaded["runtime"]
    assert loaded["schema_version"] == "1.0"
    assert "lifecycle" not in loaded
    assert "plugins" not in loaded
