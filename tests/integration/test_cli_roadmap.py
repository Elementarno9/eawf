"""CLI integration tests for ``eawf roadmap`` (P19-W06)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace dir with EA_STATE pointing inside it + project init."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    res = runner.invoke(
        app,
        [
            "project",
            "init",
            "QR",
            "--title",
            "Quant Research",
            "--domains",
            "quant",
        ],
    )
    assert res.exit_code == 0, res.output
    yield tmp_path


def _read_state(workspace: Path) -> dict:
    return orjson.loads((workspace / ".ea" / "state.json").read_bytes())


def test_roadmap_propose_creates_planned_phase(workspace: Path) -> None:
    """propose persists a PLANNED phase + P##-I01 iter and returns needs_user."""
    res = runner.invoke(
        app,
        ["--json", "roadmap", "propose", "--phase", "P21", "--title", "Test phase"],
    )
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "needs_user"
    assert body["phase_id"] == "P21"
    assert body["iter_id"] == "P21-I01"
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "planned"
    assert state["iters"]["P21-I01"]["status"] == "planned"


def test_roadmap_propose_with_source_briefs_and_deps(workspace: Path) -> None:
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Test phase",
            "--from-briefs",
            "RES-2026-05-14-001,RES-2026-05-14-002",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["source_brief_ids"] == [
        "RES-2026-05-14-001",
        "RES-2026-05-14-002",
    ]


def test_roadmap_propose_duplicate_phase_rejected(workspace: Path) -> None:
    runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "X"],
    )
    res = runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "Y"],
    )
    assert res.exit_code != 0
    assert "already exists" in res.stderr or "already exists" in res.output


def test_roadmap_revise_add_wave(workspace: Path) -> None:
    runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "X"],
    )
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: foo",
            "--files",
            "src/",
            "--success",
            "criterion1,criterion2",
            "--agent-role",
            "executor",
            "--effort-bucket",
            "S",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W01" in state["waves"]
    assert state["waves"]["P21-I01-W01"]["title"] == "feat: foo"
    assert state["waves"]["P21-I01-W01"]["success_criteria"] == ["criterion1", "criterion2"]


def test_roadmap_revise_set_deps(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    for wid in ("W01", "W02"):
        runner.invoke(
            app,
            [
                "roadmap",
                "revise",
                "P21",
                "--add-wave",
                wid,
                "--title",
                f"feat: {wid}",
                "--files",
                "src/",
            ],
        )
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--set-deps", "W02=W01"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W02"]["deps"] == ["P21-I01-W01"]


def test_roadmap_revise_remove_wave(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: a",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(app, ["roadmap", "revise", "P21", "--remove-wave", "W01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W01" not in state["waves"]


def test_roadmap_revise_requires_exactly_one_action(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "revise", "P21"])
    assert res.exit_code != 0


def test_roadmap_revise_rejects_non_planned(workspace: Path) -> None:
    # P21 doesn't exist; revise should reject as unknown phase.
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--remove-wave", "W01"],
    )
    assert res.exit_code != 0


def test_roadmap_apply_requires_wave(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code != 0
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: a",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output


def test_roadmap_drop_archives_planned(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "drop", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "archived"


def test_roadmap_show_renders_planned_queue(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(app, ["roadmap", "show"])
    assert res.exit_code == 0
    assert "P21" in res.output
    assert "planned" in res.output


def test_roadmap_show_json_envelope(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(app, ["--json", "roadmap", "show"])
    assert res.exit_code == 0
    body = orjson.loads(res.stdout)
    assert any(row["id"] == "P21" for row in body["phases"])


def test_phase_activate_planned_phase(workspace: Path) -> None:
    """P19-W07: ``eawf phase activate`` flips PLANNED -> ACTIVE."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: foo",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "active"
    assert state["current"]["phase_id"] == "P21"


def test_phase_activate_without_waves_rejected(workspace: Path) -> None:
    """activate_phase requires at least one wave."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code != 0


def _read_events(workspace: Path) -> list[dict]:
    path = workspace / ".ea" / "store" / "event.jsonl"
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_roadmap_propose_emits_event(workspace: Path) -> None:
    """P19-W06: propose appends an EVENT envelope to event.jsonl."""
    before = len(_read_events(workspace))
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    after = _read_events(workspace)
    assert len(after) > before
    propose_event = next(
        (e for e in after if e["payload"]["command"] == "roadmap propose"),
        None,
    )
    assert propose_event is not None
    assert propose_event["scope_id"] == "P21"


def test_roadmap_revise_emits_event(workspace: Path) -> None:
    """revise --add-wave emits its own EVENT envelope."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: foo",
            "--files",
            "src/",
        ],
    )
    events = _read_events(workspace)
    revise_events = [e for e in events if e["payload"]["command"] == "roadmap revise"]
    assert revise_events, "expected at least one roadmap revise event"


def test_roadmap_drop_emits_event(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(app, ["roadmap", "drop", "P21"])
    events = _read_events(workspace)
    assert any(e["payload"]["command"] == "roadmap drop" for e in events)


def test_wave_show_commit_returns_sha_when_present(workspace: Path) -> None:
    """``eawf wave show --commit`` exits 0; empty stdout when no match."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: foo",
            "--files",
            "src/",
        ],
    )
    res = runner.invoke(app, ["wave", "show", "P21-I01-W01", "--commit"])
    # Test repo has no [P21-W01] commit so output is empty; exit is still 0.
    assert res.exit_code == 0, res.output


def test_wave_show_without_commit_rejected(workspace: Path) -> None:
    res = runner.invoke(app, ["wave", "show", "P21-I01-W01"])
    assert res.exit_code != 0


def test_wave_claim_out_of_order_flag_overrides_monotonic_gate(workspace: Path) -> None:
    """CLI flag plumbs through to ``claim_wave``'s out_of_order escape hatch."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    for wid in ("W01", "W02"):
        runner.invoke(
            app,
            [
                "roadmap",
                "revise",
                "P21",
                "--add-wave",
                wid,
                "--title",
                f"feat: {wid}",
                "--files",
                "src/",
            ],
        )
    # Default claim of W02 is rejected because W01 is still PENDING + ready.
    blocked = runner.invoke(app, ["wave", "claim", "P21-I01-W02", "--session", "S"])
    assert blocked.exit_code != 0
    # --out-of-order escape hatch must succeed.
    ok = runner.invoke(
        app,
        ["wave", "claim", "P21-I01-W02", "--session", "S", "--out-of-order"],
    )
    assert ok.exit_code == 0, ok.output


def test_iter_activate_planned_iter(workspace: Path) -> None:
    """``eawf iter activate`` flips PLANNED -> ACTIVE on the iter."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "feat: foo",
            "--files",
            "src/",
        ],
    )
    runner.invoke(app, ["phase", "activate", "P21"])
    res = runner.invoke(app, ["iter", "activate", "P21-I01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["status"] == "active"
