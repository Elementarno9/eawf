"""A Task runs from claim to integration without touching a tracked file.

Driven over the registered daemon verbs on a disposable canary that is a
real git repository carrying the managed ignore block. The planned tree
is committed first, so every later ``git status`` line is something a
step wrote. Claiming, starting and sealing the Task and running and
ending its Run are in-flight status, and leave the tree clean.
Integrating and verifying the Batch is a delivery fact, and completing
the Task a terminal status; both land in the committed generation.

Nothing here stands in for the split. With the status projection folded
back into ``state.json``, the start alone rewrites a tracked file and
the first cleanliness assertion reds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import eawf.runtime.daemon.methods.delivery
import eawf.runtime.daemon.methods.domain
import eawf.runtime.daemon.methods.run  # noqa: F401  (registers the verbs the walk reaches)
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path, status_projection_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.gitignore_writer import write_gitignore
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.native_dispatch import compile_launchers
from tests.integration.workflow.release._canary_acceptance_walk import (
    StandInLauncher,
    Walker,
    _dispatch_and_seal,
    _integrate_and_verify,
    _plan,
    git,
)

pytestmark = pytest.mark.integration


def porcelain(walker: Walker) -> list[tuple[str, str]]:
    """Return every ``git status`` code and path of the canary, untracked files included."""
    output = git(walker.root, "status", "--porcelain", "--untracked-files=all")
    return [(line.split()[0], line.split()[-1]) for line in output.splitlines() if line]


def commit_all(walker: Walker, subject: str) -> None:
    """Commit everything the canary holds under *subject*."""
    git(walker.root, "add", "-A")
    git(walker.root, "commit", "-q", "-m", subject)


def document_path(walker: Walker) -> Path:
    """Return the selected generation's committed document."""
    with walker.context.session([walker.urn("track", "TRK-CANARY")]) as session:
        return session.document_path


def committed(walker: Walker) -> dict[str, Any]:
    """Return the committed document's own bytes, decoded without the projection."""
    return json.loads(document_path(walker).read_text("utf-8"))


def ledger_keys(path: Path) -> set[str]:
    """Return the record keys a ledger file holds, empty when it is absent."""
    if not path.is_file():
        return set()
    return {item.record_key for item in read_ledger_records(path)}


def test_only_a_terminal_or_delivery_fact_changes_a_tracked_file(tmp_path: Path) -> None:
    walker = Walker(tmp_path / "repo", tmp_path / "runtime")
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((StandInLauncher(),)))
    ):
        planned = _plan(walker)
        write_gitignore(walker.root)
        commit_all(walker, "chore: initialize the canary")
        assert porcelain(walker) == []
        state_path = document_path(walker)
        task_key, run_key = planned.task.rsplit("/", 1)[1], planned.run.rsplit("/", 1)[1]
        assert committed(walker)["task"][task_key]["status"] == "PLANNED"
        assert "run" not in committed(walker)

        _dispatch_and_seal(walker, planned)
        assert walker.stored(planned.task)["status"] == "READY_TO_INTEGRATE"
        assert porcelain(walker) == [], "starting a Task rewrote a tracked file"
        projection = json.loads(status_projection_path(state_path).read_text("utf-8"))
        assert projection["status"]["task"][task_key]["status"] == "READY_TO_INTEGRATE"
        assert committed(walker)["task"][task_key]["status"] == "PLANNED"

        binding = _integrate_and_verify(walker, planned)
        batch_key = planned.batch.rsplit("/", 1)[1]
        changed = porcelain(walker)
        assert ("M", state_path.relative_to(walker.root).as_posix()) in changed, changed
        assert committed(walker)["batch"][batch_key]["status"] == "READY_TO_MERGE"
        commit_all(walker, "feat: deliver the canary change")

        walker.transition(
            planned.task,
            "COMPLETED",
            "the Batch integrated the Task's work",
            updates={"integrated_binding": binding},
        )
        changed = porcelain(walker)
        assert changed, "completing a Task left no committed trace"
        assert all(
            not path.startswith(".ea/generations/gen-") or "/local/" not in path
            for _, path in changed
        ), changed
        commit_all(walker, "chore: record the completed Task")
        walker.transition(
            planned.run,
            "RUNNING",
            "the worker took the Run up",
            updates={"started_at": datetime.now(UTC).isoformat()},
        )
        walker.transition(
            planned.run,
            "COMPLETED",
            "the Run's report is bound and its work integrated",
            updates={"ended_at": datetime.now(UTC).isoformat()},
            observations=["run_report_bound"],
        )
        assert porcelain(walker) == [], "ending a Run rewrote a tracked file"

    assert ledger_path(state_path, Epoch2Collection.TASK).parent == state_path.parent / "ledger"
    assert task_key in ledger_keys(ledger_path(state_path, Epoch2Collection.TASK))
    assert run_key in ledger_keys(ledger_path(state_path, Epoch2Collection.RUN))
