"""A child Run's counters: read from its own transcript, folded into its root.

MEAS-001: a subagent's transcript is its own file under its spawning session, so its
Run's start and stop snapshots are read there; a root Run and its descendants fold into
one account per counter -- provider total, inherited baseline, accepted steps, root and
descendant shares -- with the residual beside them, and a non-zero residual is a
reconciliation failure the Run report states.

RUN-023: the child's capture never reads the transcript of the session that spawned it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    CounterSnapshot,
    MeasuredRuntime,
    Observed,
    UncapturedReason,
    UncapturedRuntime,
    Unobserved,
    VendorSessionRef,
)
from eawf.kernel.state.epoch2.run import Run
from eawf.observability.measurement.capture import capture_run_start, capture_run_terminal
from eawf.observability.measurement.fold import CounterFold, fold_subtree
from eawf.observability.reflect.run_report import ReportPartName, plan_run_report
from eawf.observability.reflect.runs import RunReading
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row

pytestmark = pytest.mark.unit

SESSION: Final = "5f0c6a8e-1111-4222-8333-944455556666"
AGENT: Final = "subagent01"
OTHER_AGENT: Final = "subagent02"
MODEL: Final = "claude-opus-4-1"
T0: Final = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)


def _stamp(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _message(second: float, ident: str, *, output: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": _stamp(second),
        "message": {
            "id": ident,
            "model": MODEL,
            "usage": {
                "input_tokens": 10,
                "output_tokens": output,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "x"}],
        },
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    (root / "-repo").mkdir(parents=True)
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "statusline-cache"))
    return root


def _subagent(projects: Path, rows: list[dict[str, Any]], *, agent: str = AGENT) -> Path:
    return _write(projects / "-repo" / SESSION / "subagents" / f"agent-{agent}.jsonl", rows)


def _output(reading: CounterSnapshot | MeasuredRuntime) -> Decimal:
    value = reading.counters[CounterName.OUTPUT_TOKENS]
    assert isinstance(value, Observed)
    return value.value


# ---- MEAS-001: a subagent session is captured from its own transcript ---------------


def test_meas_001_subagent_session_is_snapshotted_at_start_and_stop(projects: Path) -> None:
    path = _subagent(projects, [_message(1, "m1", output=40)])
    ref = VendorSessionRef(harness="claude-code", session_digest=AGENT)
    baseline = capture_run_start(ref, at=T0 + timedelta(seconds=2), concurrent_run_count=1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    _subagent(projects, [*rows, _message(4, "m2", output=25)])

    captured = capture_run_terminal(
        ref, baseline=baseline, started_at=T0 + timedelta(seconds=2), at=T0 + timedelta(seconds=5)
    )

    assert isinstance(baseline, CounterSnapshot)
    assert baseline.source is CaptureSource.TRANSCRIPT
    assert _output(baseline) == 40
    assert isinstance(captured, MeasuredRuntime)
    assert _output(captured) == 25


def test_meas_001_another_subagents_transcript_is_not_read(projects: Path) -> None:
    _subagent(projects, [_message(1, "m1", output=40)], agent=OTHER_AGENT)
    ref = VendorSessionRef(harness="claude-code", session_digest=AGENT)

    assert capture_run_start(ref, at=T0, concurrent_run_count=1) == UncapturedRuntime(
        reason=UncapturedReason.NO_SOURCE
    )


def test_run_023_child_capture_never_reads_the_parent_transcript(projects: Path) -> None:
    _write(projects / "-repo" / f"{SESSION}.jsonl", [_message(1, "p1", output=900)])
    _subagent(projects, [_message(1, "c1", output=7)])

    child = capture_run_start(
        VendorSessionRef(harness="claude-code", session_digest=AGENT),
        at=T0 + timedelta(seconds=2),
        concurrent_run_count=1,
    )
    parent = capture_run_start(
        VendorSessionRef(harness="claude-code", session_digest=SESSION),
        at=T0 + timedelta(seconds=2),
        concurrent_run_count=1,
    )

    assert isinstance(child, CounterSnapshot)
    assert isinstance(parent, CounterSnapshot)
    assert (_output(child), _output(parent)) == (Decimal(7), Decimal(900))


# ---- MEAS-001: the subtree fold -------------------------------------------------------


def _counters(output: int | None) -> dict[str, Any]:
    reading = (
        {"state": "unobserved", "reason": "not reported"}
        if output is None
        else {"state": "observed", "value": str(output)}
    )
    counters: dict[str, Any] = {
        name.value: {"state": "observed", "value": "0"} for name in CounterName
    }
    counters[CounterName.OUTPUT_TOKENS.value] = reading
    return counters


def _run(
    key: int,
    *,
    parent: int | None = None,
    session: str | None = None,
    baseline: int | None = 0,
    share: int | None = 0,
    divisor: int = 1,
) -> Run:
    """Return a completed Run; a ``session`` of ``None`` records no capture at all."""
    row = seed_row("run", "COMPLETED")
    row["key"] = f"RUN-{key:08d}"
    row["urn"] = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-{key:08d}"
    if parent is not None:
        row["parent_run_ref"] = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-{parent:08d}"
    if session is None:
        row["captured_runtime"] = {"outcome": "uncaptured", "reason": "no_vendor_session"}
        return Run.model_validate(row)
    row["vendor_session"] = {"harness": "claude-code", "session_digest": session}
    row["counter_baseline"] = {
        "captured_at": row["started_at"],
        "source": "transcript",
        "harness": "claude-code",
        "model": MODEL,
        "measurement_version": 1,
        "concurrent_run_count": divisor,
        "counters": _counters(baseline),
    }
    row["captured_runtime"] = {
        "outcome": "measured",
        "source": "transcript",
        "harness": "claude-code",
        "model": MODEL,
        "measurement_version": 1,
        "divisor": divisor,
        "derived": False,
        "measurement_quality": "measured" if divisor == 1 else "derived",
        "reconstruction_basis": None if divisor == 1 else "recorded_in_transcript",
        "counters": _counters(share),
        "spans": {"state": "unobserved", "reason": "no spans"},
    }
    return Run.model_validate(row)


def _output_fold(fold: Any) -> CounterFold:
    counter = fold.counters[CounterName.OUTPUT_TOKENS]
    assert isinstance(counter, CounterFold)
    return counter


def test_meas_001_run_without_descendants_has_no_fold() -> None:
    root = _run(1, session="root", baseline=100, share=20)

    assert fold_subtree(root, [root]) is None


def test_meas_001_root_and_children_fold_with_zero_residual() -> None:
    root = _run(1, session="root", baseline=100, share=20)
    child = _run(2, parent=1, session="child-a", baseline=0, share=30)
    grandchild = _run(3, parent=2, session="child-b", baseline=0, share=5)
    stranger = _run(4, session="other", baseline=0, share=999)

    fold = fold_subtree(root, [root, child, grandchild, stranger])

    assert fold is not None
    assert fold.descendant_keys == ("RUN-00000002", "RUN-00000003")
    assert fold.sources == 3
    assert fold.unmeasured_keys == ()
    counter = _output_fold(fold)
    assert counter.provider_total == 155
    assert counter.inherited_baseline == 100
    assert counter.accepted_step_total == 55
    assert (counter.root_share, counter.descendant_share) == (Decimal(20), Decimal(35))
    assert counter.residual == 0
    assert fold.unreconciled == ()


def test_meas_001_source_shared_by_root_and_child_is_counted_once() -> None:
    # Root and child ran one after the other on one session: a cumulative counter
    # summed per Run would count the root's 20 twice.
    root = _run(1, session="shared", baseline=100, share=20)
    child = _run(2, parent=1, session="shared", baseline=120, share=10)

    fold = fold_subtree(root, [root, child])

    assert fold is not None
    counter = _output_fold(fold)
    assert fold.sources == 1
    assert (counter.provider_total, counter.inherited_baseline) == (Decimal(130), Decimal(100))
    assert counter.accepted_step_total == 30
    assert counter.residual == 0


def test_meas_001_usage_no_run_accepted_is_a_non_zero_residual() -> None:
    # The child's baseline sits past the root's stop: 15 tokens of the shared session
    # were spent while neither Run was running.
    root = _run(1, session="shared", baseline=100, share=20)
    child = _run(2, parent=1, session="shared", baseline=135, share=10)

    fold = fold_subtree(root, [root, child])

    assert fold is not None
    assert _output_fold(fold).residual == 15
    assert fold.unreconciled == (CounterName.OUTPUT_TOKENS,)


def test_meas_001_session_shared_outside_the_subtree_is_a_non_zero_residual() -> None:
    root = _run(1, session="root", baseline=0, share=10)
    child = _run(2, parent=1, session="busy", baseline=0, share=10, divisor=2)

    fold = fold_subtree(root, [root, child])

    assert fold is not None
    assert _output_fold(fold).residual == 10


def test_meas_001_unobserved_counter_is_not_folded() -> None:
    root = _run(1, session="root", baseline=0, share=10)
    child = _run(2, parent=1, session="child", baseline=0, share=None)

    fold = fold_subtree(root, [root, child])

    assert fold is not None
    counter = fold.counters[CounterName.OUTPUT_TOKENS]
    assert isinstance(counter, Unobserved)
    assert "RUN-00000002" in counter.reason
    assert fold.unreconciled == ()


def test_meas_001_unmeasured_run_is_named_not_zeroed() -> None:
    root = _run(1, session="root", baseline=0, share=10)
    child = _run(2, parent=1)

    fold = fold_subtree(root, [root, child])

    assert fold is not None
    assert fold.unmeasured_keys == ("RUN-00000002",)
    assert fold.sources == 1
    assert _output_fold(fold).descendant_share == 0


def test_meas_001_run_report_states_the_fold_and_a_reconciliation_failure(
    tmp_path: Path,
) -> None:
    root = _run(1, session="shared", baseline=100, share=20)
    child = _run(2, parent=1, session="shared", baseline=135, share=10)
    reading = RunReading(
        run=root, events=(), canonical_sequence=7, fold=fold_subtree(root, [root, child])
    )

    plan = plan_run_report(
        reading,
        parts=(ReportPartName.USAGE_AND_COST,),
        actor=None,
        tree_root=tmp_path / ".ea",
        on=T0.date(),
    )

    text = plan.text()
    assert "usage_and_cost subtree 1 descendants · 1 sources · unmeasured none" in text
    assert (
        "usage_and_cost subtree output_tokens provider 145 · inherited 100 · steps 30"
        " · root 20 · descendants 10 · residual 15"
    ) in text
    assert "usage_and_cost reconciliation failure: non-zero residual on output_tokens" in text


def test_meas_001_run_report_of_a_leaf_run_has_no_subtree_lines(tmp_path: Path) -> None:
    root = _run(1, session="root", baseline=0, share=10)
    reading = RunReading(run=root, events=(), canonical_sequence=7)

    plan = plan_run_report(
        reading,
        parts=(ReportPartName.USAGE_AND_COST,),
        actor=None,
        tree_root=tmp_path / ".ea",
        on=T0.date(),
    )

    assert "subtree" not in plan.text()
