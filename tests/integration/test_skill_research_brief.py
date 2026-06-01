"""Integration test for the production ``/research`` skill.

End-to-end:

- Set up a tmp Eä-shaped repo (.ea/ + state.json + store/event.jsonl
  parent path).
- Drive the skill via ``eawf --json skill run /research`` (the W07 CLI
  surface) so the test exercises the registry + engine + body wiring.
- Assert the envelope shape, parse the body as :class:`ResearchBody`,
  and verify the events.jsonl record count matches the algorithm steps.

Marked ``integration`` so the test runs under both the default suite and
``pytest -m integration``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.skills.bodies.research import ResearchBody


@pytest.fixture
def integration_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal .ea/ skeleton so the skill's emit_event path can land
    its events.jsonl entries on disk."""
    repo = tmp_path / "repo"
    state_dir = repo / ".ea"
    store_dir = state_dir / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    # The W02 skills don't require a populated state.json — they only
    # write to events.jsonl and cache the probe report — but the
    # resolver needs a valid path. Drop a placeholder so the resolver
    # does not climb out of tmp_path.
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return repo


@pytest.mark.integration
def test_skill_research_full_run_persists_events_and_envelope(
    integration_repo: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "skill", "run", "/research"],
        input='{"depth": "medium"}',
    )
    assert result.exit_code == 0, result.stdout

    # Envelope round-trips through the typed model.
    payload = json.loads(result.stdout)
    env = OutputEnvelope.model_validate(payload)
    assert env.header.skill == "/research"
    assert env.header.status == "ok"

    # Body validates against the W01 ResearchBody schema.
    assert isinstance(env.body, dict)
    body = ResearchBody.model_validate(env.body)
    assert body.brief_id.startswith("BR-")
    assert len(body.questions) == 2  # depth=medium -> 2 slots
    assert len(body.options) == 2
    assert body.recommendation is not None
    assert body.recommendation.choice == "option-1"

    # Events were persisted to .ea/store/event.jsonl, one per algorithm
    # step (resolve_scope, start_brief, define_questions,
    # synthesize_options, peer_review, recommend → 6).
    events_path = integration_repo / ".ea" / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    # Each line is a valid Envelope JSON with kind=event.
    for ln in lines:
        record = json.loads(ln)
        assert record["kind"] == "event"
        assert record["payload"]["actor"] == "skill"

    # The envelope's footer mirrors the persisted records list.
    assert len(env.footer.persisted_store_records) == 6
    for rec_id in env.footer.persisted_store_records:
        assert rec_id.startswith("EV-")
