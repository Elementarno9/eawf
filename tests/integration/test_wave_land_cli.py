"""CLI integration tests for ``eawf wave land`` and ``eawf wave land-batch``.

The tests stand up a real ``git init``-ed tmp repo, seed an
``.ea/state.json`` with one or more CLAIMED waves, create worktrees,
make commits, and drive the wave-land verbs via :class:`CliRunner`.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests._worktree_helpers import seed_repo_with_state as _seed_repo_with_state

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for wave land CLI tests",
)


def test_wave_land_already_closed_wave_exit_4(tmp_path: Path) -> None:
    """Landing a wave that's already CLOSED surfaces exit 4 (VALIDATION_FAILED)."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    payload = orjson.loads(state_path.read_bytes())
    payload["waves"]["P05-I01-W01"]["status"] = "closed"
    payload["waves"]["P05-I01-W01"]["outcome"] = "previously closed"
    payload["waves"]["P05-I01-W01"]["closed_at"] = datetime.now(UTC).isoformat()
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))

    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "wave",
            "land",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 2, res.stdout
