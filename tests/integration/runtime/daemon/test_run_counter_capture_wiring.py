"""The daemon records a Run's counter readings on its start and stop edges.

The per-entity Run verbs are driven through the registered handlers against
a provisioned canary, with the capture producer pointed at a temporary
Claude transcript root. The readings must land in the same commit as the
edge, and a retry must replay the original receipt even though it reads
the transcript again.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    CounterSnapshot,
    MeasuredRuntime,
    Observed,
    UncapturedReason,
    UncapturedRuntime,
    VendorSessionRef,
)
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.runtime_counter_sidecar import RuntimeCounterSidecar
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from tests.integration.runtime.daemon._delivery_verb_fixtures import ENDED_AT, RUN_URN
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

ACTOR = "OP-0001"
SESSION = "5f0c6a8e-1111-4222-8333-944455556666"
STARTED_AT = "2026-09-08T01:00:00Z"


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep canary runtimes, transcripts and sidecars under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    projects = tmp_path / "projects"
    (projects / "-repo").mkdir(parents=True)
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "statusline-cache"))
    return projects


def _transcript(projects: Path, *, output: int, until: str) -> None:
    rows = [
        {"type": "user", "timestamp": "2026-09-08T00:59:00Z", "message": {"content": "go"}},
        {
            "type": "assistant",
            "timestamp": "2026-09-08T00:59:30Z",
            "message": {
                "id": "m1",
                "model": "claude-opus-4-1",
                "usage": {"input_tokens": 5, "output_tokens": 10},
                "content": [],
            },
        },
        {"type": "system", "subtype": "turn_duration", "durationMs": 30_000, "timestamp": until},
    ]
    if output > 10:
        rows.insert(
            2,
            {
                "type": "assistant",
                "timestamp": "2026-09-08T01:30:00Z",
                "message": {
                    "id": "m2",
                    "model": "claude-opus-4-1",
                    "usage": {"input_tokens": 5, "output_tokens": output - 10},
                    "content": [],
                },
            },
        )
    path = projects / "-repo" / f"{SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _drive(
    canary: CanaryProvision, tmp_path: Path, method: str, *, key: str, **params: Any
) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    request = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "expected_revision": 1,
        "idempotency_key": key,
        "actor": ACTOR,
        **params,
    }
    return asyncio.run(methods.dispatch(method, ctx, request))


def _stored_run(canary: CanaryProvision) -> Run:
    path = document_path(canary)
    row = document_rows(json.loads(path.read_text()), Epoch2Collection.RUN).get("RUN-00000010")
    if row is not None:
        return Run.model_validate(row)
    lines = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    return Run.model_validate(
        next(line.payload for line in lines if line.record_key == "RUN-00000010")
    )


def _queued(tmp_path: Path, **others: dict[str, Any]) -> CanaryProvision:
    canary = provision(tmp_path / "ok", code="OK")
    seed(canary, {"run": {"RUN-00000010": seed_row("run", "QUEUED"), **others}})
    return canary


def _start(
    canary: CanaryProvision, tmp_path: Path, *, key: str = "start-1", **updates: Any
) -> dict[str, Any]:
    return _drive(
        canary,
        tmp_path,
        "domain.run.start",
        key=key,
        updates={"started_at": STARTED_AT, **updates},
    )


def _session() -> dict[str, str]:
    return {"harness": "claude-code", "session_digest": SESSION}


def test_meas_001_start_verb_records_the_baseline_through_the_vendor_session(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path)

    answer = _start(canary, tmp_path, vendor_session=_session())

    assert answer["status"] == "ok", answer["errors"]
    run = _stored_run(canary)
    assert run.vendor_session == VendorSessionRef(harness="claude-code", session_digest=SESSION)
    assert run.vendor_session is not None
    assert run.vendor_session.session_digest == hash_vendor_session_id(SESSION)
    assert isinstance(run.counter_baseline, CounterSnapshot)
    assert run.counter_baseline.concurrent_run_count == 1
    assert SESSION not in document_path(canary).read_text()


def test_meas_005_start_counts_runs_already_sharing_the_session(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    digest = hash_vendor_session_id(SESSION)
    sharer = {
        **seed_row("run", "RUNNING"),
        "key": "RUN-00000011",
        "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011",
        "uid": "5b4e28ba-2fa1-11d2-883f-0016d3cca428",
        "vendor_session": {"harness": "claude-code", "session_digest": digest},
    }
    canary = _queued(tmp_path, **{"RUN-00000011": sharer})

    _start(canary, tmp_path, vendor_session=_session())

    baseline = _stored_run(canary).counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert baseline.concurrent_run_count == 2


def test_meas_003_start_without_vendor_session_records_no_captured_runtime(
    tmp_path: Path,
) -> None:
    canary = _queued(tmp_path)

    answer = _start(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    run = _stored_run(canary)
    assert run.vendor_session is None
    assert run.counter_baseline == UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)


def test_meas_000_malformed_vendor_session_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    canary = _queued(tmp_path)

    answer = _start(
        canary, tmp_path, vendor_session={"harness": "Claude Code", "session_digest": ""}
    )

    assert answer["status"] == "error"
    assert _stored_run(canary).status.value == "QUEUED"


def test_meas_001_retry_of_a_captured_start_replays_its_receipt(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path)
    first = _start(canary, tmp_path, vendor_session=_session())
    _transcript(isolated_roots, output=40, until="2026-09-08T01:40:00Z")

    retry = _start(canary, tmp_path, vendor_session=_session())

    assert retry["status"] == "ok", retry["errors"]
    assert retry["result"] == first["result"]


def test_meas_001_finish_verb_records_the_runs_measured_share(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path)
    _start(canary, tmp_path, vendor_session=_session())
    _transcript(isolated_roots, output=40, until="2026-09-08T01:40:00Z")

    answer = _drive(
        canary,
        tmp_path,
        "domain.run.finish",
        key="finish-1",
        observations=["run_report_bound"],
        updates={"ended_at": ENDED_AT},
        expected_revision=2,
    )

    assert answer["status"] == "ok", answer["errors"]
    captured = _stored_run(canary).captured_runtime
    assert isinstance(captured, MeasuredRuntime)
    assert captured.divisor == 1
    assert str(captured.counters["output_tokens"].model_dump()["value"]) == "30"


def _sharer() -> dict[str, Any]:
    return {
        **seed_row("run", "RUNNING"),
        "key": "RUN-00000011",
        "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011",
        "uid": "5b4e28ba-2fa1-11d2-883f-0016d3cca428",
        "vendor_session": {
            "harness": "claude-code",
            "session_digest": hash_vendor_session_id(SESSION),
        },
    }


def _finish(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    return _drive(
        canary,
        tmp_path,
        "domain.run.finish",
        key="finish-1",
        observations=["run_report_bound"],
        updates={"ended_at": ENDED_AT},
        expected_revision=2,
    )


def test_run_027_start_and_terminal_readings_record_their_source(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path)
    _start(canary, tmp_path, vendor_session=_session())
    _transcript(isolated_roots, output=40, until="2026-09-08T01:40:00Z")

    assert _finish(canary, tmp_path)["status"] == "ok"

    run = _stored_run(canary)
    assert isinstance(run.counter_baseline, CounterSnapshot)
    assert run.counter_baseline.source is CaptureSource.TRANSCRIPT
    assert isinstance(run.captured_runtime, MeasuredRuntime)
    assert run.captured_runtime.source is CaptureSource.TRANSCRIPT


def test_run_027_sidecar_is_read_when_no_transcript_resolves(tmp_path: Path) -> None:
    cache = Path(os.environ["EAWF_STATUSLINE_CACHE"])
    sidecar = RuntimeCounterSidecar(cache / f"{SESSION}.runtime-counters.json")
    sidecar.write(RuntimeCounters(output_tokens=5, measure_version=101, harness="claude-code"))
    canary = _queued(tmp_path)
    _start(canary, tmp_path, vendor_session=_session())
    sidecar.write(RuntimeCounters(output_tokens=25, measure_version=101, harness="claude-code"))

    assert _finish(canary, tmp_path)["status"] == "ok"

    run = _stored_run(canary)
    assert isinstance(run.counter_baseline, CounterSnapshot)
    assert run.counter_baseline.source is CaptureSource.SIDECAR
    captured = run.captured_runtime
    assert isinstance(captured, MeasuredRuntime)
    assert captured.source is CaptureSource.SIDECAR
    assert captured.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(20))


def test_run_027_neither_source_records_no_captured_runtime(tmp_path: Path) -> None:
    canary = _queued(tmp_path)
    _start(canary, tmp_path, vendor_session=_session())

    assert _finish(canary, tmp_path)["status"] == "ok"

    run = _stored_run(canary)
    assert run.counter_baseline == UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)
    assert run.captured_runtime == UncapturedRuntime(reason=UncapturedReason.NO_BASELINE)


def test_run_028_start_baseline_records_the_concurrent_run_divisor(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path, **{"RUN-00000011": _sharer()})

    _start(canary, tmp_path, vendor_session=_session())

    baseline = _stored_run(canary).counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert baseline.concurrent_run_count == 2


def test_run_028_shared_interval_is_split_not_handed_whole(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    canary = _queued(tmp_path, **{"RUN-00000011": _sharer()})
    _start(canary, tmp_path, vendor_session=_session())
    _transcript(isolated_roots, output=40, until="2026-09-08T01:40:00Z")

    assert _finish(canary, tmp_path)["status"] == "ok"

    captured = _stored_run(canary).captured_runtime
    assert isinstance(captured, MeasuredRuntime)
    assert captured.divisor == 2
    assert captured.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(15))


def test_run_028_a_different_session_does_not_share_the_interval(
    tmp_path: Path, isolated_roots: Path
) -> None:
    _transcript(isolated_roots, output=10, until="2026-09-08T00:59:40Z")
    other = _sharer()
    other["vendor_session"]["session_digest"] = hash_vendor_session_id("another-session")
    canary = _queued(tmp_path, **{"RUN-00000011": other})

    _start(canary, tmp_path, vendor_session=_session())

    baseline = _stored_run(canary).counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert baseline.concurrent_run_count == 1
