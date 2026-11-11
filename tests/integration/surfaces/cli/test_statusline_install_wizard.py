"""``eawf cc statusline install`` stays a hidden alias of ``eawf statusline install``.

``HOME`` points at ``tmp_path`` so no real ``~/.claude`` tree is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

_ENTRY = {"type": "command", "command": "eawf cc statusline", "refreshInterval": 30}


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at *tmp_path* and return its user settings file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".claude" / "settings.json"


def test_alias_installs_like_the_verb(settings_path: Path) -> None:
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")

    first = runner.invoke(app, ["cc", "statusline", "install"])
    second = runner.invoke(app, ["statusline", "install"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert second.stdout == f"statusline already installed: {settings_path}\n"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "model": "opus",
        "statusLine": _ENTRY,
    }


def test_alias_keeps_a_foreign_entry_without_yes(settings_path: Path) -> None:
    settings_path.parent.mkdir(parents=True)
    raw = json.dumps({"statusLine": {"type": "command", "command": "mine"}})
    settings_path.write_text(raw, encoding="utf-8")

    result = runner.invoke(app, ["cc", "statusline", "install"])

    assert result.exit_code == 1
    assert settings_path.read_text(encoding="utf-8") == raw
