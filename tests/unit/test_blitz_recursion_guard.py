"""Unit tests for the ``/blitz`` recursion guard (P14-W11 / D22)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.skills.blitz import (
    BlitzRecursionExhaustedError,
    BlitzSkill,
    bump_depth,
    current_depth,
    depth_cap,
    reset_depth,
    should_auto_invoke,
)
from eawf.skills.bodies.blitz import BlitzBody
from eawf.skills.engine import SkillContext, run_skill


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    yield
    reset_depth()


def test_default_depth_cap_is_eight() -> None:
    assert depth_cap() == 8


def test_depth_cap_honours_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "3")
    assert depth_cap() == 3


def test_depth_cap_falls_back_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "not-a-number")
    assert depth_cap() == 8


def test_bump_depth_increments_counter() -> None:
    assert current_depth() == 0
    assert bump_depth() == 1
    assert bump_depth() == 2
    assert current_depth() == 2


def test_bump_depth_raises_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "2")
    bump_depth()
    bump_depth()
    with pytest.raises(BlitzRecursionExhaustedError):
        bump_depth()


def test_reset_clears_counter() -> None:
    bump_depth()
    reset_depth()
    assert current_depth() == 0


def test_should_auto_invoke_only_when_more_than_one_unknown() -> None:
    assert should_auto_invoke(residual_unknowns=2) is True
    assert should_auto_invoke(residual_unknowns=1) is False
    assert should_auto_invoke(residual_unknowns=0) is False


def test_blitz_skill_registered_with_canonical_name() -> None:
    from eawf.skills import registry

    cls = registry.lookup("/blitz")
    assert cls is BlitzSkill


def test_blitz_skill_returns_followup_research_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    ctx = SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args={"residual_unknowns": 3, "followup_research_args": {"topic": "demo"}},
    )
    env = run_skill(BlitzSkill(), ctx)
    assert env.header.status == "ok"
    body = BlitzBody.model_validate(cast(dict, env.body))
    assert body.depth == 1
    assert body.residual_unknowns == 3
    assert body.followup_research_args["topic"] == "demo"
    assert body.followup_research_args["blitz"] is False


def test_blitz_skill_blocks_when_depth_cap_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "0")
    ctx = SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args={"residual_unknowns": 2},
    )
    env = run_skill(BlitzSkill(), ctx)
    assert env.header.status == "blocked"
    assert env.footer.repair_commands
