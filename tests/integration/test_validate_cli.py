"""End-to-end tests for ``eawf validate`` against documents it does not validate.

A 0.6 ``state.json`` is neither a generation document nor an output envelope,
so the verb refuses it with the upgrade path instead of guessing a mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.upgrade_refusal import UPGRADE_STEPS

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


@pytest.mark.parametrize("name", ["01-empty-repo.json", "03-phase-iter-wave-active.json"])
def test_validate_refuses_an_epoch1_document_with_the_upgrade_path(name: str) -> None:
    result = runner.invoke(app, ["validate", str(FIXTURES / "valid" / name)])
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert "neither a generation document nor an output envelope" in result.output
    assert UPGRADE_STEPS in result.output


def test_validate_refuses_an_empty_object(tmp_path: Path) -> None:
    target = tmp_path / "empty.json"
    target.write_text("{}")
    result = runner.invoke(app, ["validate", str(target)])
    assert result.exit_code == exit_codes.VALIDATION_ERROR


def test_validate_refuses_a_top_level_list(tmp_path: Path) -> None:
    target = tmp_path / "list.json"
    target.write_text("[]")
    result = runner.invoke(app, ["validate", str(target)])
    assert result.exit_code == exit_codes.VALIDATION_ERROR
    assert "top-level value must be a JSON object" in result.output


def test_validate_missing_path_exits_nonzero() -> None:
    """A nonexistent file must be a hard error (Typer/Click reports != 0)."""
    result = runner.invoke(app, ["validate", "/no/such/path.json"])
    assert result.exit_code != 0
