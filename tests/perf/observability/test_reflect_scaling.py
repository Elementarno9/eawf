"""Measure one reflect over a ledger the size of a long-lived tree.

The ledger is pinned at 1,555 Runs and 21,646 run-event lines, the size of the tree
whose reflect once took 13 minutes, with every fifth Run delegated by the one before
it so the subtree folds run too. That each line is validated once is asserted in the
blocking suite; this lane bounds the wall time of the whole verb.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import RUN_PREFIX, run_row
from tests.unit.observability.reflect.test_runs_grouping import bulk_tree, events_of, run_key

pytestmark = pytest.mark.perf

#: The ceiling one reflect over the pinned ledger must finish under.
REFLECT_CEILING_SECONDS = 20.0

RUNS = 1_555
EVENT_LINES = 21_646

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


def _pinned_tree(repo_root: Path) -> Path:
    keys = [run_key(index) for index in range(1, RUNS + 1)]
    per_run, extra = divmod(EVENT_LINES, RUNS)
    lines = events_of(keys, per_run) + events_of(keys[:extra], 1)
    # The extra lines need their own sequence and event ref past the first block.
    lines[RUNS * per_run :] = [
        item.model_copy(
            update={
                "run_sequence": per_run + 1,
                "event_ref": f"EVT-{RUNS * per_run + offset + 1:08x}",
            }
        )
        for offset, item in enumerate(lines[RUNS * per_run :])
    ]
    rows = [
        run_row(key) | {"parent_run_ref": f"{RUN_PREFIX}/{keys[index - 1]}"}
        if index % 5 == 4
        else run_row(key)
        for index, key in enumerate(keys)
    ]
    assert len(lines) == EVENT_LINES
    return bulk_tree(repo_root, rows, lines)


@_skip_perf
def test_reflect_over_the_pinned_ledger_finishes_inside_its_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = _pinned_tree(tmp_path)

    started = time.perf_counter()
    result = CliRunner().invoke(
        app,
        ["-w", str(tree_root.parent), "reflect", "run", "--local-only", "--format", "json"],
    )
    elapsed = time.perf_counter() - started

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert len(document["sessions"]) + len(document["excluded"]) == RUNS
    assert elapsed < REFLECT_CEILING_SECONDS
