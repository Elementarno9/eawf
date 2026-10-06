"""An auto-spawned daemon leaves a stock v0.6.8 tree byte-identical.

The epoch-2 cutover of a live repository is staged from its committed
corpus and refuses with ``migration_live_state_diverged`` when the live
``.ea/state.json`` or a store ledger no longer digests to the staged copy.
A daemon that boots on an unmigrated tree and rewrites its document --
a boot sweep marking a session stale, a reconcile nobody commits -- would
make the first apply after an upgrade refuse. This suite builds the live
tree the ``used`` corpus was staged from, lets a read verb auto-spawn the
daemon, stops it, and proves nothing the cutover pins has moved -- also
under a runtime dir whose lineage predates the tree's live session, the
one an operator upgrading in place actually has.

Every daemon runs as a real subprocess under a tmp ``HOME`` with an
explicit ``EAWF_RUNTIME_DIR``, so it can never reach the operator's own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.snapshot import is_committed
from eawf.runtime.daemon.churn import CHURN_LEDGER_ROTATED_NAME
from eawf.runtime.daemon.spawn import daemon_pid_if_ready, request_daemon_shutdown
from eawf.surfaces.cli.app import app
from tests.integration.kernel.migration._corpus_shapes import DEFAULT_TRACK_KEY
from tests.integration.kernel.migration._cutover_harness import (
    PROJECT_KEY,
    REPOSITORY_KEY,
    WORKSPACE_KEY,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket runtime dirs"),
]

USED = Path(__file__).resolve().parents[3] / "fixtures" / "migration" / "v068-stock" / "used"

#: Upper bound on one CLI run, cold daemon spawn included.
CLI_TIMEOUT_SECONDS = 60.0

runner = CliRunner()


@dataclass(frozen=True)
class Sandbox:
    """A tmp ``HOME`` holding one live tree and the runtime dir its daemon uses."""

    home: Path
    tree: Path
    runtime: Path

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "EA_STATE", "EAWF_DAEMONLESS"):
            env.pop(key, None)
        env["HOME"] = str(self.home)
        env["EAWF_HOME"] = str(self.home / ".eawf")
        env["EAWF_RUNTIME_DIR"] = str(self.runtime)
        # A daemon the teardown misses still exits on its own.
        env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
        return env

    def eawf(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "eawf", *args],
            cwd=self.tree,
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=CLI_TIMEOUT_SECONDS,
        )


def _live_tree(corpus: Path, root: Path) -> Path:
    """Lay the staged corpus back out as the live ``.ea`` tree it came from."""
    live = root / ".ea"
    (live / "store").mkdir(parents=True)
    shutil.copyfile(corpus / "document.json", live / "state.json")
    shutil.copyfile(corpus / "config" / "base.yaml", live / "config.yaml")
    for ledger in sorted((corpus / "store").glob("*.jsonl")):
        shutil.copyfile(ledger, live / "store" / ledger.name)
    return root


def _restage(tree: Path, corpus: Path, destination: Path) -> Path:
    """Stage the live tree's pinned surfaces into a snapshot root.

    Like a staging from HEAD, only ledgers the commit policy tracks are
    carried. The registry and telemetry are synthesised by staging rather
    than read from the live tree, so they are taken from the original corpus.
    """
    live = tree / ".ea"
    (destination / "store").mkdir(parents=True)
    (destination / "config").mkdir()
    shutil.copyfile(live / "state.json", destination / "document.json")
    shutil.copyfile(live / "config.yaml", destination / "config" / "base.yaml")
    for ledger in sorted((live / "store").glob("*.jsonl")):
        if is_committed(ledger.relative_to(tree).as_posix()):
            shutil.copyfile(ledger, destination / "store" / ledger.name)
    for name in ("registry.json", "telemetry.json"):
        shutil.copyfile(corpus / name, destination / name)
    return destination


def _digests(tree: Path) -> dict[str, str]:
    """Return the sha256 of the live document and of every store ledger."""
    live = tree / ".ea"
    paths = [live / "state.json", *sorted((live / "store").glob("*.jsonl"))]
    return {
        path.relative_to(tree).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _plan_args(snapshot_root: Path) -> list[str]:
    return [
        "--json",
        "migrate",
        "epoch2",
        "--plan",
        "--snapshot-root",
        str(snapshot_root),
        "--workspace-key",
        WORKSPACE_KEY,
        "--project-key",
        PROJECT_KEY,
        "--repository-key",
        REPOSITORY_KEY,
        "--default-track-key",
        DEFAULT_TRACK_KEY,
    ]


def _manifest_digest(output: str) -> str:
    assert "live_state_diverged" not in output
    return str(json.loads(output)["result"]["manifest_digest"])


def _reopen_session(corpus: Path) -> None:
    """Make the corpus's one closed session live, the state FU-117 names."""
    path = corpus / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for session in document["agent_sessions"].values():
        session["status"] = "active"
        session["ended_at"] = None
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _age_lineage(runtime: Path) -> None:
    """Give the runtime dir a lineage older than every session on the tree.

    A runtime dir the operator has used across releases has rotated its
    boot ledger, so the booting daemon can no longer prove it did not open
    the tree's sessions; it is the runtime dir an upgrade actually meets.
    """
    (runtime / CHURN_LEDGER_ROTATED_NAME).write_text("", encoding="utf-8")


def _stop(runtime: Path) -> None:
    pid = daemon_pid_if_ready(runtime)
    with contextlib.suppress(OSError, RuntimeError):
        request_daemon_shutdown(runtime, drain=False, timeout_seconds=5)
    if pid is None:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    # Rooted at /tmp: a socket under the macOS per-user temp dir runs past
    # the 104-byte AF_UNIX path cap.
    home = Path(tempfile.mkdtemp(prefix="eawf-h-", dir="/tmp"))
    runtime = home / "rt"
    runtime.mkdir(mode=0o700)
    box = Sandbox(home=home, tree=home / "tree", runtime=runtime)
    try:
        yield box
    finally:
        _stop(runtime)
        shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize(
    ("live_session", "old_lineage"),
    [(False, False), (True, False), (True, True)],
    ids=["as-staged", "live-session", "live-session-old-lineage"],
)
def test_auto_spawned_daemon_leaves_v068_tree_untouched(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_session: bool,
    old_lineage: bool,
) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(USED, corpus)
    if live_session:
        _reopen_session(corpus)
    if old_lineage:
        _age_lineage(sandbox.runtime)
    _live_tree(corpus, sandbox.tree)
    before = _digests(sandbox.tree)

    status = sandbox.eawf("status")
    # ``status`` reads in-process on an epoch-1 tree, so the plan -- the
    # first verb of the upgrade, routed through the daemon by default --
    # is what is left to spawn it.
    routed = sandbox.eawf(*_plan_args(corpus))
    spawned = daemon_pid_if_ready(sandbox.runtime)
    stopped = sandbox.eawf("daemon", "stop")

    assert status.returncode == 0, status.stderr
    assert routed.returncode == 0, routed.stderr
    assert spawned is not None, f"no read verb auto-spawned a daemon: {routed.stderr}"
    assert stopped.returncode == 0, stopped.stderr
    after = _digests(sandbox.tree)
    assert {locator: after.get(locator) for locator in before} == before
    # A ledger the daemon creates is divergence only when the commit policy
    # tracks it: the apply ignores a gitignored firehose such as events.
    assert [locator for locator in after.keys() - before.keys() if is_committed(locator)] == []

    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_HOME", str(sandbox.home / ".eawf"))
    restaged = _restage(sandbox.tree, corpus, tmp_path / "restaged")
    planned = runner.invoke(app, _plan_args(corpus))
    replanned = runner.invoke(app, _plan_args(restaged))

    assert planned.exit_code == 0, planned.output
    assert replanned.exit_code == 0, replanned.output
    assert "live_state_diverged" not in routed.stdout + routed.stderr
    assert _manifest_digest(replanned.output) == _manifest_digest(planned.output)
