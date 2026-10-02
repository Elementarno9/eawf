"""Every skill-dispatching surface refuses a retired skill and names its successor.

The registry lookup every skill-dispatching surface goes through must refuse a
retired name even when a class still registers it, and the skill bootstrap must
not import a retired skill's module.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf.workflow.skills._bootstrap as bootstrap_module
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills import registry
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillRetiredError
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult


@pytest.fixture
def cli_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    (state_dir / "store").mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_path


@pytest.mark.parametrize("row", SKILL_CATALOG.retired, ids=lambda row: row.skill_id)
def test_lookup_refuses_every_retired_name(row: Any) -> None:
    with pytest.raises(SkillRetiredError, match=f"skill '/{row.skill_id}' is retired"):
        registry.lookup(f"/{row.skill_id}")


def test_lookup_refuses_a_registered_retired_class() -> None:
    class _RetiredFlowSkill(Skill):
        name: SkillName = "/flow"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            return SkillResult(status="ok", body={})

    registry.register(_RetiredFlowSkill)
    try:
        assert registry.list_registered().get("/flow") is _RetiredFlowSkill
        with pytest.raises(SkillRetiredError, match="use /dispatch instead"):
            registry.lookup("/flow")
    finally:
        registry.unregister("/flow")


def test_lookup_catalog_skill_returns_its_class() -> None:
    cls = registry.lookup("/research")

    assert cls is not None
    assert cls.name == "/research"


def test_lookup_unknown_name_returns_none() -> None:
    assert registry.lookup("/no-such-skill") is None


def test_skill_run_refuses_retired_name_naming_successor(cli_state: Path) -> None:
    result = CliRunner().invoke(app, ["--json", "skill", "run", "/ship"])

    assert result.exit_code == UserError.exit_code, result.output
    assert "use /release instead" in result.stdout


def test_bootstrap_imports_only_catalog_skill_modules() -> None:
    tree = ast.parse(Path(bootstrap_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.replace("_", "-")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "eawf.workflow.skills"
        for alias in node.names
    }
    catalog_ids = {entry.skill_id for entry in SKILL_CATALOG.entries}
    retired_ids = {row.skill_id for row in SKILL_CATALOG.retired}

    assert imported
    assert imported <= catalog_ids
    assert not imported & retired_ids


@pytest.mark.parametrize("module", ["audit", "flow", "prep", "review", "ship"])
def test_a_retired_skill_body_nothing_dispatches_is_gone(module: str) -> None:
    assert SKILL_CATALOG.retired_row(module) is not None
    assert importlib.util.find_spec(f"eawf.workflow.skills.{module}") is None
