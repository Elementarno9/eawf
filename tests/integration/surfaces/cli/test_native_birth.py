"""``eawf init`` bears its tree native: no v1 document, no epoch-1 preview.

The wizard used to write a v1 ``state.json`` with empty phase, iter and wave
maps plus a starter ``P00-I00-W00`` wave preview, which the birth then
migrated. A born tree is the epoch-2 generation alone, so neither the
document nor the preview may land, and the CI and release smoke steps must
not look for the document either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.install import wizard
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

REPO: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = (REPO / ".github/workflows/ci.yaml", REPO / ".github/workflows/release.yaml")
PREVIEW_WAVE_ID: Final = "P00-I00-W00"
EPOCH1_LIFECYCLE_KEYS: Final = frozenset({"phases", "iters", "waves"})


def _keys(node: Any) -> set[str]:
    """Return every mapping key anywhere inside a parsed JSON or YAML value."""
    if isinstance(node, dict):
        return {str(key) for key in node} | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for value in node for k in _keys(value)}
    return set()


def _assert_no_epoch1_preview(tree: Path) -> None:
    """Assert no file under ``tree`` carries the preview wave id or a v1 lifecycle key."""
    files = [path for path in tree.rglob("*") if path.is_file()]
    assert files
    for path in files:
        raw = path.read_bytes()
        assert PREVIEW_WAVE_ID.encode() not in raw, path
        if path.suffix == ".json":
            parsed: Any = json.loads(raw)
        elif path.suffix in {".yaml", ".yml"}:
            parsed = yaml.safe_load(raw)
        else:
            continue
        assert not _keys(parsed) & EPOCH1_LIFECYCLE_KEYS, path


def test_no_input_init_writes_no_v1_document(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "--no-input", "init", "--target", str(tmp_path), "--project-code", "DEMO"],
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert not (tmp_path / ".ea" / "state.json").exists()
    authority = resolve_authority(tmp_path / ".ea")
    assert authority.epoch == 2, authority.gap
    assert authority.generation_id is not None
    assert json.loads(result.output)["generation_id"] == authority.generation_id


def test_init_paths_write_no_epoch1_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_input = tmp_path / "no-input"
    result = runner.invoke(
        app,
        ["--json", "--no-input", "init", "--target", str(no_input), "--project-code", "DEMO"],
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert PREVIEW_WAVE_ID not in result.output
    _assert_no_epoch1_preview(no_input / ".ea")

    quick = tmp_path / "quick"
    quick.mkdir()
    (quick / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--quick", "--target", str(quick)])
    assert result.exit_code == exit_codes.OK, result.output
    _assert_no_epoch1_preview(quick / ".ea")

    answers = wizard.WizardAnswers(
        state_path=".ea/state.json",
        project_code="DEMO",
        project_title="Demo",
        profiles=("core",),
        runtime="claude-code",
        lifecycle_depth="phase",
    )

    def _answered(target_dir: Path, *, force: bool = False) -> wizard.WizardResult:
        return wizard.run_wizard_no_input(answers, target_dir, force=force)

    monkeypatch.setattr(wizard, "run_wizard_interactive", _answered)
    interactive = tmp_path / "interactive"
    result = runner.invoke(app, ["init", "--target", str(interactive)])
    assert result.exit_code == exit_codes.OK, result.output
    _assert_no_epoch1_preview(interactive / ".ea")


def _smoke_steps() -> list[tuple[str, dict[str, Any]]]:
    """Return ``(workflow name, step)`` for every step that runs ``eawf ... init``."""
    steps: list[tuple[str, dict[str, Any]]] = []
    for workflow in WORKFLOWS:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if "eawf" in run and " init " in run:
                    steps.append((workflow.name, step))
    return steps


def test_ci_and_release_smoke_assert_no_v1_document() -> None:
    steps = _smoke_steps()
    assert {name for name, _ in steps} == {"ci.yaml", "release.yaml"}
    for name, step in steps:
        assert ".ea/state.json" not in step["run"], f"{name}: {step.get('name')}"
        assert "EA_STATE" not in step["run"], f"{name}: {step.get('name')}"
        assert "EA_STATE" not in step.get("env", {}), f"{name}: {step.get('name')}"
