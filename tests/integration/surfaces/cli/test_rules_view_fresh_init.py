"""``eawf rules view`` works on a tree ``eawf init --quick`` just created.

A fresh tree has no ``.ea/rules.yaml``, so the view is read from the
built-in catalogue. The assertions take the expected rule titles from the
catalogue itself, so the test does not pin any rule's wording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.platform.rules.loader import RULE_SOURCE_PATH
from eawf.platform.rules.modules import builtin_rule_modules
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

_INTEGRITY = "eawf.core.integrity"

runner = CliRunner()


@pytest.fixture
def fresh_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository ``eawf init --quick`` initialised under a tmp home."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--quick", "--target", str(repo)])
    assert result.exit_code == exit_codes.OK, result.output
    assert (repo / ".ea").is_dir()
    assert not repo.joinpath(*RULE_SOURCE_PATH.parts).exists()
    return repo


def _view(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--workspace", str(repo), "rules", "view", _INTEGRITY])
    return result.exit_code, result.output


def test_rules_view_on_a_fresh_tree_prints_the_builtin_module(fresh_tree: Path) -> None:
    exit_code, output = _view(fresh_tree, "--plain")
    assert exit_code == exit_codes.OK, output
    titles = [record.title for record in builtin_rule_modules()[_INTEGRITY].records]
    assert titles
    for title in titles:
        assert f"## {title}" in output
    assert "built-in catalogue" in output.splitlines()[0]


def test_rules_view_on_a_fresh_tree_reports_builtin_status_in_json(fresh_tree: Path) -> None:
    exit_code, output = _view(fresh_tree, "--json")
    assert exit_code == exit_codes.OK, output
    payload = json.loads(output)
    assert payload["reference"] == _INTEGRITY
    assert payload["status"] == "builtin"
    assert RULE_SOURCE_PATH.as_posix() in payload["message"]
    assert payload["text"].startswith("# ")


@pytest.mark.parametrize("reference", ["eawf.core.no-such-module", "", "eawf"])
def test_rules_view_unknown_module_on_a_fresh_tree_is_not_found(
    fresh_tree: Path, reference: str
) -> None:
    result = runner.invoke(
        app, ["--json", "--workspace", str(fresh_tree), "rules", "view", reference]
    )
    assert result.exit_code == exit_codes.USER_ERROR == 1, result.output
    payload = json.loads(result.output)
    assert payload["data"]["kind"] == "NotFound"
    assert "built-in catalogue" in payload["message"]
