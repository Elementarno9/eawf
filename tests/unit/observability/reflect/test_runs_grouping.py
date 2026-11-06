"""Reflect reads the run ledger's event lines once, and only for the Runs it keeps.

The event lines of every kept Run are validated in one pass over the ledger, so a
reflect costs one validation per line however many Runs the tree holds; --window and
--scope select the Runs before any line is read, and each reading phase reports its
progress on stderr while stdout carries the report alone.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import RunEventRecord
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.ledger import LedgerRecord, render_ledger_line
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect import runs as runs_module
from eawf.observability.reflect.runs import RunReading, read_tree_runs
from eawf.runtime.daemon.run_events import run_events_by_run
from eawf.surfaces.cli.commands.reflect import _ReportFormat, reflect_run
from eawf.surfaces.cli.flags import GlobalFlags
from tests.contract.surfaces.cli._reflect_tree import AT, RUN_PREFIX, event, native_tree, run_row
from tests.contract.surfaces.cli.test_reflect_scope import on_task, place, recent

pytestmark = pytest.mark.unit

_PROGRESS = re.compile(r"^reflect: (load|events|analyse|index|sessions|render) (\d+)/(\d+)$")


def run_key(index: int) -> str:
    """Return the key of the ``index``-th synthetic Run."""
    return f"RUN-{index:08d}"


def events_of(keys: Sequence[str], per_run: int) -> list[RunEventRecord]:
    """Return ``per_run`` event lines for each Run in ``keys``, interleaved across Runs."""
    lines: list[RunEventRecord] = []
    for sequence in range(1, per_run + 1):
        for key in keys:
            ordinal = len(lines) + 1
            lines.append(event(key, sequence, event_ref=f"EVT-{ordinal:08x}"))
    return lines


def bulk_tree(repo_root: Path, runs: list[dict[str, Any]], events: list[RunEventRecord]) -> Path:
    """Return a canary tree holding ``runs``, its run ledger written in one go."""
    tree_root = native_tree(repo_root, runs, [])
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    ledger = ledger_path(document, Epoch2Collection.RUN)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for item in events:
            record = LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=item.event_ref,
                status="recorded",
                recorded_at=item.recorded_at,
                payload=item.model_dump(mode="json"),
            )
            handle.write(f"{render_ledger_line(record)}\n")
    return tree_root


def synthetic_tree(repo_root: Path, runs: int, per_run: int) -> Path:
    """Return a tree of ``runs`` finished Runs with ``per_run`` event lines each."""
    keys = [run_key(index) for index in range(1, runs + 1)]
    return bulk_tree(repo_root, [run_row(key) for key in keys], events_of(keys, per_run))


@pytest.fixture
def validated(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the Run key of every run-event line validated anywhere."""
    seen: list[str] = []
    original = RunEventRecord.model_validate

    def counting(cls: type[RunEventRecord], obj: Any, *args: Any, **kwargs: Any) -> Any:
        record = original(obj, *args, **kwargs)
        seen.append(record.run_ref.entity_key)
        return record

    monkeypatch.setattr(RunEventRecord, "model_validate", classmethod(counting))
    yield seen


def _reflect(
    tree_root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    window: str | None = None,
    scope: str = "project",
    fmt: _ReportFormat = _ReportFormat.JSON,
) -> tuple[str, str]:
    """Run the ``reflect run`` handler over ``tree_root``; return its stdout and stderr."""
    capsys.readouterr()
    ctx = cast(typer.Context, SimpleNamespace(obj=GlobalFlags(workspace=tree_root.parent)))
    reflect_run(ctx, local_only=True, out=None, window=window, scope=scope, fmt=fmt)
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_linear_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    validated: list[str],
) -> None:
    """Validations equal the line count; doubling the Runs doubles them; no Run is read twice."""
    monkeypatch.delenv("EA_STATE", raising=False)
    per_run = 3
    counts: dict[int, int] = {}
    for runs in (200, 400):
        tree_root = synthetic_tree(tmp_path / f"r{runs}", runs, per_run)
        validated.clear()
        _reflect(tree_root, capsys)
        counts[runs] = len(validated)
        assert counts[runs] == runs * per_run
        assert set(Counter(validated).values()) == {per_run}
    assert counts[400] == 2 * counts[200]


@pytest.fixture
def placed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the scope fixture's placed Runs, each with two event lines."""
    monkeypatch.delenv("EA_STATE", raising=False)
    keys = [run_key(index) for index in range(1, 6)]
    rows = [
        on_task(run_row(keys[0]), "RFL-0001"),
        on_task(run_row(keys[1]), "RFL-0002"),
        on_task(recent(run_row(keys[2])), "RFL-0003"),
        on_task(run_row(keys[3]), "RFL-0004"),
        on_task(run_row(keys[4], status="QUEUED"), "RFL-0001"),
    ]
    tree_root = bulk_tree(tmp_path, rows, events_of(keys, 2))
    place(tree_root)
    return tree_root


def _progress(stderr: str) -> list[tuple[str, int, int]]:
    lines = stderr.splitlines()
    found = [_PROGRESS.match(line) for line in lines]
    assert all(found), lines
    return [(m.group(1), int(m.group(2)), int(m.group(3))) for m in found if m is not None]


@pytest.mark.parametrize(
    ("window", "scope", "kept"),
    [
        ("7d", "project", {"RUN-00000003"}),
        (None, "milestone:MLS-0104", {"RUN-00000001", "RUN-00000005"}),
        ("7d", "milestone:MLS-0104", set()),
    ],
    ids=["window", "scope", "both-empty"],
)
def test_filter_first_and_progress(
    placed_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    validated: list[str],
    window: str | None,
    scope: str,
    kept: set[str],
) -> None:
    """Runs outside --window or --scope never reach the reader; progress goes to stderr only."""
    asked: list[set[str] | None] = []

    def logging_reader(
        records: Sequence[LedgerRecord], run_keys: set[str] | None = None
    ) -> Mapping[Any, tuple[RunEventRecord, ...]]:
        asked.append(None if run_keys is None else set(run_keys))
        return run_events_by_run(records, run_keys)

    monkeypatch.setattr(runs_module, "run_events_by_run", logging_reader)
    out, err = _reflect(placed_tree, capsys, window=window, scope=scope)

    assert asked == [kept]
    assert set(validated) == kept
    document = json.loads(out)
    assert {line["run_key"] for line in document["sessions"]} == kept
    assert "reflect:" not in out
    steps = _progress(err)
    phases = [phase for phase, _ in groupby(phase for phase, *_ in steps)]
    assert phases == ["load", "events", "analyse", "index", "sessions", "render"]
    assert ("load", 5, 5) in steps
    assert ("events", len(kept), len(kept)) in steps
    assert ("analyse", len(kept), len(kept)) in steps
    assert steps[-1] == ("render", 2, 2)
    assert all(0 <= done <= total for _, done, total in steps)


def test_text_report_stdout_carries_no_progress(
    placed_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The text report's stdout ends with its path, and every progress line is on stderr."""
    out, err = _reflect(placed_tree, capsys, fmt=_ReportFormat.TEXT)
    assert "reflect:" not in out
    assert out.rstrip().splitlines()[-1].startswith("report: ")
    assert len(_progress(err)) >= 4


# --- run_events_by_run ------------------------------------------------------


def _records(events: Sequence[RunEventRecord], *extra: dict[str, Any]) -> list[LedgerRecord]:
    rows = [item.model_dump(mode="json") for item in events]
    return [
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"REC-{index}",
            status="recorded",
            recorded_at=AT,
            payload=payload,
        )
        for index, payload in enumerate([*rows, *extra])
    ]


def _urn(key: str) -> Any:
    return parse_qualified_urn(f"{RUN_PREFIX}/{key}")


def test_grouping_of_an_empty_ledger_is_empty() -> None:
    assert run_events_by_run([]) == {}
    assert run_events_by_run([], set()) == {}


def test_grouping_keeps_ledger_order_per_run() -> None:
    lines = events_of(["RUN-00000001", "RUN-00000002"], 3)
    grouped = run_events_by_run(_records(lines))
    assert list(grouped) == [_urn("RUN-00000001"), _urn("RUN-00000002")]
    assert [item.run_sequence for item in grouped[_urn("RUN-00000001")]] == [1, 2, 3]
    assert sum(map(len, grouped.values())) == len(lines)


def test_grouping_of_a_single_line() -> None:
    (line,) = events_of(["RUN-00000001"], 1)
    assert run_events_by_run(_records([line])) == {_urn("RUN-00000001"): (line,)}


def test_grouping_skips_lines_that_are_not_run_events() -> None:
    lines = events_of(["RUN-00000001"], 1)
    grouped = run_events_by_run(_records(lines, {"key": "RUN-00000009", "status": "COMPLETED"}))
    assert list(grouped) == [_urn("RUN-00000001")]


def test_grouping_validates_only_the_named_runs(validated: list[str]) -> None:
    """A key one digit off another is not a match; an empty selection validates nothing."""
    lines = events_of(["RUN-00000001", "RUN-00000010", "RUN-00000011"], 2)
    validated.clear()
    grouped = run_events_by_run(_records(lines), {"RUN-00000010"})
    assert list(grouped) == [_urn("RUN-00000010")]
    assert validated == ["RUN-00000010", "RUN-00000010"]
    validated.clear()
    assert run_events_by_run(_records(lines), set()) == {}
    assert validated == []


def test_grouping_raises_on_a_corrupt_event_line() -> None:
    corrupt = {"payload_kind": "run_event", "run_ref": f"{RUN_PREFIX}/RUN-00000001"}
    with pytest.raises(ValidationError):
        run_events_by_run(_records([], corrupt))


def test_grouping_raises_on_a_run_reference_of_the_wrong_type() -> None:
    corrupt = {"payload_kind": "run_event", "run_ref": 7}
    with pytest.raises(ValidationError):
        run_events_by_run(_records([], corrupt), {"RUN-00000001"})


def test_grouping_skips_a_corrupt_line_of_an_unselected_run() -> None:
    corrupt = {"payload_kind": "run_event", "run_ref": f"{RUN_PREFIX}/RUN-00000002"}
    lines = events_of(["RUN-00000001"], 1)
    assert list(run_events_by_run(_records(lines, corrupt), {"RUN-00000001"})) == [
        _urn("RUN-00000001")
    ]


# --- read_tree_runs selection -------------------------------------------------


def test_read_tree_runs_keeps_every_run_without_a_filter(tmp_path: Path) -> None:
    tree_root = synthetic_tree(tmp_path, 3, 2)
    readings = read_tree_runs(tree_root)
    assert [item.run.key for item in readings] == [run_key(n) for n in (1, 2, 3)]
    assert [len(item.events) for item in readings] == [2, 2, 2]


def test_read_tree_runs_admits_nothing(tmp_path: Path, validated: list[str]) -> None:
    tree_root = synthetic_tree(tmp_path, 3, 2)
    validated.clear()
    steps: list[tuple[str, int, int]] = []
    assert read_tree_runs(tree_root, admits=lambda _: False, progress=_collect(steps)) == ()
    assert validated == []
    assert ("load", 3, 3) in steps
    assert ("analyse", 0, 0) in steps


def test_read_tree_runs_started_since_is_inclusive(tmp_path: Path) -> None:
    """A Run started exactly at the bound is kept; one a microsecond later bound drops it."""
    tree_root = bulk_tree(tmp_path, [run_row(run_key(1)), run_row(run_key(2), status="QUEUED")], [])
    assert [item.run.key for item in read_tree_runs(tree_root, started_since=AT)] == [run_key(1)]
    later = AT + timedelta(microseconds=1)
    assert read_tree_runs(tree_root, started_since=later) == ()


def test_read_tree_runs_admits_judges_a_reading_without_events(tmp_path: Path) -> None:
    tree_root = synthetic_tree(tmp_path, 2, 2)
    judged: list[int] = []

    def admits(reading: RunReading) -> bool:
        judged.append(len(reading.events))
        return reading.run.key == run_key(2)

    (reading,) = read_tree_runs(tree_root, admits=admits)
    assert judged == [0, 0]
    assert reading.run.key == run_key(2) and len(reading.events) == 2


def test_read_tree_runs_progress_counts_each_phase(tmp_path: Path) -> None:
    tree_root = synthetic_tree(tmp_path, 25, 1)
    steps: list[tuple[str, int, int]] = []
    read_tree_runs(tree_root, progress=_collect(steps))
    assert [step for step in steps if step[0] == "events"] == [
        ("events", 0, 25),
        ("events", 25, 25),
    ]
    analyse = [done for phase, done, _ in steps if phase == "analyse"]
    assert analyse[0] == 0 and analyse[-1] == 25 and analyse == sorted(analyse)
    assert len(analyse) <= 15


def test_read_tree_runs_raises_on_a_corrupt_kept_line(tmp_path: Path) -> None:
    tree_root = synthetic_tree(tmp_path, 1, 1)
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    corrupt = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key="EVT-ffffffff",
        status="recorded",
        recorded_at=AT,
        payload={"payload_kind": "run_event", "run_ref": f"{RUN_PREFIX}/{run_key(1)}"},
    )
    with ledger_path(document, Epoch2Collection.RUN).open("a", encoding="utf-8") as handle:
        handle.write(f"{render_ledger_line(corrupt)}\n")
    with pytest.raises(ValidationError):
        read_tree_runs(tree_root)


def _collect(steps: list[tuple[str, int, int]]) -> Any:
    def sink(phase: str, done: int, total: int) -> None:
        steps.append((phase, done, total))

    return sink
