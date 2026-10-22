"""``reflect run`` selects its Runs by --window and --scope, and prints JSON on --format json.

A Run is placed by its scope: a Task's Batch ships under a Milestone, and the Milestone
serves its primary Track and any contributing ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.observability.reflect import report as rp
from eawf.observability.reflect.report import ReflectReport
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import native_tree, run_row

runner = CliRunner()

BASE = "eawf://WSP-RFL/PRJ-RFL/REP-RFL"


def on_task(row: dict[str, Any], task: str) -> dict[str, Any]:
    """Return ``row`` scoped to the Task keyed ``task``."""
    return row | {"scope": row["scope"] | {"task_ref": f"{BASE}/task/{task}"}}


def recent(row: dict[str, Any]) -> dict[str, Any]:
    """Return ``row`` started a day ago and ended an hour later."""
    started = datetime.now(UTC) - timedelta(days=1)
    return row | {
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(hours=1)).isoformat(),
    }


def place(tree_root: Path) -> None:
    """File the Tasks, Batches and Milestones the fixture Runs are placed by."""
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    document["task"] = {
        f"RFL-000{n}": {"key": f"RFL-000{n}", "batch_ref": f"{BASE}/batch/BAT-000{n}"}
        for n in (1, 2, 4)
    }
    document["batch"] = {
        "BAT-0001": {"key": "BAT-0001", "milestone_ref": f"{BASE}/milestone/MLS-0104"},
        "BAT-0002": {"key": "BAT-0002", "milestone_ref": f"{BASE}/milestone/MLS-0105"},
        "BAT-0004": {"key": "BAT-0004", "milestone_ref": f"{BASE}/milestone/MLS-0106"},
    }
    document["milestone"] = {
        "MLS-0104": {"key": "MLS-0104", "primary_track_ref": f"{BASE}/track/TRK-EAWF-CORE"},
        "MLS-0105": {"key": "MLS-0105", "primary_track_ref": f"{BASE}/track/TRK-RFL-SIDE"},
        "MLS-0106": {
            "key": "MLS-0106",
            "primary_track_ref": f"{BASE}/track/TRK-RFL-SIDE",
            "contributing_track_refs": [f"{BASE}/track/TRK-EAWF-CORE"],
        },
    }
    write_document(path, document)


@pytest.fixture
def placed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree with Runs under two Milestones, one recent unplaced Run and a queued one."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [
            on_task(run_row("RUN-00000001"), "RFL-0001"),
            on_task(run_row("RUN-00000002"), "RFL-0002"),
            on_task(recent(run_row("RUN-00000003")), "RFL-0003"),
            on_task(run_row("RUN-00000004"), "RFL-0004"),
            on_task(run_row("RUN-00000005", status="QUEUED"), "RFL-0001"),
        ],
        [],
    )
    place(tree_root)
    return tree_root


def _json(tree_root: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "-w",
            str(tree_root.parent),
            "reflect",
            "run",
            "--local-only",
            "--format",
            "json",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _keys(document: dict[str, Any]) -> list[str]:
    return [line["run_key"] for line in document["sessions"]]


def test_window_scope_and_format_select_runs(placed_tree: Path) -> None:
    """Window and scope narrow sessions and totals; the refused scopes exit 2 naming the three."""
    everything = _json(placed_tree)
    assert ReflectReport.model_validate(everything).schema_version == 2
    assert _keys(everything) == [f"RUN-0000000{n}" for n in range(1, 6)]
    assert everything["scope"] == {"kind": "project", "key": None}

    week = _json(placed_tree, "--window", "7d")
    assert _keys(week) == ["RUN-00000003"]
    assert week["window_days"] == 7

    milestone = _json(placed_tree, "--scope", "milestone:MLS-0104")
    assert _keys(milestone) == ["RUN-00000001", "RUN-00000005"]
    assert [row["unpriced_runs"] for row in milestone["totals"]] == [2]

    track = _json(placed_tree, "--scope", "track:TRK-EAWF-CORE")
    assert _keys(track) == ["RUN-00000001", "RUN-00000004", "RUN-00000005"]
    assert [row["unpriced_runs"] for row in track["totals"]] == [3]

    for refused in ("all_local", "workspace"):
        result = runner.invoke(
            app, ["-w", str(placed_tree.parent), "reflect", "run", "--scope", refused]
        )
        assert result.exit_code == 2, result.output
        for name in ("project", "milestone", "track"):
            assert name in result.output


@pytest.mark.parametrize(
    "value",
    ["", "0d", "7", "7w", "-1d", "10000d", "d"],
    ids=["empty", "zero", "no-unit", "weeks", "negative", "over-max", "no-number"],
)
def test_a_window_that_is_not_whole_days_is_refused(value: str) -> None:
    """Error path: --window takes 1d to 9999d."""
    with pytest.raises(ValueError, match="<N>d"):
        rp.parse_window(value)


def test_window_bounds_are_one_and_9999_days() -> None:
    """Boundary: the smallest and largest windows parse."""
    assert (rp.parse_window("1d"), rp.parse_window("9999d")) == (1, 9999)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "project:",
        "project:EAWF",
        "milestone:",
        "milestone:M-1",
        "track:EAWF",
        "cohort:x",
        "all_local",
    ],
    ids=[
        "empty",
        "bare-colon",
        "keyed-project",
        "no-key",
        "bad-milestone",
        "bad-track",
        "cohort",
        "all-local",
    ],
)
def test_a_scope_outside_the_three_is_refused(value: str) -> None:
    """Error path: only project, milestone:<KEY> and track:<KEY> parse."""
    with pytest.raises(ValueError, match="milestone"):
        rp.parse_scope(value)


def test_an_unknown_format_exits_2(placed_tree: Path) -> None:
    """Error path: --format takes text or json."""
    result = runner.invoke(
        app, ["-w", str(placed_tree.parent), "reflect", "run", "--format", "yaml"]
    )
    assert result.exit_code == 2, result.output
