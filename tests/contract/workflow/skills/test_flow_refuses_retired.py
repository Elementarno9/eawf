"""``eawf flow run`` refuses retired skills and names their successors.

The catalog retires ``/flow`` and four of its five steps. A flow run must
refuse before any step runs, and the refusal must name each successor. The
registry lookup every skill-dispatching surface goes through must refuse a
retired name even when the retired class is registered, and the skill
bootstrap must not import a retired skill's module.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf.workflow.skills._bootstrap as bootstrap_module
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.skills import flow as flow_module
from eawf.workflow.skills import registry
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalog, SkillRetiredError
from eawf.workflow.skills.flow import FlowRetiredError, FlowSkill, check_flow_runnable

_RETIRED_FLOW_NAMES = ("flow", "prep", "audit", "polish", "ship")


@pytest.fixture
def cli_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    (state_dir / "store").mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_path


@pytest.fixture
def step_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _record(skill: Any, ctx: Any) -> Any:
        calls.append(str(skill.name))
        raise AssertionError(f"retired flow ran {skill.name}")

    monkeypatch.setattr(flow_module, "run_skill", _record)
    monkeypatch.setattr("eawf.workflow.skills.engine.run_skill", _record)
    return calls


def test_check_flow_runnable_names_every_retired_step_successor() -> None:
    with pytest.raises(FlowRetiredError) as excinfo:
        check_flow_runnable()

    assert [row.skill_id for row in excinfo.value.rows] == list(_RETIRED_FLOW_NAMES)
    message = str(excinfo.value)
    for name in _RETIRED_FLOW_NAMES:
        row = SKILL_CATALOG.retired_row(name)
        assert row is not None
        assert f"skill '/{name}' is retired; use {row.successor_text()} instead" in message


def test_check_flow_runnable_single_retired_step_names_only_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = SKILL_CATALOG.retired_row("ship")
    assert ship is not None
    monkeypatch.setattr(
        flow_module,
        "SKILL_CATALOG",
        SkillCatalog(entries=SKILL_CATALOG.entries, retired=(ship,)),
    )

    with pytest.raises(FlowRetiredError) as excinfo:
        check_flow_runnable()

    assert excinfo.value.rows == (ship,)
    assert str(excinfo.value) == str(SkillRetiredError(ship))


def test_check_flow_runnable_empty_retirement_set_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        flow_module, "SKILL_CATALOG", SkillCatalog(entries=SKILL_CATALOG.entries, retired=())
    )

    assert check_flow_runnable() is None


def test_flow_run_refuses_before_any_step_runs(cli_state: Path, step_calls: list[str]) -> None:
    result = CliRunner().invoke(app, ["--json", "flow", "run", "--topic", "demo"])

    assert result.exit_code == UserError.exit_code, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["kind"] == "InvalidInput"
    for successor in ("/dispatch", "/plan", "/verify", "/release"):
        assert successor in payload["message"]
    assert step_calls == []
    assert not (cli_state.parent / "store" / "flow.jsonl").exists()


def test_flow_run_resume_refuses_before_any_step_runs(
    cli_state: Path, step_calls: list[str]
) -> None:
    result = CliRunner().invoke(app, ["--json", "flow", "run", "--resume"])

    assert result.exit_code == UserError.exit_code, result.output
    assert "skill '/flow' is retired; use /dispatch instead" in result.stdout
    assert step_calls == []


def test_flow_run_stop_after_research_still_refuses(cli_state: Path, step_calls: list[str]) -> None:
    result = CliRunner().invoke(app, ["--json", "flow", "run", "--stop-after", "research"])

    assert result.exit_code == UserError.exit_code, result.output
    assert step_calls == []


@pytest.mark.parametrize("row", SKILL_CATALOG.retired, ids=lambda row: row.skill_id)
def test_lookup_refuses_every_retired_name(row: Any) -> None:
    with pytest.raises(SkillRetiredError, match=f"skill '/{row.skill_id}' is retired"):
        registry.lookup(f"/{row.skill_id}")


def test_lookup_refuses_a_registered_retired_class() -> None:
    assert FlowSkill.name == "/flow"
    assert registry.list_registered().get("/flow") is FlowSkill

    with pytest.raises(SkillRetiredError, match="use /dispatch instead"):
        registry.lookup("/flow")


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
