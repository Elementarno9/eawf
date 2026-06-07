"""Tests for the ``eawf cc statusline install`` wizard (P29-I13-W41).

Covers the happy path (a fresh global settings file gets the statusLine key
patched in), idempotence (a re-run on an already-installed file is a no-op),
and the rejection / error paths (declining the confirmation aborts with a
user error; an existing non-JSON settings file is an operator-facing error).
The global settings path is redirected to a tmp file so no real ``~/.claude``
tree is touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.commands import cc as cc_module
from eawf.surfaces.cli.commands import statusline as statusline_module
from eawf.surfaces.cli.commands.cc import cc_app

runner = CliRunner()

_EXPECTED_COMMAND = {"type": "command", "command": "eawf cc statusline"}


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the global settings path to a tmp file."""
    path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(statusline_module, "global_settings_path", lambda: path)
    yield path


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_fresh_settings_writes_statusline(settings_path: Path) -> None:
    # happy path: a fresh install patches the statusLine command in and the
    # command exits 0.
    result = runner.invoke(cc_app, ["statusline", "install", "--yes"])
    assert result.exit_code == 0, result.output
    assert settings_path.is_file()
    assert _read(settings_path)["statusLine"] == _EXPECTED_COMMAND
    assert "installed" in result.output


def test_install_preserves_other_settings_keys(settings_path: Path) -> None:
    # The patch is namespace-narrow: pre-existing keys survive verbatim.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"model": "opus", "theme": "dark"}), encoding="utf-8")
    result = runner.invoke(cc_app, ["statusline", "install", "--yes"])
    assert result.exit_code == 0, result.output
    written = _read(settings_path)
    assert written["model"] == "opus"
    assert written["theme"] == "dark"
    assert written["statusLine"] == _EXPECTED_COMMAND


def test_install_is_idempotent_no_op_on_rerun(settings_path: Path) -> None:
    # boundary: a re-run on an already-installed file is a no-op exit 0.
    first = runner.invoke(cc_app, ["statusline", "install", "--yes"])
    assert first.exit_code == 0
    before = settings_path.read_bytes()
    second = runner.invoke(cc_app, ["statusline", "install", "--yes"])
    assert second.exit_code == 0, second.output
    assert "already installed" in second.output
    assert settings_path.read_bytes() == before  # byte-stable, not rewritten


def test_install_declined_aborts_with_user_error(settings_path: Path) -> None:
    # rejection path: answering "n" at the confirm prompt aborts non-zero
    # without writing the settings file.
    result = runner.invoke(cc_app, ["statusline", "install"], input="n\n")
    assert result.exit_code != 0
    assert not settings_path.exists()


def test_install_confirmed_interactively_writes(settings_path: Path) -> None:
    # happy path via the interactive confirm ("y") rather than --yes.
    result = runner.invoke(cc_app, ["statusline", "install"], input="y\n")
    assert result.exit_code == 0, result.output
    assert _read(settings_path)["statusLine"] == _EXPECTED_COMMAND


def test_install_rejects_invalid_json_settings(settings_path: Path) -> None:
    # error path: a corrupt existing settings file is an operator-facing
    # error, not a silent overwrite.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{ not json", encoding="utf-8")
    result = runner.invoke(cc_app, ["statusline", "install", "--yes"])
    assert result.exit_code != 0
    # The corrupt file is left untouched.
    assert settings_path.read_text(encoding="utf-8") == "{ not json"


def test_wizard_is_wired_into_cc_statusline() -> None:
    # The wizard typer is registered under cc statusline so the subcommand
    # resolves rather than 404-ing.
    assert cc_module.statusline_wizard_app is statusline_module.statusline_wizard_app
