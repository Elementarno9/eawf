"""REL-026: the known-flaky set is green on five consecutive fresh runs.

Two tests carried the suite's shared-state flakes. Both are re-run here as
real subprocesses under ``-n auto``, five times in a row, with every run
required to exit ``0`` -- one green run proves nothing about a race, and a
single in-process re-run would reuse the parent session's already-warmed
runtime dir.

**Heartbeat ticker** (``test_ticker_refreshes_heartbeat_during_hold``).
:meth:`eawf.runtime.lock.portalock.LockHandle.heartbeat` refreshes the
lockfile by ``seek(0)`` / ``truncate()`` / ``write()`` on the held handle,
so the file is observably empty between the truncate and the write. The
test reads the same path from the main thread while the ticker thread is
mid-refresh, which on a loaded runner lands inside that window and raises
``JSONDecodeError`` rather than failing an assertion. The window is a
property of the non-atomic rewrite, not of the test: it closes only when
the writer swaps in a fully-written file. Fixing the writer is outside
this wave's file scope, so the loop below is the standing witness that the
window stays narrow enough not to fire.

**Concurrent evidence writes**
(``test_evidence_concurrent_property.py``). N threads race the same
``state.json`` through ``state_transaction``; the macOS portalock-plus-unlink
layer admits an in-process race the module documents. Contention is
sensitive to worker count, so the run is pinned to ``-n auto`` where the
flake was observed rather than to a fixed pool.

Each child run gets its own ``EAWF_RUNTIME_DIR``: the variable is dropped
from the child environment so the child's own session fixture mints a fresh
one, making the five runs independent rather than five passes over one
warmed dir.
"""

from __future__ import annotations

import os
import subprocess  # noqa: EAWF024 the loop's whole point is a real re-run
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.property

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: The tests whose flakes REL-026 root-caused, as repo-relative node ids.
FLAKE_TARGETS: tuple[str, ...] = (
    "tests/unit/test_lock_portalock.py::test_ticker_refreshes_heartbeat_during_hold",
    "tests/property/test_evidence_concurrent_property.py",
)

#: How many consecutive green runs the acceptance criterion demands.
CONSECUTIVE_RUNS: int = 5

#: Generous per-run ceiling: the target set runs in ~4s locally, so this
#: only fires when a run has actually wedged rather than merely gone slow.
_RUN_TIMEOUT_S: float = 300.0


def run_targets_once(targets: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run *targets* in a fresh ``pytest -n auto`` subprocess.

    Args:
        targets: Repo-relative node ids to hand to the child pytest. An
            empty tuple runs the whole suite, so callers must not pass one.

    Returns:
        The completed child process, with stdout and stderr captured.

    Raises:
        ValueError: *targets* is empty, which would silently re-run the
            entire suite (including this module) instead of the named set.
    """
    if not targets:
        raise ValueError("run_targets_once needs at least one target node id")

    env = dict(os.environ)
    # Let the child mint its own isolated runtime dir rather than inheriting
    # the parent session's, so consecutive runs share no daemon state.
    env.pop("EAWF_RUNTIME_DIR", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "auto",
            "-q",
            "-p",
            "no:cacheprovider",
            *targets,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
        check=False,
    )


@pytest.mark.slow
def test_flake_prone_targets_exit_zero_on_five_consecutive_runs() -> None:
    """Every one of five consecutive ``-n auto`` runs exits ``0``."""
    exit_codes: list[int] = []
    for attempt in range(CONSECUTIVE_RUNS):
        completed = run_targets_once(FLAKE_TARGETS)
        exit_codes.append(completed.returncode)
        assert completed.returncode == 0, (
            f"run {attempt + 1} of {CONSECUTIVE_RUNS} exited "
            f"{completed.returncode}\n{completed.stdout[-2000:]}\n"
            f"{completed.stderr[-800:]}"
        )
    assert exit_codes == [0] * CONSECUTIVE_RUNS


def test_run_targets_once_rejects_an_empty_target_set() -> None:
    """An empty target tuple raises rather than re-running the whole suite."""
    with pytest.raises(ValueError, match="at least one target node id"):
        run_targets_once(())


@pytest.mark.slow
def test_run_targets_once_reports_a_nonzero_exit_for_a_missing_target() -> None:
    """A single unknown node id (boundary: one target) yields a nonzero exit."""
    completed = run_targets_once(("tests/property/test_no_such_module.py",))
    assert completed.returncode != 0


def test_flake_targets_exist_on_disk() -> None:
    """Each named target still resolves, so a rename cannot silently empty the loop."""
    assert FLAKE_TARGETS
    for target in FLAKE_TARGETS:
        assert (REPO_ROOT / target.split("::", 1)[0]).is_file(), target
