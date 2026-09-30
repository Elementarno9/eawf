"""SURF-004: the release chokepoint renders the steering chain in certified mode.

A release claims what an agent on this machine receives, so the sweep that
``eawf release tag --push`` and ``eawf release preflight`` are gated on refuses,
before any signal runs, when the host's global instruction documents put a
loaded chain over its prompt-budget ceiling; an ordinary local render of the
same tree only warns. Each case runs over a temporary home.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.platform.rules.render import plan_rule_projections
from eawf.runtime.release import sweep_for_tag
from eawf.surfaces.cli.app import app
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.release_readiness import ReleaseReadiness

pytestmark = pytest.mark.integration

_VERSION = "0.7.0.dev2"
_GLOBAL_BYTES = 40_000


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(variable, raising=False)
    root = tmp_path / "cut"
    (root / ".ea").mkdir(parents=True)
    rules = {"schema_version": 1, "rules": []}
    (root / ".ea" / "rules.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndescription = "A demo project"\n', encoding="utf-8"
    )
    return root


def _overflow(tmp_path: Path) -> None:
    codex = tmp_path / "home" / ".codex"
    codex.mkdir(parents=True)
    (codex / "AGENTS.md").write_text("g" * _GLOBAL_BYTES, encoding="utf-8")


def _config() -> ReleaseConfig:
    return load_release_config(checkpoint_config_yaml(_VERSION), train=V07_TRAIN)


def _sweep(repo: Path) -> ReleaseReadiness:
    return sweep_for_tag(
        _config(),
        version=_VERSION,
        repo_root=repo,
        remote="origin",
        source=None,
        waiver_count=0,
        computed_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
    )


def test_surf_004_the_release_sweep_refuses_a_global_overflow(repo: Path, tmp_path: Path) -> None:
    _overflow(tmp_path)

    with pytest.raises(ValueError, match="certified steering render refuses the release") as info:
        _sweep(repo)

    assert f"~/.codex/AGENTS.md ({_GLOBAL_BYTES} bytes)" in str(info.value)
    assert "-byte ceiling" in str(info.value)


def test_surf_004_the_same_tree_renders_locally_with_a_warning(repo: Path, tmp_path: Path) -> None:
    _overflow(tmp_path)

    manifest = plan_rule_projections(repo).manifest

    assert manifest.render_mode == "local"
    assert any(f"({_GLOBAL_BYTES} bytes)" in line for line in manifest.render_warnings)


def test_surf_004_the_release_sweep_runs_without_an_overflow(repo: Path) -> None:
    readiness = _sweep(repo)

    assert readiness.version == _VERSION
    assert readiness.signals


def test_surf_004_release_preflight_refuses_a_global_overflow(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overflow(tmp_path)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["release", "preflight", _VERSION])

    assert result.exit_code != 0
    assert f"~/.codex/AGENTS.md ({_GLOBAL_BYTES} bytes)" in result.output
