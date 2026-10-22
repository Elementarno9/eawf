"""The real bubblewrap jail of a native Run holds against every containment attempt.

A native Run's workspace is leased under the tree's ``.ea``, which the jail
masks, so the mask, the write-ahead-log mask and the mount-point directories
the workspace bind leaves beneath the mask must all refuse writes while the
workspace itself stays writable. These tests launch the real ``bwrap`` with
the argv the jail builds and skip on a host without it.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.runtimes.claude.sdk_launcher import _write_wrapper
from eawf.runtime.runtimes.containment import CONTAINMENT_ATTEMPTS
from eawf.runtime.runtimes.containment_observation import (
    PROBE_DENIAL_REASONS,
    ContainmentTargets,
    observe_containment,
)
from eawf.runtime.sandbox.jail import build_jail_argv

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux" or shutil.which("bwrap") is None,
        reason="needs Linux and bubblewrap on PATH",
    ),
]

_CLAUDE: Final = "claude-code"

#: What runs in the jail: one ``<path> <errno or ok>`` line per write attempt,
#: then whether the daemon's state file could be read.
_WRITE_PROGRAM: Final = """\
import json, os, sys
storage, *directories = sys.argv[1:]
writes = {}
for directory in directories:
    path = os.path.join(directory, "probe")
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(path)
        writes[directory] = 0
    except OSError as error:
        writes[directory] = error.errno
try:
    with open(os.path.join(storage, "state.json"), "rb") as handle:
        state = len(handle.read())
except OSError as error:
    state = -error.errno
print(json.dumps({"writes": writes, "state": state}))
"""


class _Tree:
    """A tree whose workspace is leased under its ``.ea``, as the daemon leases one."""

    def __init__(self, tmp_path: Path) -> None:
        """Lay the tree out under *tmp_path*, every target present on the host."""
        self.repo = tmp_path / "repo"
        self.storage = self.repo / ".ea"
        self.workspace = self.storage / "local" / "epoch2" / "workspaces" / "wsh-1" / "ws"
        self.run_dir = self.workspace / MCP_ARTIFACT_DIRNAME
        self.private_home = self.run_dir / "home"
        self.private_home.mkdir(parents=True)
        (self.storage / "state.json").write_text('{"secret": true}', encoding="utf-8")
        (self.repo / ".git").mkdir()
        self.wal = tmp_path / "runtime" / "wal"
        self.wal.mkdir(parents=True)
        self.home = tmp_path / "home"
        self.home.mkdir()

    def jail(self) -> list[str]:
        """Return the jail prefix a native Run of this tree is launched under."""
        prefix = build_jail_argv(
            _CLAUDE,
            cwd=self.workspace,
            root=self.repo,
            platform="linux",
            home=self.home,
            private_home=self.private_home,
            storage_dirs=(self.storage, self.wal),
        )
        return [shutil.which(prefix[0]) or prefix[0], *prefix[1:]]


def test_storage_masks_read_only_while_the_workspace_stays_writable(tmp_path: Path) -> None:
    tree = _Tree(tmp_path)
    beside = tree.workspace.parent

    ran = subprocess.run(
        [
            *tree.jail(),
            sys.executable,
            "-I",
            "-c",
            _WRITE_PROGRAM,
            str(tree.storage),
            str(tree.storage),
            str(tree.wal),
            str(beside),
            str(tree.workspace),
            str(tree.private_home),
        ],
        cwd=tree.workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    seen = json.loads(ran.stdout)
    assert seen["writes"] == {
        str(tree.storage): errno.EROFS,
        str(tree.wal): errno.EROFS,
        str(beside): errno.EROFS,
        str(tree.workspace): 0,
        str(tree.private_home): 0,
    }
    assert seen["state"] == -errno.ENOENT
    # Nothing the jail let through landed on the host.
    assert sorted(path.name for path in tree.wal.iterdir()) == []
    assert sorted(path.name for path in beside.iterdir()) == ["ws"]


def test_observe_containment_in_the_real_bwrap_jail_denies_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _Tree(tmp_path)
    decoy = tree.storage / "local" / "decoy" / ".aws"
    decoy.mkdir(parents=True)
    (decoy / "credentials").write_text("decoy", encoding="utf-8")
    # The daemon's own PWD is not admitted; bubblewrap sets one of its own in
    # the jail, which must not read as the daemon's leaking through.
    monkeypatch.setenv("PWD", str(tmp_path))
    _write_wrapper(
        tree.run_dir, argv=[*tree.jail(), "/bin/false"], env_keys=["PATH"], python=sys.executable
    )
    targets = ContainmentTargets(
        canonical_git=tree.repo / ".git",
        daemon_storage=tree.storage,
        write_ahead_log=tree.wal,
        outside_writable_roots=tree.workspace.parent,
        credential_paths=(decoy,),
        host_process=os.getpid(),
    )

    observed = observe_containment(tree.run_dir, workspace=tree.workspace, targets=targets)

    assert {row.attempt: row.denied for row in observed} == dict.fromkeys(
        CONTAINMENT_ATTEMPTS, True
    )
    assert all(row.denial_reason == PROBE_DENIAL_REASONS[row.attempt] for row in observed)
    assert list(tmp_path.rglob(".eawf-canary-*")) == []
