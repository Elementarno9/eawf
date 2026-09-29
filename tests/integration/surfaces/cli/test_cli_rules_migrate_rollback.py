"""``eawf rules migrate`` and ``eawf rules rollback`` dispatch to the library.

RULE-090: ``migrate`` fails naming each item without exactly one disposition.
RULE-073: ``rollback`` re-selects a complete stored generation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from eawf.platform.rules.render import POLICY_TARGET, render_rule_projections
from eawf.surfaces.cli.app import app

runner = CliRunner()


def _source(root: Path, *, legacy: list[dict[str, Any]] | None = None, extra: bool = False) -> None:
    rules = [
        {
            "rule_id": f"repo.{name}",
            "obligation_id": f"demo.{name}",
            "revision": 1,
            "title": f"Demo rule {name}",
            "zone": "steering",
            "force": "should",
            "effectiveness": "behavioral",
            "instruction": f"Apply the {name} convention to every module.",
            "verification": {"method": "review"},
        }
        for name in (("naming", "layout") if extra else ("naming",))
    ]
    document: dict[str, Any] = {"schema_version": 1, "modules": [], "rules": rules}
    if legacy is not None:
        document["legacy"] = legacy
    path = root / ".ea" / "rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _source(root)
    config = {"schema_version": "1.0", "profiles": {"enabled": ["python"]}}
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def test_rule_090_migrate_cli_names_every_undisposed_block(repo: Path) -> None:
    result = runner.invoke(app, ["--workspace", str(repo), "rules", "migrate"])
    assert result.exit_code != 0
    assert "undisposed: block:python-style" in result.output
    assert "undisposed: block:test-discipline" in result.output


def test_rule_090_migrate_cli_passes_when_every_item_is_disposed(repo: Path) -> None:
    legacy = [
        {
            "item": "block:python-style",
            "disposition": "duplicate_of",
            "obligation_id": "demo.naming",
        },
        {"item": "block:test-discipline", "disposition": "rejected"},
    ]
    _source(repo, legacy=legacy)
    result = runner.invoke(app, ["--json", "--workspace", str(repo), "rules", "migrate"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["complete"] is True
    assert payload["undisposed"] == []


def test_rule_073_rollback_cli_selects_the_previous_generation(repo: Path) -> None:
    first = render_rule_projections(repo)
    policy = (repo / POLICY_TARGET).read_bytes()
    _source(repo, extra=True)
    render_rule_projections(repo)
    result = runner.invoke(app, ["--json", "--workspace", str(repo), "rules", "rollback"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["manifest"]["generation"] == first.manifest.generation
    assert (repo / POLICY_TARGET).read_bytes() == policy


def test_rule_073_rollback_cli_without_a_previous_generation_exits_nonzero(repo: Path) -> None:
    render_rule_projections(repo)
    result = runner.invoke(app, ["--workspace", str(repo), "rules", "rollback"])
    assert result.exit_code != 0
    assert "no stored rule generation" in result.output
