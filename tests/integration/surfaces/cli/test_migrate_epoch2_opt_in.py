"""``eawf migrate epoch2 --opt-in`` opts a live epoch-1 tree into the cutover.

The apply admits a live repository only on an opt-in declaration pinned to a
verified backup, and until this verb only ``eawf init`` wrote one. Each test
lays down a plain epoch-1 tree, so the flag-day gate is in force, and drives
the verb through the CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import (
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
    DisposableTarget,
    OptInDeclaration,
    declaration_path,
    opt_in_path,
    read_declaration,
)
from eawf.kernel.migration.epoch2.opt_in import verify_opt_in_backup
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.install.epoch2_birth import bear_epoch2_tree
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plain epoch-1 tree; returns its ``.ea`` directory."""
    ea = tmp_path / "repo" / ".ea"
    ea.mkdir(parents=True)
    (ea / "state.json").write_text(json.dumps({"schema_version": "1.0", "scope_kind": "repo"}))
    monkeypatch.setenv("EA_STATE", str(ea / "state.json"))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return ea


def _opt_in(*argv: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "migrate", "epoch2", "--opt-in", *argv])
    return result.exit_code, json.loads(result.output)


def test_opt_in_pins_a_backup_the_apply_verifies(tree: Path) -> None:
    code, envelope = _opt_in("--target-root", str(tree), "--sealed-by", "alice")
    assert code == 0, envelope
    declaration = read_declaration(tree)
    assert isinstance(declaration, OptInDeclaration)
    assert declaration.declared_by == "alice"
    assert envelope["result"]["backup_ts"] == declaration.backup_ts
    assert envelope["result"]["declaration"] == str(opt_in_path(tree))
    verified = verify_opt_in_backup(DisposableTarget.require(tree))
    assert verified is not None
    assert verified.digest == declaration.backup_digest
    # Opting in is not the cutover: the tree stays at epoch 1 until the apply.
    assert resolve_authority(tree).epoch == 1


def test_opt_in_without_a_target_root_is_a_usage_error(tree: Path) -> None:
    code, envelope = _opt_in()
    assert code == exit_codes.USER_ERROR
    assert "--opt-in requires --target-root" in str(envelope["message"])
    assert not opt_in_path(tree).exists()


def test_opt_in_with_a_second_mode_is_refused(tree: Path) -> None:
    code, envelope = _opt_in("--plan", "--target-root", str(tree))
    assert code == exit_codes.USER_ERROR
    assert "pass exactly one of" in str(envelope["message"])
    assert not opt_in_path(tree).exists()


def test_opt_in_where_no_tree_exists_names_the_missing_document(tmp_path: Path) -> None:
    empty = tmp_path / "empty" / ".ea"
    empty.mkdir(parents=True)
    code, envelope = _opt_in("--target-root", str(empty))
    assert code == exit_codes.USER_ERROR
    assert envelope["data"]["kind"] == "NotFound"
    assert not opt_in_path(empty).exists()


def test_opt_in_refuses_a_tree_that_declared_itself_disposable(tree: Path) -> None:
    declaration_path(tree).write_text(
        json.dumps({"disposable": True, "declared_by": "t", "purpose": "rehearsal"})
    )
    code, envelope = _opt_in("--target-root", str(tree))
    assert code == exit_codes.VALIDATION_ERROR
    assert "migration_target_not_disposable" in str(envelope["message"])
    assert not opt_in_path(tree).exists()


def test_opt_in_refuses_a_tree_already_cut_over(tree: Path) -> None:
    bear_epoch2_tree(tree / "state.json", project_code="QR", born_at=datetime.now(UTC))
    assert (tree / GENERATIONS_DIRNAME / MARKER_FILENAME).is_file()
    before = opt_in_path(tree).read_bytes()
    code, envelope = _opt_in("--target-root", str(tree))
    assert code == exit_codes.VALIDATION_ERROR
    assert "migration_already_cut_over" in str(envelope["message"])
    assert opt_in_path(tree).read_bytes() == before


def test_opt_in_refuses_an_empty_principal(tree: Path) -> None:
    code, envelope = _opt_in("--target-root", str(tree), "--sealed-by", "")
    assert code == exit_codes.VALIDATION_ERROR
    assert "invalid opt-in declaration" in str(envelope["message"])
    assert not opt_in_path(tree).exists()
