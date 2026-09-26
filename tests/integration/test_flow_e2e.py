"""End-to-end integration test for the ``/flow`` skill.

The acceptance contract runs ``/flow "demo"`` against a tmp Eä repo and
collects envelopes for all five current core skills.

``/flow`` is retired from the operator skill surface (``eawf skill run
/flow`` refuses and names ``/dispatch``), so the test drives the engine
directly through :func:`run_skill`, exercising the engine + body wiring +
the meta skill's own short-circuit logic.

Marked ``integration`` so the test runs under both the default suite
and ``pytest -m integration``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.flow import FlowSkill


@pytest.fixture
def integration_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal .ea/ skeleton so the meta skill's emit_event path
    can land its events.jsonl entries on disk."""
    repo = tmp_path / "repo"
    state_dir = repo / ".ea"
    store_dir = state_dir / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    # The W02/W03 skills don't require a populated state.json. The
    # resolver does, so drop a placeholder.
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return repo


@pytest.mark.integration
def test_flow_demo_runs_six_core_skills(integration_repo: Path) -> None:
    env = run_skill(
        FlowSkill(),
        SkillContext(
            scope="urn:eawf:v1:state:cli-skill-run",
            session="urn:eawf:v1:store:cli/sessions/SES-skill-run",
            args={"topic": "demo", "advance_after": True},
        ),
    )
    assert env.header.skill == "/flow"
    assert env.header.status == "ok"

    body = FlowBody.model_validate(env.body)
    assert body.topic == "demo"
    assert body.terminal_status == "ok"
    # Five core-skill envelopes collected, in canonical order.
    assert len(body.steps) == 5
    assert [s["header"]["skill"] for s in body.steps] == [
        "/research",
        "/prep",
        "/audit",
        "/polish",
        "/ship",
    ]
    # Every step's own status is ok in the v0.1 happy path.
    for step in body.steps:
        assert step["header"]["status"] == "ok"

    # Events from both the flow itself and the inner skills land in the
    # repo's events.jsonl.
    events_path = integration_repo / ".ea" / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Lower bound: 5 core skills emit several events each + the flow's
    # own start/end + 2 per step (start, end). Verify the canonical flow
    # events are present.
    seen_event_types: set[str] = set()
    for ln in lines:
        rec = json.loads(ln)
        seen_event_types.add(rec["payload"].get("event_type", ""))
    assert "flow.start" in seen_event_types
    assert "flow.end" in seen_event_types
    assert "flow.step_start" in seen_event_types
    assert "flow.step_end" in seen_event_types
