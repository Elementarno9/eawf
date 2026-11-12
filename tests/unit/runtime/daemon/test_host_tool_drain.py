"""Queued host tool observations land before the daemon exits, and land once.

An observation is answered once it is queued, so the daemon owes it a landing
past its answer: a stopping daemon answers only once the call is on disk, the
drainer ends only once the queue is empty, and the daemon's loop awaits the
drainer before it exits. A batch locked out by another holder of the document
lock stays queued until it lands, except past a bounded grace once the daemon
stops. A retried call queued twice lands once, and one refused call does not
cost the others in its batch their landing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.store.ledger import read_collection_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import host_calls, methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootFencedError
from eawf.runtime.daemon.host_calls import HostCallObservation, HostCallQueue, host_call_id
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.host_tool import HOST_TOOL_OBSERVE_METHOD
from eawf.runtime.daemon.tool_events import ToolPhase, tool_event_ref
from eawf.runtime.lock.portalock import LockTimeout
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    root_context,
    tree_root,
)
from tests.integration.runtime.daemon.test_host_context_boundary import canary, ctx
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    RUN_URN,
    SESSION,
)

__all__ = ["canary", "ctx"]

_NOW: Final = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
_RESULT_PHASES: Final[tuple[ToolPhase, ...]] = ("requested", "accepted", "result")


def _call_id(key: str) -> str:
    return host_call_id(parse_qualified_urn(RUN_URN), key)


def _receipts(provisioned: CanaryProvision) -> dict[str, list[str]]:
    """Return each call's receipt ids, in ledger order."""
    held: dict[str, list[str]] = {}
    for item in read_collection_records(document_path(provisioned), Epoch2Collection.RECEIPT):
        if item.payload.get("payload_kind") == "host_call_receipt":
            held.setdefault(item.payload["call_id"], []).append(item.payload["receipt_id"])
    return held


def _event_refs(provisioned: CanaryProvision) -> list[str]:
    """Return the key of every line on the run ledger, in ledger order."""
    path = ledger_path(document_path(provisioned), Epoch2Collection.RUN)
    return [item.record_key for item in read_ledger_records(path)]


def _landed(provisioned: CanaryProvision, key: str) -> bool:
    """Return whether the call *key* names has its receipt and every phase on disk."""
    call_id = _call_id(key)
    keys = "\n".join(_event_refs(provisioned))
    phases = all(tool_event_ref(call_id, phase) in keys for phase in _RESULT_PHASES)
    return phases and len(_receipts(provisioned).get(call_id, [])) == 1


async def _observe(ctx: MethodContext, provisioned: CanaryProvision, key: str) -> Any:
    return await methods.dispatch(
        HOST_TOOL_OBSERVE_METHOD,
        ctx,
        {
            "repo_root": str(provisioned.root),
            "harness": "claude-code",
            "host_session_id": SESSION,
            "tool_name": "Bash",
            "host_call_key": key,
            "phase": "result",
            "output": f"{key} passed",
        },
    )


@contextlib.contextmanager
def _lock_held(provisioned: CanaryProvision, runtime: Path) -> Iterator[threading.Event]:
    """Hold the tree's document lock on another thread until the yielded event is set."""
    holder = root_context(provisioned, runtime)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with holder.session([RUN_URN]):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=hold, name="lock-holder")
    thread.start()
    try:
        assert held.wait(10), "the holder never took the document lock"
        yield release
    finally:
        release.set()
        thread.join(30)


# ---- a clean shutdown lands every answered call -------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 6])
def test_calls_answered_before_the_loop_ends_have_all_landed_once_it_has(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path, count: int
) -> None:
    keys = [f"toolu_drain{n:02d}" for n in range(count)]

    async def answer_all(release: threading.Event) -> list[Any]:
        answers = await asyncio.gather(*(_observe(ctx, canary, key) for key in keys))
        # Every call is answered and none has landed: the lock is still held.
        assert not any(_landed(canary, key) for key in keys)
        release.set()
        return answers

    with _lock_held(canary, tmp_path / "holder") as release:
        answers = asyncio.run(answer_all(release))

    assert [answer["call_id"] for answer in answers] == [_call_id(key) for key in keys]
    assert all(_landed(canary, key) for key in keys)


def test_a_stopping_daemon_answers_only_once_the_call_has_landed(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    early = [f"toolu_early{n}" for n in range(3)]

    async def stop_with_calls_queued(release: threading.Event) -> Any:
        await asyncio.gather(*(_observe(ctx, canary, key) for key in early))
        ctx.stopping = True
        late = asyncio.create_task(_observe(ctx, canary, "toolu_late"))
        await asyncio.sleep(0.3)
        held_back = not late.done()
        release.set()
        answer = await late
        # The late call's batch took every call queued before it.
        return held_back, answer, all(_landed(canary, key) for key in [*early, "toolu_late"])

    with _lock_held(canary, tmp_path / "holder") as release:
        held_back, answer, landed = asyncio.run(stop_with_calls_queued(release))

    assert held_back, "a stopping daemon answered before the call landed"
    assert answer == {"run_ref": RUN_URN, "call_id": _call_id("toolu_late")}
    assert landed


def test_a_stopping_daemon_reports_a_call_that_could_not_land(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    ctx.stopping = True
    ctx.native_root_context(tree_root(canary)).fence()

    with pytest.raises(RootFencedError):
        asyncio.run(_observe(ctx, canary, "toolu_fenced"))

    assert _call_id("toolu_fenced") not in _receipts(canary)


# ---- the queue: one drainer, empty when it ends ------------------------------------------------


class _Deferred:
    """Holds the drainer a queue asks to start, so a test runs it when it chooses."""

    def __init__(self) -> None:
        self.drains: list[Callable[[], None]] = []

    def __call__(self, drain: Callable[[], None]) -> None:
        self.drains.append(drain)


def _observation(key: str, *, output: str | None = "ok") -> HostCallObservation:
    return HostCallObservation(
        run_ref=parse_qualified_urn(RUN_URN),
        tool_name="Bash",
        host_call_key=key,
        output=output,
        failed=False,
        actor=HARNESS_ACTORS["claude-code"],
        now=_NOW,
    )


def _context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    return root_context(canary, tmp_path / "runtime")


def _never() -> bool:
    return False


def _always() -> bool:
    return True


def test_one_drainer_runs_at_a_time_and_ends_with_the_queue_empty(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    landings = [queue.put(_observation(f"toolu_q{n}"), start=deferred) for n in range(3)]
    assert len(deferred.drains) == 1
    assert not any(landing.done() for landing in landings)

    deferred.drains[0]()

    assert [landing.done() for landing in landings] == [True, True, True]
    assert all(_landed(canary, f"toolu_q{n}") for n in range(3))
    # The drainer ended with the queue empty, so the next call starts another.
    queue.put(_observation("toolu_after"), start=deferred)
    assert len(deferred.drains) == 2


def test_an_empty_drain_returns_at_once(canary: CanaryProvision, tmp_path: Path) -> None:
    before = _event_refs(canary)

    HostCallQueue(_context(canary, tmp_path), stopping=_never).drain()

    assert _event_refs(canary) == before


def test_a_requested_call_lands_with_no_receipt(canary: CanaryProvision, tmp_path: Path) -> None:
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    landing = queue.put(_observation("toolu_asked", output=None), start=deferred)

    deferred.drains[0]()

    assert landing.result(timeout=0) is None
    assert tool_event_ref(_call_id("toolu_asked"), "requested") in _event_refs(canary)
    assert _call_id("toolu_asked") not in _receipts(canary)


def test_a_retried_call_queued_twice_lands_once(canary: CanaryProvision, tmp_path: Path) -> None:
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    first = queue.put(_observation("toolu_twice"), start=deferred)
    again = queue.put(_observation("toolu_twice"), start=deferred)

    deferred.drains[0]()

    receipt, repeated = first.result(timeout=0), again.result(timeout=0)
    assert receipt is not None and repeated is not None
    assert repeated.receipt_id == receipt.receipt_id
    assert _receipts(canary)[_call_id("toolu_twice")] == [receipt.receipt_id]
    refs = _event_refs(canary)
    for phase in _RESULT_PHASES:
        assert refs.count(tool_event_ref(_call_id("toolu_twice"), phase)) == 1


def test_one_refused_call_does_not_cost_its_batch(canary: CanaryProvision, tmp_path: Path) -> None:
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    # A Run on no ledger refuses its tool phases; the call beside it still lands.
    stray = HostCallObservation(
        run_ref=parse_qualified_urn(RUN_URN.replace(RUN_KEY, "RUN-00000077")),
        tool_name="Bash",
        host_call_key="toolu_stray",
        output="ok",
        failed=False,
        actor=HARNESS_ACTORS["claude-code"],
        now=_NOW,
    )
    refused = queue.put(stray, start=deferred)
    landed = queue.put(_observation("toolu_kept"), start=deferred)

    deferred.drains[0]()

    assert refused.exception(timeout=0) is not None
    assert landed.result(timeout=0) is not None
    assert _landed(canary, "toolu_kept")


def test_a_batch_that_cannot_open_its_session_fails_its_calls_and_ends(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    context = _context(canary, tmp_path)
    queue, deferred = HostCallQueue(context, stopping=_never), _Deferred()
    landings = [queue.put(_observation(f"toolu_f{n}"), start=deferred) for n in range(2)]
    context.fence()

    deferred.drains[0]()

    assert all(isinstance(item.exception(timeout=0), RootFencedError) for item in landings)
    queue.put(_observation("toolu_next"), start=deferred)
    assert len(deferred.drains) == 2


def test_an_observation_queued_while_a_batch_lands_is_landed_by_the_same_drainer(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    original = host_calls._outcome
    late: list[Any] = []

    def arriving_meanwhile(session: Any, observation: HostCallObservation) -> Any:
        if not late:
            late.append(queue.put(_observation("toolu_meanwhile"), start=deferred))
        return original(session, observation)

    monkeypatch.setattr(host_calls, "_outcome", arriving_meanwhile)
    first = queue.put(_observation("toolu_landing"), start=deferred)

    deferred.drains[0]()

    assert len(deferred.drains) == 1, "a drainer was running, so none more may start"
    assert first.result(timeout=0) is not None
    assert late[0].result(timeout=0) is not None
    assert _landed(canary, "toolu_meanwhile")


# ---- a document lock held by another process ---------------------------------------------------

#: The lock wait the tests patch in, so a held lock times a try out quickly.
_LOCK_WAIT: Final = 0.2


def _event_counts(provisioned: CanaryProvision, key: str) -> list[int]:
    refs = _event_refs(provisioned)
    return [refs.count(tool_event_ref(_call_id(key), phase)) for phase in _RESULT_PHASES]


def test_a_batch_locked_out_past_the_lock_timeout_stays_queued_and_lands_once(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EA_LOCK_TIMEOUT", str(_LOCK_WAIT))
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_never), _Deferred()
    keys = [f"toolu_held{n}" for n in range(5)]
    caplog.set_level(logging.WARNING, logger=host_calls.__name__)
    with _lock_held(canary, tmp_path / "holder") as release:
        landings = [queue.put(_observation(key), start=deferred) for key in keys[:3]]
        drainer = threading.Thread(target=deferred.drains[0], name="drainer")
        drainer.start()
        time.sleep(_LOCK_WAIT * 3)
        landings += [queue.put(_observation(key), start=deferred) for key in keys[3:]]
        assert not any(landing.done() for landing in landings)
        release.set()
    drainer.join(10)

    assert not drainer.is_alive()
    assert len(deferred.drains) == 1
    assert all(landing.result(timeout=0) is not None for landing in landings)
    assert all(_landed(canary, key) for key in keys)
    assert all(_event_counts(canary, key) == [1, 1, 1] for key in keys)
    assert "lock busy" in caplog.text


#: The grace the stop tests patch in, in tries: the step clock moves one second
#: per locked-out try, so the drainer gives up after exactly this many.
_GRACE_TRIES: Final = 3


class _StepClock:
    """The drainer's clock, moved one second forward by each landing try.

    A held lock makes every try wait out the lock timeout, so a try is the unit
    the grace is spent in; counting tries proves where the drainer gave up
    without timing the machine.

    Attributes:
        now: The seconds the clock reads.
        tries: The landing tries made so far.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.tries = 0

    def monotonic(self) -> float:
        return self.now


def _step_clock(monkeypatch: pytest.MonkeyPatch) -> _StepClock:
    """Drive the drainer's grace by a clock that moves only when a try ends."""
    clock = _StepClock()
    land = HostCallQueue._land

    def counted_land(queue: HostCallQueue, first: Any) -> None:
        land(queue, first)
        clock.tries += 1
        clock.now += 1.0

    monkeypatch.setattr(host_calls, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(host_calls, "HOST_CALL_STOP_GRACE_SECONDS", float(_GRACE_TRIES))
    monkeypatch.setattr(HostCallQueue, "_land", counted_land)
    return clock


@pytest.mark.parametrize("count", [1, 3])
def test_a_stop_past_the_grace_with_the_lock_held_ends_in_time_and_logs_the_loss(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    count: int,
) -> None:
    monkeypatch.setenv("EA_LOCK_TIMEOUT", str(_LOCK_WAIT))
    clock = _step_clock(monkeypatch)
    queue, deferred = HostCallQueue(_context(canary, tmp_path), stopping=_always), _Deferred()
    keys = [f"toolu_lost{n}" for n in range(count)]
    with _lock_held(canary, tmp_path / "holder"):
        landings = [queue.put(_observation(key), start=deferred) for key in keys]
        deferred.drains[0]()

    assert clock.tries == _GRACE_TRIES
    assert all(isinstance(landing.exception(timeout=0), LockTimeout) for landing in landings)
    assert f"count={count}" in caplog.text
    assert all(_call_id(key) in caplog.text for key in keys)
    assert not any(_call_id(key) in _receipts(canary) for key in keys)


def test_a_stopping_daemon_with_the_lock_held_answers_within_the_grace(
    canary: CanaryProvision,
    ctx: MethodContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EA_LOCK_TIMEOUT", str(_LOCK_WAIT))
    clock = _step_clock(monkeypatch)
    ctx.stopping = True
    with _lock_held(canary, tmp_path / "holder"), pytest.raises(LockTimeout):
        asyncio.run(_observe(ctx, canary, "toolu_stopped"))

    assert clock.tries == _GRACE_TRIES
    assert "count=1" in caplog.text and _call_id("toolu_stopped") in caplog.text
