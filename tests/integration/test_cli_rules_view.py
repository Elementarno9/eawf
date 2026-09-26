"""``eawf rules view`` prints a module view the sync render wrote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.platform.rules.render import read_rule_view, render_rule_projections
from eawf.platform.rules.views import view_target
from eawf.surfaces.cli.app import app

_TEST = "eawf.craft.test"

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        yaml.safe_dump({"schema_version": 1, "modules": [_TEST], "rules": []}), encoding="utf-8"
    )
    return root


def _view(root: Path, reference: str) -> Path:
    return root / view_target(reference)


def test_rules_view_cli_prints_a_current_view(repo: Path) -> None:
    render_rule_projections(repo)
    result = runner.invoke(app, ["--json", "--workspace", str(repo), "rules", "view", _TEST])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "current"
    assert payload["text"] == read_rule_view(repo, _TEST).text


def test_rules_view_cli_prints_a_stale_notice_before_the_view(repo: Path) -> None:
    render_rule_projections(repo)
    view = _view(repo, _TEST)
    view.write_text(view.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    result = runner.invoke(app, ["--plain", "--workspace", str(repo), "rules", "view", _TEST])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"{view_target(_TEST)} is stale")


def test_rules_view_cli_absent_view_exits_nonzero_naming_sync(repo: Path) -> None:
    result = runner.invoke(app, ["--workspace", str(repo), "rules", "view", _TEST])
    assert result.exit_code != 0
    assert "eawf sync" in result.output


def test_rules_view_cli_unknown_module_exits_nonzero(repo: Path) -> None:
    result = runner.invoke(app, ["--workspace", str(repo), "rules", "view", "eawf.craft.markdown"])
    assert result.exit_code != 0
    assert "not selected" in result.output


def test_rules_view_cli_without_rule_source_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--workspace", str(tmp_path), "rules", "view", _TEST])
    assert result.exit_code != 0
