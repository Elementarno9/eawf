"""A root Run started from inside a host session is measured through it.

``eawf run start`` runs inside the agent host's session, and the host names
that session in the command's environment. These tests drive the real
Typer commands against the daemon's registered verbs, in process, on a
provisioned canary: the start command presents the host session, the
daemon's start edge takes the baseline through it, and the finish edge
banks the Run's share -- the live path a root Run takes, with no synthetic
``vendor_session`` in the request document.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    CounterSnapshot,
    ExcludedRuntime,
    ExclusionReason,
    MeasuredRuntime,
    Observed,
    SpanSummary,
    UncapturedReason,
    UncapturedRuntime,
    Unobserved,
    VendorSessionRef,
)
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.runtimes.claude.statusline_modules import scope
from eawf.runtime.runtimes.claude.transcript_counters import MEASURE_VERSION
from eawf.runtime.session.host_session import host_vendor_session, with_host_session
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.runtime.daemon._delivery_verb_fixtures import ENDED_AT, RUN_URN
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient
from tests.integration.runtime.daemon.test_run_counter_capture_wiring import _stored_run

pytestmark = pytest.mark.integration

runner = CliRunner()

SESSION: Final = "7a1d2c3b-4444-4555-8666-977788889999"
MODEL: Final = "claude-opus-4-1"
STARTED_AT: Final = "2026-09-08T01:00:00Z"


@pytest.fixture(autouse=True)
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Route the CLI into the registered verbs, with every capture root under tmp.

    Yields:
        The Claude projects root the capture producer reads transcripts from.
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "-repo").mkdir(parents=True)
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "statusline-cache"))
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", InProcessClient)
    InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    yield projects
    InProcessClient.context = None


def _message(stamp: str, message_id: str, *, output: int, **content: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {
            "id": message_id,
            "model": MODEL,
            "usage": {"input_tokens": 5, "output_tokens": output},
            "content": [content] if content else [],
        },
    }


def _head() -> list[dict[str, Any]]:
    """Return the host turn that finished before the Run started."""
    return [
        {"type": "user", "timestamp": "2026-09-08T00:59:00Z", "message": {"content": "go"}},
        _message("2026-09-08T00:59:30Z", "m1", output=10),
        {
            "type": "system",
            "subtype": "turn_duration",
            "durationMs": 30_000,
            "timestamp": "2026-09-08T00:59:40Z",
        },
    ]


def _after_start() -> list[dict[str, Any]]:
    """Return the host turn the Run's own work fills."""
    return [
        {"type": "user", "timestamp": "2026-09-08T01:10:00Z", "message": {"content": "work"}},
        _message("2026-09-08T01:20:00Z", "m2", output=30, type="tool_use", id="t1"),
        {
            "type": "user",
            "timestamp": "2026-09-08T01:25:00Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "durationMs": 1_200_000,
            "timestamp": "2026-09-08T01:30:00Z",
        },
    ]


def _transcript(projects: Path, rows: list[dict[str, Any]]) -> None:
    path = projects / "-repo" / f"{SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _queued(tmp_path: Path, **others: dict[str, Any]) -> CanaryProvision:
    canary = provision(tmp_path / "ok", code="OK")
    seed(canary, {"run": {"RUN-00000010": seed_row("run", "QUEUED"), **others}})
    return canary


def _eawf(canary: CanaryProvision, tmp_path: Path, verb: str, document: dict[str, Any]) -> None:
    """Run one ``eawf run <verb>`` against the canary and require it to commit."""
    spec = tmp_path / f"{verb}.json"
    spec.write_text(json.dumps(document), encoding="utf-8")
    revision = "1" if verb == "start" else "2"
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(canary.root),
            "--json",
            "run",
            verb,
            RUN_URN,
            "--expected-run-revision",
            revision,
            "--idempotency-key",
            f"{verb}-1",
            "--actor",
            "OP-0001",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == 0, result.output


def _start(canary: CanaryProvision, tmp_path: Path, **updates: Any) -> None:
    _eawf(canary, tmp_path, "start", {"updates": {"started_at": STARTED_AT, **updates}})


def _finish(canary: CanaryProvision, tmp_path: Path) -> None:
    _eawf(
        canary,
        tmp_path,
        "finish",
        {"updates": {"ended_at": ENDED_AT}, "observations": ["run_report_bound"]},
    )


def _hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def _measured(canary: CanaryProvision) -> MeasuredRuntime:
    captured = _stored_run(canary).captured_runtime
    assert isinstance(captured, MeasuredRuntime), captured
    return captured


def test_meas_000_run_start_presents_the_host_session_it_runs_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)

    _start(canary, tmp_path)

    run = _stored_run(canary)
    assert run.parent_run_ref is None
    assert run.vendor_session == VendorSessionRef(
        harness="claude-code", session_digest=hash_vendor_session_id(SESSION)
    )
    assert SESSION not in document_path(canary).read_text(encoding="utf-8")


def test_meas_000_a_session_the_request_names_is_kept_over_the_host_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted(monkeypatch)
    canary = _queued(tmp_path)

    _start(canary, tmp_path, vendor_session={"harness": "claude-code", "session_digest": "other"})

    run = _stored_run(canary)
    assert run.vendor_session is not None
    assert run.vendor_session.session_digest == hash_vendor_session_id("other")


def test_meas_001_meas_002_meas_004_meas_010_root_run_is_measured_from_start_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)
    _start(canary, tmp_path)
    _transcript(cli, [*_head(), *_after_start()])

    _finish(canary, tmp_path)

    run = _stored_run(canary)
    baseline = run.counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert baseline.source is CaptureSource.TRANSCRIPT
    assert baseline.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(10))
    captured = _measured(canary)
    assert captured.source is CaptureSource.TRANSCRIPT
    assert (captured.harness, captured.model) == ("claude-code", MODEL)
    assert captured.measurement_version == MEASURE_VERSION
    assert captured.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(30))


def test_meas_003_run_start_outside_a_host_session_records_no_captured_runtime(
    tmp_path: Path,
) -> None:
    canary = _queued(tmp_path)

    _start(canary, tmp_path)
    _finish(canary, tmp_path)

    run = _stored_run(canary)
    assert run.vendor_session is None
    assert run.counter_baseline == UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)
    assert run.captured_runtime == UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)


def test_meas_005_meas_008_root_runs_sharing_a_host_session_split_its_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    sharer = {
        **seed_row("run", "RUNNING"),
        "key": "RUN-00000011",
        "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011",
        "uid": "5b4e28ba-2fa1-11d2-883f-0016d3cca428",
        "vendor_session": {
            "harness": "claude-code",
            "session_digest": hash_vendor_session_id(SESSION),
        },
    }
    canary = _queued(tmp_path, **{"RUN-00000011": sharer})
    _start(canary, tmp_path)
    _transcript(cli, [*_head(), *_after_start()])

    _finish(canary, tmp_path)

    baseline = _stored_run(canary).counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert baseline.concurrent_run_count == 2
    captured = _measured(canary)
    assert captured.divisor == 2
    assert captured.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(15))


def test_meas_006_root_run_started_mid_turn_banks_only_the_share_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    # The host turn opened at 00:50 and reports its duration only when it
    # ends at 01:10, after the Run started at 01:00.
    turn = [
        {"type": "user", "timestamp": "2026-09-08T00:50:00Z", "message": {"content": "go"}},
        _message("2026-09-08T00:55:00Z", "m1", output=10),
    ]
    _transcript(cli, turn)
    canary = _queued(tmp_path)
    _start(canary, tmp_path)
    ended = {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 1_200_000,
        "timestamp": "2026-09-08T01:10:00Z",
    }
    _transcript(cli, [*turn, _message("2026-09-08T01:05:00Z", "m2", output=10), ended])

    _finish(canary, tmp_path)

    captured = _measured(canary)
    assert captured.derived is True
    duration = captured.counters[CounterName.DURATION_MS]
    assert isinstance(duration, Observed)
    assert duration.value < 1_200_000


def test_meas_009_root_run_whose_counters_fell_is_excluded_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)
    _start(canary, tmp_path)
    _transcript(cli, [{"type": "user", "timestamp": "2026-09-08T01:10:00Z", "message": {}}])

    _finish(canary, tmp_path)

    excluded = _stored_run(canary).captured_runtime
    assert isinstance(excluded, ExcludedRuntime)
    assert excluded.reason is ExclusionReason.COUNTER_RESET
    assert excluded.detail


def test_meas_050_root_run_spans_attribute_to_a_named_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)
    _start(canary, tmp_path)
    _transcript(cli, [*_head(), *_after_start()])

    _finish(canary, tmp_path)

    spans = _measured(canary).spans
    assert isinstance(spans, SpanSummary)
    assert spans.attributed_ms > 0
    assert spans.attributed_ms + spans.unattributed_ms == spans.total_ms


def test_meas_053_root_run_keeps_an_unreported_class_unobserved_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)

    _start(canary, tmp_path)

    baseline = _stored_run(canary).counter_baseline
    assert isinstance(baseline, CounterSnapshot)
    assert isinstance(baseline.counters[CounterName.CACHE_READ_INPUT_TOKENS], Unobserved)
    assert baseline.counters[CounterName.INPUT_TOKENS] == Observed(value=Decimal(5))


def test_meas_030_scope_segment_resolves_the_root_run_the_host_session_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted(monkeypatch)
    canary = _queued(tmp_path)
    before = scope.build({"session_id": SESSION}, canary.root / ".ea" / "state.json")

    _start(canary, tmp_path)

    segment = scope.build({"session_id": SESSION}, canary.root / ".ea" / "state.json")
    assert before.text == "scope:n/a(no-run-for-session)"
    assert segment.text.startswith("scope:")
    assert "n/a" not in segment.text
    assert segment.truth.provenance_refs == (RUN_URN,)


def test_meas_000_host_session_reads_only_a_named_non_blank_session() -> None:
    assert host_vendor_session({}) is None
    assert host_vendor_session({"CLAUDE_CODE_SESSION_ID": "  "}) is None
    assert host_vendor_session({"CLAUDE_CODE_SESSION_ID": SESSION}) == VendorSessionRef(
        harness="claude-code", session_digest=SESSION
    )
    assert with_host_session({"started_at": STARTED_AT}, {}) == {"started_at": STARTED_AT}
