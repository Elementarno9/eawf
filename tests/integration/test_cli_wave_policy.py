"""Integration test for ``eawf wave blocks-rebuild`` (P20-W15 / B026).

The brief lists ``tests/integration/test_cli_wave_policy.py`` as the
home for this wave's new integration coverage. Despite the filename
mentioning "policy", the W15 scope is the Wave DAG persistence edges:
this module exercises the ``blocks-rebuild`` round-trip against a
non-trivial DAG and confirms the typed :class:`WaveDagEdges` view
matches the rebuilt state.

Sandbox-policy CLI surface (``eawf wave policy set/show``) is
exercised separately in :mod:`tests.unit.test_cli_wave_policy_cmd`.
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


def _bootstrap_diamond(workspace: Path) -> None:
    """Init QR, open P01-I01, plan a diamond W01->{W02,W03}->W04."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    plan_cases = [
        ("P01-I01-W01", None),
        ("P01-I01-W02", "P01-I01-W01"),
        ("P01-I01-W03", "P01-I01-W01"),
        ("P01-I01-W04", "P01-I01-W02,P01-I01-W03"),
    ]
    for wid, deps in plan_cases:
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
        ]
        if deps is not None:
            args.extend(["--deps", deps])
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.stdout


# ---- blocks-rebuild round-trip ---------------------------------------------


def test_blocks_rebuild_requires_apply_all_flag(workspace: Path) -> None:
    """Without --all the command refuses and exits invalid-input."""
    _bootstrap_diamond(workspace)
    res = runner.invoke(app, ["wave", "blocks-rebuild"])
    assert res.exit_code != 0
    assert "--all" in res.stdout


def test_blocks_rebuild_idempotent_on_clean_diamond(workspace: Path) -> None:
    """Plan-time blocks are already correct: rebuild rewrites zero waves."""
    _bootstrap_diamond(workspace)
    res = runner.invoke(app, ["--json", "wave", "blocks-rebuild", "--all"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["count"] == 0
    assert payload["rewritten"] == []


def test_blocks_rebuild_repairs_drifted_state(workspace: Path) -> None:
    """Manually zero a wave's blocks and confirm rebuild restores it."""
    _bootstrap_diamond(workspace)
    state_path = workspace / ".ea" / "state.json"
    state = json.loads(state_path.read_bytes())
    # Forge drift: clear W01.blocks (which should be [W02, W03] post-plan).
    assert state["waves"]["P01-I01-W01"]["blocks"] == ["P01-I01-W02", "P01-I01-W03"]
    state["waves"]["P01-I01-W01"]["blocks"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")

    res = runner.invoke(app, ["--json", "wave", "blocks-rebuild", "--all"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["count"] == 1
    rewritten = payload["rewritten"]
    assert len(rewritten) == 1
    assert rewritten[0]["id"] == "P01-I01-W01"
    assert rewritten[0]["from"] == []
    assert rewritten[0]["to"] == ["P01-I01-W02", "P01-I01-W03"]

    # On-disk state was rewritten through the state CLI lock.
    restored = json.loads(state_path.read_bytes())
    assert restored["waves"]["P01-I01-W01"]["blocks"] == [
        "P01-I01-W02",
        "P01-I01-W03",
    ]


def test_blocks_rebuild_emits_typed_edges_view(workspace: Path) -> None:
    """The rebuild payload includes a typed deps/blocks/blocked_by triple.

    Confirms the W15 typed-edges surface (``WaveDagEdges`` via
    :func:`eawf.kernel.state.wave_graph.edges`) is primed by the rebuild so
    the TUI wave-board (W03) can read the runtime DAG view off a
    single call site.
    """
    _bootstrap_diamond(workspace)
    # Force at least one rewrite so the typed-edges block lands too.
    state_path = workspace / ".ea" / "state.json"
    state = json.loads(state_path.read_bytes())
    state["waves"]["P01-I01-W01"]["blocks"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")

    res = runner.invoke(app, ["--json", "wave", "blocks-rebuild", "--all"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    edges = {row["id"]: row for row in payload["edges"]}
    assert set(edges) == {
        "P01-I01-W01",
        "P01-I01-W02",
        "P01-I01-W03",
        "P01-I01-W04",
    }
    # Root: no deps, no blocked_by, blocks W02 + W03 (sorted).
    assert edges["P01-I01-W01"]["deps"] == []
    assert edges["P01-I01-W01"]["blocked_by"] == []
    assert edges["P01-I01-W01"]["blocks"] == ["P01-I01-W02", "P01-I01-W03"]
    # Mid-tier W02 / W03: each blocked by W01.
    assert edges["P01-I01-W02"]["deps"] == ["P01-I01-W01"]
    assert edges["P01-I01-W02"]["blocked_by"] == ["P01-I01-W01"]
    assert edges["P01-I01-W02"]["blocks"] == ["P01-I01-W04"]
    assert edges["P01-I01-W03"]["deps"] == ["P01-I01-W01"]
    assert edges["P01-I01-W03"]["blocked_by"] == ["P01-I01-W01"]
    # Sink W04: blocked by both W02 and W03.
    assert edges["P01-I01-W04"]["deps"] == ["P01-I01-W02", "P01-I01-W03"]
    assert edges["P01-I01-W04"]["blocked_by"] == [
        "P01-I01-W02",
        "P01-I01-W03",
    ]
    assert edges["P01-I01-W04"]["blocks"] == []


def test_blocks_rebuild_reflects_close_in_blocked_by(workspace: Path) -> None:
    """After closing a dep, the rebuild payload's blocked_by shrinks."""
    _bootstrap_diamond(workspace)
    # Close W01 so W02/W03 are unblocked.
    assert runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", "S"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["wave", "close", "P01-I01-W01", "--outcome", "done"],
        ).exit_code
        == 0
    )

    res = runner.invoke(app, ["--json", "wave", "blocks-rebuild", "--all"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    edges = {row["id"]: row for row in payload["edges"]}
    # W02 / W03 are unblocked now that W01 is CLOSED.
    assert edges["P01-I01-W02"]["blocked_by"] == []
    assert edges["P01-I01-W03"]["blocked_by"] == []
    # W04 is still blocked by W02 + W03.
    assert edges["P01-I01-W04"]["blocked_by"] == [
        "P01-I01-W02",
        "P01-I01-W03",
    ]
