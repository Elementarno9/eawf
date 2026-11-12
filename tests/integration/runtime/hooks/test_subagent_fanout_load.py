"""Side-by-side marked subagents adopt within the deadline on a live-sized run ledger.

The live tree's run ledger holds about 30,000 lines of other Runs' events, control
facts and compacted records, and another session's tool hooks keep taking the
document lock while five subagents start and stop together. Every adoption verb
reads that ledger under the lock several times, so each delivery's share of the
adoption queue depends on what a locked read costs there. This runs the five starts
and the five stops with the real retry deadline and the default lock wait, beside a
thread sending tool calls for another live Run, as the live check does.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.control import ControlDisposition, ControlFact, ControlPhase
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.run import RunPurpose, RunStatus, TaskScope
from eawf.kernel.store.ledger import LedgerRecord, render_ledger_line
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods.host_subagent import ADOPTION_RETRY_DEADLINE_SECONDS
from eawf.runtime.daemon.methods.host_tool import HOST_TOOL_OBSERVE_METHOD
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.health import HookFailure
from eawf.runtime.hooks.host_calls import HOST_TOOL_HOOK
from eawf.runtime.hooks.runner import HookResult, HookRunner, register_runtime_capture_hooks
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    provision,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    RUN_URN,
    _running_run,
)
from tests.integration.runtime.hooks.test_subagent_fanout_binding import (
    FANOUT,
    TASK_CONTAINER,
    _DaemonClient,
    hermetic_runtime,
    latest_runs,
    repository_row,
    routed,
    start_payload,
    stop_payload,
    subagent_transcript,
    write_prompt,
)

pytestmark = pytest.mark.integration

__all__ = ["hermetic_runtime"]

#: The other session, whose tool hooks keep firing on its own live Run.
TOOL_SESSION: Final = "7f3c2a10-5d4e-4b6a-9c8d-0e1f2a3b4c5d"

#: The hook each subagent delivery answers through.
SUBAGENT_HOOK: Final = "runtime.host_subagent"

#: How long the tool sender may take to stop before the test stops waiting for it.
TEST_BUDGET_SECONDS: Final = 60.0

#: The real deadline measures the host, so a parallel suite's own load moves it;
#: an xdist worker shares the host with its sibling workers, so only a serial run counts.
_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1" or "PYTEST_XDIST_WORKER" in os.environ,
    reason="EAWF_SKIP_PERF=1 or an xdist worker - perf gate runs only serially",
)

#: How many lines the synthetic run ledger holds, about the live one's 29,687.
LEDGER_LINES: Final = 30_000

#: How many other Runs the synthetic lines belong to.
OTHER_RUNS: Final = 1_000


def other_key(number: int) -> str:
    """Return the key of the *number*-th other Run, above every key the test adopts."""
    return f"RUN-{100_000 + number:08d}"


def ledger_line(key: str, payload: dict[str, Any], *, status: str) -> str:
    """Return the text of one run ledger line keyed *key*."""
    record = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=key,
        status=status,
        recorded_at=AT,
        payload=payload,
    )
    return render_ledger_line(record)


def control_line(run_urn: str) -> str:
    """Return a requested cancel of the Run at *run_urn*."""
    fact = ControlFact(
        control_request_ref="CTL-0000000a",
        run_ref=parse_qualified_urn(run_urn),
        control=ControlKind.CANCEL,
        phase=ControlPhase.REQUESTED,
        disposition=ControlDisposition.REQUESTING,
        actor="OP-0001",
        recorded_at=AT,
        sequence=1,
    )
    return ledger_line(fact.control_request_ref, fact.model_dump(mode="json"), status="requested")


def run_record_line(key: str) -> str:
    """Return the compacted record of a COMPLETED Run keyed *key*."""
    return ledger_line(key, rekeyed(seed_row("run", "COMPLETED"), key=key), status="COMPLETED")


class _RecordingClient(_DaemonClient):
    """The daemon client of one tool hook process, keeping each observe answer."""

    def __init__(self, runtime: Path, answers: list[dict[str, Any]]) -> None:
        super().__init__(runtime)
        self._answers = answers

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        answer = super().call(method, params)
        if method == HOST_TOOL_OBSERVE_METHOD:
            self._answers.append(answer)
        return answer


@dataclass(frozen=True)
class Delivery:
    """One hook process's answer, its health failures and how long it took."""

    result: HookResult
    failures: list[HookFailure]
    seconds: float


def tool_event(payload: dict[str, Any]) -> HookEvent:
    """Return the event ``eawf hook run`` makes of a tool hook's stdin, keyed by its type."""
    event_type = (
        HookEventType.POST_TOOL_USE
        if payload["hook_event_name"] == "PostToolUse"
        else HookEventType.PRE_TOOL_USE
    )
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={event_type.value: payload},
    )


def deliver(
    canary: CanaryProvision,
    runtime: Path,
    event: HookEvent,
    hook: str,
    answers: list[dict[str, Any]],
) -> Delivery:
    """Run one hook process's dispatch of *event* and time it; answer *hook*'s result."""
    runner = HookRunner()
    register_runtime_capture_hooks(
        runner,
        daemon_client_factory=lambda: _RecordingClient(runtime, answers),
        repo_root=canary.root,
    )
    started = time.perf_counter()
    results = runner.run_event(event)
    seconds = time.perf_counter() - started
    (result,) = [item for item in results if item.name == hook]
    return Delivery(result, runner.failures, seconds)


def deliver_together(
    canary: CanaryProvision, runtime: Path, payloads: list[dict[str, Any]]
) -> list[Delivery]:
    """Deliver subagent *payloads* from one thread each, released at the same instant."""
    gate = threading.Barrier(len(payloads))

    def one(payload: dict[str, Any]) -> Delivery:
        gate.wait()
        return deliver(canary, runtime, routed(payload), SUBAGENT_HOOK, [])

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(one, payloads))


def tool_payload(tmp_path: Path, number: int, *, after: bool) -> dict[str, Any]:
    """Return the PreToolUse, or with *after* the PostToolUse, of the *number*-th tool call."""
    payload: dict[str, Any] = {
        "session_id": TOOL_SESSION,
        "transcript_path": str(tmp_path / "projects" / f"{TOOL_SESSION}.jsonl"),
        "cwd": str(tmp_path / "repo"),
        "tool_name": "Bash",
        "tool_input": {"command": f"pytest -q tests/test_case_{number}.py"},
        "tool_use_id": f"toolu_load{number:08d}",
        "hook_event_name": "PostToolUse" if after else "PreToolUse",
    }
    if after:
        payload["tool_response"] = {"stdout": f"{number} passed", "stderr": "", "exit_code": 0}
    return payload


@dataclass
class ToolTraffic:
    """The other session's tool calls, requested and resulted back to back until stopped."""

    canary: CanaryProvision
    runtime: Path
    tmp_path: Path
    answers: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    calls: int = 0

    def pair(self) -> None:
        """Send one tool call's request and result."""
        for after in (False, True):
            event = tool_event(tool_payload(self.tmp_path, self.calls, after=after))
            delivery = deliver(self.canary, self.runtime, event, HOST_TOOL_HOOK, self.answers)
            self.deliveries.append(delivery)
        self.calls += 1

    def run(self) -> None:
        """Send pairs until :attr:`stop` is set."""
        while not self.stop.is_set():
            self.pair()


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary with a planned Task per subagent and the other session's live Run."""
    provisioned = provision(tmp_path / "repo", code="FANOUT")
    tasks = {key: rekeyed(seed_row("task", "PLANNED"), key=key) for key in FANOUT.values()}
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.TASK.value: tasks,
            Epoch2Collection.RUN.value: {RUN_KEY: _running_run(TOOL_SESSION)},
        },
    )
    return provisioned


@pytest.fixture
def traffic(canary: CanaryProvision, tmp_path: Path) -> ToolTraffic:
    """The other session's tool traffic, on a run ledger filled to the live one's size.

    One real tool call gives the event lines the live ledger holds most of; the rest
    of the ledger is copies of them, a requested cancel and a compacted record for
    each of a thousand other Runs.
    """
    traffic = ToolTraffic(canary, tmp_path / "runtime", tmp_path)
    traffic.pair()
    ledger = ledger_path(document_path(canary), Epoch2Collection.RUN)
    held = ledger.read_text(encoding="utf-8").splitlines()
    events = [line for line in held if '"payload_kind":"run_event"' in line]
    assert len(events) == 3, held
    control, record = control_line(RUN_URN), run_record_line(RUN_KEY)
    lines: list[str] = []
    for number in range(OTHER_RUNS):
        lines.append(control.replace(RUN_KEY, other_key(number)))
        lines.append(record.replace(RUN_KEY, other_key(number)))
    remaining = LEDGER_LINES - len(held) - len(lines)
    lines.extend(
        events[n % len(events)].replace(RUN_KEY, other_key(n % OTHER_RUNS))
        for n in range(remaining)
    )
    with ledger.open("ab") as handle:
        handle.write("".join(f"{line}\n" for line in lines).encode("utf-8"))
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == LEDGER_LINES
    traffic.answers.clear()
    traffic.deliveries.clear()
    return traffic


# ---- CR-799: five subagents adopt within the deadline on a live-sized ledger --------


@_skip_perf
def test_five_subagents_adopt_within_the_deadline_under_load(
    canary: CanaryProvision,
    traffic: ToolTraffic,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Five starts then five marked stops at once, beside another Run's tool calls."""
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    runtime = tmp_path / "runtime"
    began = time.perf_counter()
    sender = threading.Thread(target=traffic.run, daemon=True)
    sender.start()
    try:
        starts = deliver_together(canary, runtime, [start_payload(tmp_path, a) for a in FANOUT])
        for agent_id, task_key in FANOUT.items():
            write_prompt(subagent_transcript(tmp_path, agent_id), agent_id, task_key)
        stops = deliver_together(canary, runtime, [stop_payload(tmp_path, a) for a in FANOUT])
    finally:
        traffic.stop.set()
        sender.join(timeout=TEST_BUDGET_SECONDS)
    elapsed = time.perf_counter() - began

    for delivery in [*starts, *stops]:
        assert delivery.result.output.startswith("runtime.host_subagent ok run=eawf://"), (
            delivery.result.output
        )
        assert delivery.failures == []
        assert delivery.seconds < ADOPTION_RETRY_DEADLINE_SECONDS
    stopped = {
        agent_id: delivery.result.output.split(" ok run=", 1)[1].split(";", 1)[0]
        for agent_id, delivery in zip(FANOUT, stops, strict=True)
    }
    for agent_id, delivery in zip(FANOUT, starts, strict=True):
        assert delivery.result.output.split(" ok run=", 1)[1] == stopped[agent_id]

    runs = {str(run.urn): run for run in latest_runs(canary).values()}
    adopted = {ref: run for ref, run in runs.items() if ref in stopped.values()}
    assert len(adopted) == 5
    others = {ref: run for ref, run in runs.items() if ref not in adopted and ref != RUN_URN}
    assert len(others) == OTHER_RUNS
    assert {run.status for run in [*adopted.values(), *others.values()]} == {RunStatus.COMPLETED}
    for agent_id, task_key in FANOUT.items():
        run = adopted[stopped[agent_id]]
        assert isinstance(run.scope, TaskScope)
        assert str(run.scope.task_ref) == f"{TASK_CONTAINER}/{task_key}"
        assert run.scope.purpose is RunPurpose.OBSERVE

    assert traffic.deliveries
    for delivery in traffic.deliveries:
        assert delivery.result.output.startswith(f"{HOST_TOOL_HOOK} ok run={RUN_URN}"), (
            delivery.result.output
        )
        assert delivery.failures == []
    assert len(traffic.answers) == len(traffic.deliveries)
    results = traffic.answers[1::2]
    assert all(answer["run_ref"] == RUN_URN and answer["call_id"] for answer in results), results

    with capsys.disabled():
        slowest = max(delivery.seconds for delivery in [*starts, *stops])
        tool_slowest = max(delivery.seconds for delivery in traffic.deliveries)
        print(
            f"\nadoptions: {elapsed:.2f} s; slowest delivery {slowest:.2f} s; "
            f"starts {[round(d.seconds, 2) for d in starts]}; "
            f"stops {[round(d.seconds, 2) for d in stops]}; "
            f"tool deliveries {len(traffic.deliveries)}, slowest {tool_slowest:.2f} s"
        )
