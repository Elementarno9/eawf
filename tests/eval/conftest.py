"""Shared pytest fixtures for the skill eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.skills.engine import SkillContext


@pytest.fixture
def eval_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``EA_STATE`` and ``EA_INSTRUMENT_PROBE`` under *tmp_path*."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


@pytest.fixture
def eval_ctx() -> SkillContext:
    """Canonical :class:`SkillContext` used by every eval-harness case."""
    return SkillContext(
        scope="urn:eawf:v1:state:EVL/P00",
        session="urn:eawf:v1:store:EVL/sessions/SES-eval",
    )
