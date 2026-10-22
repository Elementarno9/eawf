"""Reflect reads a tree's Runs from both tiers: the document and the run ledger.

A terminal Run is compacted out of the generation document into the run ledger,
so a reader of the document alone lists only the Runs still in flight. These tests
place Runs in both tiers and check the listing and the single-Run lookup agree.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect.runs import read_tree_run, read_tree_runs
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import AT, native_tree, run_row

runner = CliRunner()


def _compact(tree_root: Path, rows: list[dict[str, Any]]) -> None:
    """File each row on the run ledger as a compacted terminal Run, oldest first."""
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    for offset, row in enumerate(rows):
        append_ledger_record(
            ledger_path(document, Epoch2Collection.RUN),
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=row["key"],
                status="compacted",
                recorded_at=AT + timedelta(minutes=offset),
                payload=row,
            ),
        )


@pytest.fixture
def two_tier_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary with two live Runs in the document and three on the ledger."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [run_row("RUN-00000002", status="RUNNING"), run_row("RUN-00000004", status="QUEUED")],
        [],
    )
    _compact(
        tree_root,
        [run_row("RUN-00000005"), run_row("RUN-00000001"), run_row("RUN-00000003", hours=5)],
    )
    return tree_root


def test_reflect_lists_live_and_compacted_runs(two_tier_tree: Path) -> None:
    """Both tiers are read: five readings in key order, and the report lists five sessions."""
    readings = read_tree_runs(two_tier_tree)
    assert [reading.run.key for reading in readings] == [
        "RUN-00000001",
        "RUN-00000002",
        "RUN-00000003",
        "RUN-00000004",
        "RUN-00000005",
    ]
    result = runner.invoke(
        app, ["--json", "-w", str(two_tier_tree.parent), "reflect", "run", "--local-only"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["manifest"]["sessions_listed"] == 5
    document = json.loads(Path(payload["document"]).read_text(encoding="utf-8"))
    assert len(document["sessions"]) == 5


def test_reflect_lists_a_compacted_run_once_at_its_latest_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key filed twice on the ledger reads once, as its later line holds it."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(tmp_path, [], [])
    _compact(
        tree_root,
        [run_row("RUN-00000001", status="RUNNING"), run_row("RUN-00000001")],
    )
    readings = read_tree_runs(tree_root)
    assert [(item.run.key, item.run.status) for item in readings] == [("RUN-00000001", "COMPLETED")]
    assert read_tree_run(tree_root, "RUN-00000001").run.status == "COMPLETED"


def test_reflect_prefers_the_live_row_over_a_ledger_line_of_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a key still in the document is read from the document alone."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(tmp_path, [run_row("RUN-00000001", status="RUNNING")], [])
    _compact(tree_root, [run_row("RUN-00000001")])
    readings = read_tree_runs(tree_root)
    assert [(item.run.key, item.run.status) for item in readings] == [("RUN-00000001", "RUNNING")]


def test_reflect_reads_an_empty_tree_as_no_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: neither tier holds a Run, so the listing is empty and a lookup fails."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(tmp_path, [], [])
    assert read_tree_runs(tree_root) == ()
    with pytest.raises(LookupError):
        read_tree_run(tree_root, "RUN-00000001")
