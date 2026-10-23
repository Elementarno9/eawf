"""The statusline draws no budget ceiling from an un-migrated epoch-1 tree's document."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.v1_state import State
from eawf.runtime.runtimes.claude import statusline as statusline_orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import budget as budget_module
from tests.unit.runtime.budget._wave_payload import (
    _WAVE_ID,
    _state_payload,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config layer out of every resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def write_repo(tmp_path: Path, *, budget: int | None, config: str | None = None) -> Path:
    """Write a repo whose active wave carries *budget*; return its state path."""
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["token_budget"] = budget
    state_dir = tmp_path / "repo" / ".ea"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    if config is not None:
        (state_dir / "config.yaml").write_text(config, encoding="utf-8")
    return state_path


def test_statusline_render_pipeline_never_draws_an_epoch1_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=40_000)
    monkeypatch.setattr(statusline_orchestrator, "_safe_resolve_state_path", lambda _w: state_path)
    line = statusline_orchestrator.render_pipeline({}, workspace=None, theme_name="ascii-fallback")
    assert "budget:n/a(epoch1-undeclared)" in line
    assert "60.0k" not in line


def test_budget_module_names_why_it_cannot_draw(tmp_path: Path) -> None:
    assert budget_module.build({}, None).text == "budget:n/a(no-state)"
    epoch1 = write_repo(tmp_path / "a", budget=None)
    assert budget_module.build({}, epoch1).text == "budget:n/a(epoch1-undeclared)"
