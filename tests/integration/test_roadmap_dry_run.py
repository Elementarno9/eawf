"""``roadmap propose --dry-run`` unit tests (P30-I23-W46, SKH-8b CR-02).

Pin the dry-run contract: ``roadmap propose --dry-run`` renders the plan text
plus the EAWF022 coverage lint WITHOUT any state mutation -- ``state.json``
and the event store are byte-identical before and after. The test drives the
CLI in-process against a ``tmp_path`` ``EA_STATE`` sandbox (never the live
repo ``.ea/``) so the byte-identity assertion is honest and isolated.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace with EA_STATE inside it + a fresh project init."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant Research", "--domains", "quant"],
    )
    assert res.exit_code == 0, res.output
    yield tmp_path


def _state_bytes(workspace: Path) -> bytes:
    return (workspace / ".ea" / "state.json").read_bytes()


def _events_bytes(workspace: Path) -> bytes:
    path = workspace / ".ea" / "store" / "event.jsonl"
    return path.read_bytes() if path.exists() else b""


def test_roadmap_dry_run_leaves_state_byte_identical(workspace: Path) -> None:
    """``--dry-run`` renders the plan but never writes state.json / event store."""
    before_state = _state_bytes(workspace)
    before_events = _events_bytes(workspace)

    res = runner.invoke(
        app,
        ["--json", "roadmap", "propose", "--phase", "P21", "--title", "Test phase", "--dry-run"],
    )

    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["dry_run"] is True
    # The plan text + advisory coverage lint still render.
    assert body["plan_text"]
    assert "P21" in body["plan_text"]
    assert body["coverage_gaps"] == []
    # No mutation: the phase was NOT persisted and the files are byte-identical.
    assert body["phase_id"] == "P21"
    assert _state_bytes(workspace) == before_state
    assert _events_bytes(workspace) == before_events
    state = orjson.loads(_state_bytes(workspace))
    assert "P21" not in state["phases"]
    assert "P21-I01" not in state["iters"]


def test_roadmap_dry_run_contrasts_with_persisting_propose(workspace: Path) -> None:
    """Without ``--dry-run`` the same propose DOES persist the PLANNED phase."""
    before_state = _state_bytes(workspace)

    res = runner.invoke(
        app,
        ["--json", "roadmap", "propose", "--phase", "P21", "--title", "Test phase"],
    )

    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["dry_run"] is False
    assert _state_bytes(workspace) != before_state
    state = orjson.loads(_state_bytes(workspace))
    assert state["phases"]["P21"]["status"] == "planned"


def test_roadmap_dry_run_parses_criteria_from_brief(workspace: Path) -> None:
    """``--criteria-from-brief`` is parsed + surfaced without mutating state."""
    brief = workspace / "brief.md"
    brief.write_text("Implement the parser tokeniser module.\n", encoding="utf-8")
    before_state = _state_bytes(workspace)

    res = runner.invoke(
        app,
        [
            "--json",
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Test phase",
            "--criteria-from-brief",
            str(brief),
            "--dry-run",
        ],
    )

    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["criteria_from_brief"] == str(brief)
    assert body["dry_run"] is True
    assert _state_bytes(workspace) == before_state


def test_roadmap_dry_run_criteria_from_brief_missing_file_rejected(workspace: Path) -> None:
    """A ``--criteria-from-brief`` path that does not exist is rejected at parse."""
    before_state = _state_bytes(workspace)
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Test phase",
            "--criteria-from-brief",
            str(workspace / "absent.md"),
            "--dry-run",
        ],
    )
    assert res.exit_code != 0
    assert _state_bytes(workspace) == before_state


def test_roadmap_dry_run_from_plan_leaves_state_byte_identical(workspace: Path) -> None:
    """``propose --from-plan --dry-run`` renders the staged DAG without persisting."""
    plan_path = workspace / "roadmap-plan.yaml"
    plan_path.write_text(
        """
schema_version: "1.0"
kind: RoadmapPlan
phase:
  id: P22
  title: Plan import
iters:
  - id: P22-I01
    title: First iter
    waves:
      - id: P22-I01-W01
        title: "First wave"
        file_scopes:
          - src/a
        agent_role: executor
        effort_bucket: XS
        intent:
          problem: first wave needs staging
          desired_outcome: first wave is planned
          priority_rationale: stage the leaf wave first
""".lstrip(),
        encoding="utf-8",
    )
    before_state = _state_bytes(workspace)
    before_events = _events_bytes(workspace)

    res = runner.invoke(
        app,
        ["--json", "roadmap", "propose", "--from-plan", str(plan_path), "--dry-run"],
    )

    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["dry_run"] is True
    assert body["wave_count"] == 1
    assert _state_bytes(workspace) == before_state
    assert _events_bytes(workspace) == before_events
    state = orjson.loads(_state_bytes(workspace))
    assert "P22" not in state["phases"]
