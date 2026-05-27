"""Workflow-scope tests for deep ``/research`` planning."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.surfaces.render.envelope import to_markdown
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.research import ResearchSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    return state_dir


def test_research_deep_emits_typed_research_plan(state_dir: Path) -> None:
    ctx = SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args={"depth": "deep", "topic": "dispatch planning"},
    )

    env = run_skill(ResearchSkill(), ctx)

    assert env.header.status == "ok"
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert body.user_question is None
    assert body.research_plan is not None
    assert body.research_plan.section_heading == "## ResearchPlan"
    assert body.research_plan.topic == "dispatch planning"
    assert len(body.research_plan.fanout_envelopes) == len(body.questions) == 3
    assert [e.agent_role for e in body.research_plan.fanout_envelopes] == [
        "researcher",
        "researcher",
        "researcher",
    ]

    markdown = to_markdown(env)
    assert "## ResearchPlan" in markdown
    assert "fanout_envelopes:" in markdown
