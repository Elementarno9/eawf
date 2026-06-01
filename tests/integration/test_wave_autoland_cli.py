"""CLI integration tests for ``eawf wave autoland``.

The tests stand up a real ``git init``-ed tmp repo, seed an
``.ea/state.json`` with CLAIMED waves, create worktrees + commits via the
worktree CLI, flip the waves to CLOSED (the close that the dispatched
executor performs in-worktree), then drive ``wave autoland`` -- the land
back-half that brings the commits home in dependency order.

Integration tests run daemonless (``tests/integration/conftest.py``
forces ``EAWF_DAEMONLESS=1``), so these exercise the in-process
carve-out path through ``_call_worktree_daemonless``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.integration.test_worktree_cli_create import _seed_repo_with_state
from tests.integration.test_worktree_cli_merge_back import _create_worktree_and_commit

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for wave autoland CLI tests",
)


def _close_wave_in_state(state_path: Path, wave_id: str) -> None:
    """Flip *wave_id* to CLOSED in the on-disk state (worktree stays ACTIVE).

    Mirrors the real close path: the wave is also dropped from
    ``current.active_wave_ids`` so the ``INV.CURRENT.WAVE_NOT_ACTIVE``
    invariant (a closed wave must not be listed active) stays satisfied.
    """
    payload = orjson.loads(state_path.read_bytes())
    wave = payload["waves"][wave_id]
    wave["status"] = "closed"
    wave["outcome"] = "closed in worktree"
    wave["closed_at"] = datetime.now(UTC).isoformat()
    active = payload["current"].get("active_wave_ids", [])
    payload["current"]["active_wave_ids"] = [w for w in active if w != wave_id]
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _inject_second_wave(state_path: Path, *, wave_id: str, deps: list[str]) -> None:
    """Append a CLAIMED iter-mate wave so a worktree can be created for it.

    The reverse ``blocks`` edge is added to each dep so the
    ``INV.GRAPH.BLOCKS_MISSING_REVERSE`` invariant stays satisfied.
    """
    payload = orjson.loads(state_path.read_bytes())
    payload["waves"][wave_id] = {
        "id": wave_id,
        "iter_id": "P05-I01",
        "title": wave_id,
        "status": "claimed",
        "deps": deps,
        "blocks": [],
        "file_scopes": ["src/eawf/dispatch/"],
        "claim_session_id": "SES-002",
        "worktree_id": None,
        "outcome": None,
        "opened_at": datetime.now(UTC).isoformat(),
        "closed_at": None,
    }
    for dep_id in deps:
        dep_wave = payload["waves"][dep_id]
        dep_blocks = dep_wave.get("blocks", [])
        if wave_id not in dep_blocks:
            dep_wave["blocks"] = [*dep_blocks, wave_id]
    payload["iters"]["P05-I01"]["wave_ids"] = sorted(
        {*payload["iters"]["P05-I01"]["wave_ids"], wave_id}
    )
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _create_second_worktree_and_commit(
    repo: Path,
    state_path: Path,
    *,
    wave_id: str,
    file_name: str,
    content: str,
    msg: str,
) -> str:
    """Create a worktree for *wave_id* and commit content; return the path."""
    res = runner.invoke(
        app,
        ["--json", "-w", str(repo), "worktree", "create", "--wave", wave_id],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    wt_path = repo / envelope["path"]
    (wt_path / file_name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(wt_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-q", "-m", msg], check=True)
    return str(wt_path)


def test_wave_autoland_happy_path_envelope_shape(tmp_path: Path) -> None:
    """The success envelope carries order / landed / failed_wave / remaining."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    wt_path, _ = _create_worktree_and_commit(
        repo,
        state_path,
        file_name="hello.txt",
        content="hi\n",
        msg="add hello",
    )
    _close_wave_in_state(state_path, "P05-I01-W01")

    res = runner.invoke(
        app,
        ["--json", "-w", str(repo), "wave", "autoland", "--iter", "P05-I01"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    for key in ("order", "landed", "failed_wave", "remaining", "dry_run"):
        assert key in envelope, f"missing key {key!r} in {envelope}"
    assert envelope["order"] == ["P05-I01-W01"]
    assert envelope["failed_wave"] is None
    assert envelope["remaining"] == []
    assert len(envelope["landed"]) == 1
    assert envelope["landed"][0]["wave"] == "P05-I01-W01"
    assert envelope["landed"][0]["worktree_cleaned"] is True
    # Parent branch now has the file; worktree dir is gone.
    assert (repo / "hello.txt").exists()
    assert not Path(wt_path).exists()


def test_wave_autoland_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """``--dry-run`` prints the order and exits 0 without landing anything."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    wt_path, _ = _create_worktree_and_commit(
        repo,
        state_path,
        file_name="hello.txt",
        content="hi\n",
        msg="add hello",
    )
    _close_wave_in_state(state_path, "P05-I01-W01")

    res = runner.invoke(
        app,
        ["--json", "-w", str(repo), "wave", "autoland", "--iter", "P05-I01", "--dry-run"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["dry_run"] is True
    assert envelope["order"] == ["P05-I01-W01"]
    assert envelope["landed"] == []
    # Nothing landed: parent branch lacks the file; worktree dir intact.
    assert not (repo / "hello.txt").exists()
    assert Path(wt_path).exists()


def test_wave_autoland_stops_on_conflict_exit_state_conflict(tmp_path: Path) -> None:
    """A mid-sequence conflict halts the land and exits STATE_CONFLICT (3)."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    _inject_second_wave(state_path, wave_id="P05-I01-W02", deps=["P05-I01-W01"])

    # W01's worktree adds a clean file; W02's worktree adds a divergent
    # conflict.txt that will collide with a later parent commit.
    _create_worktree_and_commit(
        repo,
        state_path,
        file_name="a.txt",
        content="a\n",
        msg="add a",
    )
    # Seed conflict.txt on the parent so W02 can diverge from it.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)
    _create_second_worktree_and_commit(
        repo,
        state_path,
        wave_id="P05-I01-W02",
        file_name="conflict.txt",
        content="worktree-2\n",
        msg="wt-2 edit",
    )
    # Move parent forward so W02's cherry-pick will conflict.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    _close_wave_in_state(state_path, "P05-I01-W01")
    _close_wave_in_state(state_path, "P05-I01-W02")

    res = runner.invoke(
        app,
        ["--json", "-w", str(repo), "wave", "autoland", "--iter", "P05-I01"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 3, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["failed_wave"] == "P05-I01-W02"
    assert envelope["error"]
    # W01 landed before the conflict; W02 is the un-landed remainder.
    assert [row["wave"] for row in envelope["landed"]] == ["P05-I01-W01"]
    assert envelope["remaining"] == ["P05-I01-W02"]
    # The persisted worktree record for W02 is CONFLICTED (evidence kept).
    payload = orjson.loads(state_path.read_bytes())
    w2_worktree_id = payload["waves"]["P05-I01-W02"]["worktree_id"]
    assert payload["worktrees"][w2_worktree_id]["status"] == "conflicted"


def test_wave_autoland_empty_set_exit_zero(tmp_path: Path) -> None:
    """No landable waves -> exit 0 with an empty order/landed envelope."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    # The seeded wave is CLAIMED (no worktree, not closed) -> not landable.
    res = runner.invoke(
        app,
        ["--json", "-w", str(repo), "wave", "autoland", "--iter", "P05-I01"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["order"] == []
    assert envelope["landed"] == []
    assert envelope["failed_wave"] is None
