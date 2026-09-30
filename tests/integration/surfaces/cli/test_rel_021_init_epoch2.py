"""REL-021: ``eawf init`` creates an epoch-2 tree on every one of its paths.

After the flag day an epoch-1 tree refuses every mutation, so a tree init
left at epoch 1 would be born unusable. Each path -- ``--no-input``,
``--quick``, the interactive wizard and the ``/init`` skill -- must leave a
tree carrying an opt-in pinned to a backup that still verifies, a selected
generation and the epoch marker, so the resolver grants it epoch 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import (
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
    DisposableTarget,
    OptInDeclaration,
)
from eawf.kernel.migration.epoch2.opt_in import verify_opt_in_backup
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.install import wizard
from eawf.platform.install.epoch2_birth import INIT_DECLARED_BY
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _assert_born_at_epoch2(root: Path) -> str:
    """Assert the tree under ``root`` is an activated epoch-2 tree; return its generation."""
    tree = root / ".ea"
    authority = resolve_authority(tree)
    assert authority.epoch == 2, authority.gap
    assert (tree / GENERATIONS_DIRNAME / MARKER_FILENAME).is_file()
    target = DisposableTarget.require(tree)
    assert isinstance(target.declaration, OptInDeclaration)
    assert target.declaration.declared_by == INIT_DECLARED_BY
    verified = verify_opt_in_backup(target)
    assert verified is not None
    assert "state.json" in verified.artifacts
    assert authority.generation_id is not None
    return authority.generation_id


def test_rel_021_no_input_init_creates_an_epoch2_tree(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "--no-input", "init", "--target", str(tmp_path), "--project-code", "DEMO"],
    )
    assert result.exit_code == exit_codes.OK, result.output
    payload = json.loads(result.output)
    generation_id = _assert_born_at_epoch2(tmp_path)
    assert payload["epoch"] == 2
    assert payload["generation_id"] == generation_id


def test_rel_021_quick_init_creates_an_epoch2_tree(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--quick", "--target", str(tmp_path)])
    assert result.exit_code == exit_codes.OK, result.output
    _assert_born_at_epoch2(tmp_path)


def test_rel_021_interactive_init_creates_an_epoch2_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    result = runner.invoke(app, ["init", "--target", str(tmp_path)])
    assert result.exit_code == exit_codes.OK, result.output
    _assert_born_at_epoch2(tmp_path)


def test_rel_021_a_forced_reinit_of_an_epoch2_tree_refuses(tmp_path: Path) -> None:
    argv = ["--no-input", "init", "--target", str(tmp_path), "--project-code", "DEMO"]
    assert runner.invoke(app, argv).exit_code == exit_codes.OK
    first = _assert_born_at_epoch2(tmp_path)
    state_before = (tmp_path / ".ea" / "state.json").read_bytes()
    result = runner.invoke(app, ["--json", *argv, "--force"])
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert json.loads(result.output)["data"]["kind"] == "LegacyOperationRemoved"
    assert (tmp_path / ".ea" / "state.json").read_bytes() == state_before
    assert _assert_born_at_epoch2(tmp_path) == first


def test_rel_021_an_initialised_tree_takes_a_mutating_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ["--no-input", "init", "--target", str(tmp_path), "--project-code", "DEMO"]
    assert runner.invoke(app, argv).exit_code == exit_codes.OK
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "memory", "add", "a durable fact"])
    assert "MigrationRequired" not in result.output
