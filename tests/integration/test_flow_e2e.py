"""End-to-end integration test for the ``/flow`` skill.

The W03 acceptance contract requires running ``/flow "demo"`` against a
tmp Eä repo and collecting envelopes for all six core skills.

Drives the flow via the W07 CLI surface (``eawf --json skill run /flow``)
so the test exercises the registry + engine + body wiring + the meta
skill's own short-circuit logic.

Marked ``integration`` so the test runs under both the default suite
and ``pytest -m integration``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.skills.bodies.flow import FlowBody


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
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "skill", "run", "/flow"],
        input='{"topic": "demo"}',
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    env = OutputEnvelope.model_validate(payload)
    assert env.header.skill == "/flow"
    assert env.header.status == "ok"

    body = FlowBody.model_validate(env.body)
    assert body.topic == "demo"
    assert body.terminal_status == "ok"
    # Six core-skill envelopes collected, in canonical order.
    assert len(body.steps) == 6
    assert [s["header"]["skill"] for s in body.steps] == [
        "/research",
        "/prep",
        "/audit",
        "/ship",
        "/review",
        "/polish",
    ]
    # Every step's own status is ok in the v0.1 happy path.
    for step in body.steps:
        assert step["header"]["status"] == "ok"

    # Events from both the flow itself and the inner skills land in the
    # repo's events.jsonl.
    events_path = integration_repo / ".ea" / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Lower bound: 6 core skills emit several events each + the flow's
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
