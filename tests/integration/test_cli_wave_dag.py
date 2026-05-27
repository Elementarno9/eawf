"""Integration tests for the wave-DAG read-only CLI verbs (B026).

Exercises ``eawf wave graph`` and ``eawf wave next-ready`` against a
temp ``.ea/state.json`` via :class:`typer.testing.CliRunner`. Mutating
verbs are tested separately in :mod:`tests.integration.test_cli_lifecycle`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _bootstrap_chain(workspace: Path) -> None:
    """Init QR, open P01-I01, plan W01->W02->W03 linear chain."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    for wid, deps in (
        ("P01-I01-W01", None),
        ("P01-I01-W02", "P01-I01-W01"),
        ("P01-I01-W03", "P01-I01-W02"),
    ):
        args = [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            wid,
            "--title",
            f"title-{wid}",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ]
        if deps is not None:
            args.extend(["--deps", deps])
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.stdout


# ---- wave graph -------------------------------------------------------------


def test_wave_graph_topo_order(workspace: Path) -> None:
    """Linear chain W01->W02->W03 prints in topo order with increasing depth."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["--json", "wave", "graph"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I01"
    ids = [row["id"] for row in payload["waves"]]
    depths = [row["depth"] for row in payload["waves"]]
    assert ids == ["P01-I01-W01", "P01-I01-W02", "P01-I01-W03"]
    assert depths == [0, 1, 2]
    # blocks / blocked_by symmetry surfaces in the payload.
    by_id = {row["id"]: row for row in payload["waves"]}
    assert by_id["P01-I01-W01"]["blocks"] == ["P01-I01-W02"]
    assert by_id["P01-I01-W01"]["blocked_by"] == []
    assert by_id["P01-I01-W02"]["blocks"] == ["P01-I01-W03"]
    assert by_id["P01-I01-W02"]["blocked_by"] == ["P01-I01-W01"]
    assert by_id["P01-I01-W03"]["blocks"] == []
    assert by_id["P01-I01-W03"]["blocked_by"] == ["P01-I01-W02"]


def test_wave_graph_text_indents_by_depth(workspace: Path) -> None:
    """Text output indents two spaces per depth level."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "graph"])
    assert res.exit_code == 0, res.stdout
    lines = res.stdout.splitlines()
    # Expect three lines, ordered W01, W02, W03 with 0/2/4 leading spaces.
    assert lines[0].startswith("⏳ P01-I01-W01")  # depth 0
    assert lines[1].startswith("  ⏳ P01-I01-W02")  # depth 1 → 2 spaces
    assert lines[2].startswith("    ⏳ P01-I01-W03")  # depth 2 → 4 spaces


def test_wave_graph_no_current_iter_no_flag_exits_invalid_input(workspace: Path) -> None:
    """No --iter and no current iter pointer → exit 3 (INVALID_INPUT)."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    # No phase / iter opened ⇒ state.current.iter_id is None.
    res = runner.invoke(app, ["wave", "graph"])
    assert res.exit_code == 1, res.stdout
    assert "state.current.iter_id" in res.stdout


def test_wave_graph_unknown_iter_exits_invalid_input(workspace: Path) -> None:
    """An --iter flag pointing at a non-existent iter exits 3."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "graph", "--iter", "P99-I99"])
    assert res.exit_code == 1, res.stdout


def test_wave_graph_empty_iter(workspace: Path) -> None:
    """An iter with no waves prints the empty-iter human banner and empty JSON list."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    res = runner.invoke(app, ["--json", "wave", "graph"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["waves"] == []


# ---- wave next-ready --------------------------------------------------------


def test_wave_next_ready_when_dep_closed(workspace: Path) -> None:
    """Closing W01 should make W02 ready (its only dep is now closed)."""
    _bootstrap_chain(workspace)
    # Initially only W01 is ready (no deps).
    res = runner.invoke(app, ["--json", "wave", "next-ready"])
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["ready"] == ["P01-I01-W01"]
    # Claim + close W01.
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["wave", "close", "P01-I01-W01", "--outcome", "done"],
        ).exit_code
        == 0
    )
    res = runner.invoke(app, ["--json", "wave", "next-ready"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["ready"] == ["P01-I01-W02"]
    assert payload["blocked_by_failure"] == []


def test_wave_next_ready_excludes_failure_blocked(workspace: Path) -> None:
    """A pending wave whose dep failed must NOT appear in ready."""
    _bootstrap_chain(workspace)
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["wave", "fail", "P01-I01-W01", "--reason", "broken"],
        ).exit_code
        == 0
    )
    res = runner.invoke(app, ["--json", "wave", "next-ready"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["ready"] == []
    assert payload["blocked_by_failure"] == ["P01-I01-W02"]


def test_wave_next_ready_empty_when_nothing_pending(workspace: Path) -> None:
    """No pending waves at all ⇒ ready is empty (exit still 0)."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    res = runner.invoke(app, ["--json", "wave", "next-ready"])
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["ready"] == []


def test_wave_next_ready_uses_iter_flag(workspace: Path) -> None:
    """--iter overrides state.current.iter_id."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["--json", "wave", "next-ready", "--iter", "P01-I01"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["iter"] == "P01-I01"
    assert payload["ready"] == ["P01-I01-W01"]


# ---- plan-side cycle / self-dep refused at the CLI seam --------------------


def test_wave_plan_self_dep_exits_invalid_input(workspace: Path) -> None:
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W04",
            "--title",
            "x",
            "--files",
            "src/",
            "--deps",
            "P01-I01-W04",
            "--effort-bucket",
            "M",
        ],
    )
    assert res.exit_code == 1, res.stdout
    assert "cannot depend on itself" in res.stdout


def test_wave_plan_blocks_index_persists_to_disk(workspace: Path) -> None:
    """After plan, the dep wave's blocks list lands in state.json on disk."""
    _bootstrap_chain(workspace)
    state = json.loads((workspace / ".ea" / "state.json").read_bytes())
    assert state["waves"]["P01-I01-W01"]["blocks"] == ["P01-I01-W02"]
    assert state["waves"]["P01-I01-W02"]["blocks"] == ["P01-I01-W03"]
    assert state["waves"]["P01-I01-W03"]["blocks"] == []
