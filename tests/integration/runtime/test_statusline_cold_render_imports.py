"""A cold statusline render loads only what its segments read.

A cache miss renders on the redraw's own time, so every module the render
imports is paid per miss. On an epoch-2 tree holding no MCP rows the render
reads the generation document, the memory ledger and the payload; it must not
build the CLI app, the payload-kind registry, the migration cutover or the
epoch-1 state models. The render runs in a fresh interpreter, because the test
process has all of them imported already.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from eawf.platform.install.canary import canary_ref, provision_canary

_HEAVY: Final = (
    "eawf.surfaces.cli.app",
    "eawf.kernel.store.compact",
    "eawf.kernel.store.kinds.audit",
    "eawf.kernel.migration.epoch2.cutover",
    "eawf.kernel.state.models",
    "eawf.kernel.state.epoch2.task",
    "eawf.runtime.mcp.installer",
)

_PROBE: Final = (
    "import sys\n"
    "sys.argv = ['eawf', 'cc', 'statusline']\n"
    "from eawf.__main__ import main\n"
    "main()\n"
    f"print('\\n'.join(name for name in {_HEAVY!r} if name in sys.modules), file=sys.stderr)\n"
)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provision an epoch-2 canary under this test's tmp dir; return its state path."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    repo = tmp_path / "canary"
    provision_canary(
        repo_root=repo, ref=canary_ref("SLC"), provisioned_at=datetime(2026, 10, 6, tzinfo=UTC)
    )
    return (repo / ".ea" / "state.json").resolve()


def _render(tmp_path: Path, state_path: Path, payload: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "EA_STATE": str(state_path),
        "EAWF_STATUSLINE_CACHE": str(tmp_path / "cache"),
        "EAWF_STATUSLINE_THEME": "ascii-fallback",
    }
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=True,
    )


def test_a_cold_render_leaves_the_heavy_modules_unloaded(tmp_path: Path, tree: Path) -> None:
    proc = _render(tmp_path, tree, json.dumps({"session_id": "ses-cold", "model": "haiku"}))
    assert "model:haiku" in proc.stdout
    assert "mcp:n/a(no-mcp-servers)" in proc.stdout
    assert proc.stderr.strip() == ""


def test_an_unreadable_payload_still_renders_without_them(tmp_path: Path, tree: Path) -> None:
    proc = _render(tmp_path, tree, "{not json")
    assert proc.stdout.count("\n") == 1
    assert proc.stderr.strip() == ""
