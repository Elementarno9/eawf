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
