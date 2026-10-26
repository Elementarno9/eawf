"""Proving a landed Batch is fast and seen live.

Receipts are reused, criteria run at once in one checkout per head, requests answer
while a proof runs, a crash is filed rather than rerun, and every change in what holds
the Batch is announced on the daemon's bus. The gate runner is replaced by a counting
stand-in where a test measures how often, where or how long criteria run, so the
timings measure the proof's own scheduling rather than a child interpreter's start-up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.enums import StoreKind
from eawf.runtime.daemon import batch_proof, methods
from eawf.runtime.daemon.batch_close import close_landed_batch
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.gate_execution import GateExecutionContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, delivery_proof
from eawf.runtime.daemon.methods.delivery_close import DELIVERY_CLOSE_LANDED_METHOD
from eawf.runtime.daemon.server import _projection_frames
from eawf.runtime.verification.receipts import VerificationLeg
from eawf.workflow.audit_dsl.models import CheckResult
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    method_context,
)
from tests.integration.runtime.daemon.test_batch_close import ACTOR, batch, close, git
from tests.integration.runtime.daemon.test_batch_close_proof import (  # noqa: F401  (fixture)
    FAILING,
    PASSING,
    make_tree,
    proved_task,
    pytest_of,
    receipts,
    settle,
    uv_shim,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("uv_shim")]

#: How many criteria the concurrency test proves, each taking one second.
CRITERIA: Final = 4
# Long enough for a loaded host to start every criterion, short enough to fail fast.
BARRIER_TIMEOUT_SECONDS: Final = 30.0


class Runner:
    """A stand-in for the gate runner that counts its calls and can take its time."""

    def __init__(
        self,
        *,
        seconds: float = 0.0,
        real: Callable[..., CheckResult] | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.seconds = seconds
        self.real = real
        self.barrier = barrier
        self.calls = 0
        self.cwds: list[Path] = []
        self._lock = threading.Lock()

    def __call__(
        self, leg: VerificationLeg, *, cwd: Path, gate_context: GateExecutionContext
    ) -> CheckResult:
        with self._lock:
            self.calls += 1
            self.cwds.append(cwd)
        if self.real is not None:
            return self.real(leg, cwd=cwd, gate_context=gate_context)
        started = datetime.now(UTC)
        if self.barrier is not None:
            # Every stand-in waits for all the others, so a proof that runs its
            # criteria one after another breaks the barrier instead of passing.
            self.barrier.wait()
        time.sleep(self.seconds)
        return CheckResult(
            name=leg.contract.gate_id,
            kind="command_exit_zero",
            passed=True,
            status="pass",
            exit_status=0,
            started_at=started,
            ended_at=datetime.now(UTC),
        )


@pytest.fixture
def provers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let one proof run :data:`CRITERIA` criteria at once whatever the host's cores."""
    monkeypatch.setattr(batch_proof, "PROVERS", CRITERIA)
    yield


def test_a_fresh_receipt_is_reused_rather_than_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(real=delivery_proof.run_leg)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING, FAILING)},
    )

    kept = settle(canary, tmp_path)
    again = [close(canary, tmp_path) for _ in range(3)]

    assert kept.proof == "unmet"
    assert [closure.reason for closure in again] == [kept.reason] * 3
    assert runner.calls == 2
    assert len(receipts(canary)) == 2


def test_the_operators_ask_runs_only_the_failed_criterion_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(real=delivery_proof.run_leg)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING, FAILING)},
    )
    settle(canary, tmp_path)

    answer = close_landed(tmp_path, canary.root, rerun_failed=True)
    kept = settle(canary, tmp_path)

    (closure,) = answer["closures"]
    assert closure["proof"] == "proving"
    assert closure["reason"].startswith("proving 1 criteria at ")
    assert kept.proof == "unmet"
    assert runner.calls == 3


def test_a_plain_ask_answers_the_settled_failure_without_proving_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(real=delivery_proof.run_leg)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING, FAILING)},
    )
    kept = settle(canary, tmp_path)

    answers = [close_landed(tmp_path, canary.root) for _ in range(2)]

    for answer in answers:
        (closure,) = answer["closures"]
        assert closure["proof"] == "unmet"
        assert closure["reason"] == kept.reason
    assert " unmet at " in kept.reason
    assert runner.calls == 2


def test_criteria_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provers: None
) -> None:
    barrier = threading.Barrier(CRITERIA, timeout=BARRIER_TIMEOUT_SECONDS)
    runner = Runner(barrier=barrier)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", *[PASSING] * CRITERIA)},
    )

    closure = settle(canary, tmp_path)

    assert closure.closed, closure.reason
    assert runner.calls == CRITERIA
    assert not barrier.broken


def close_landed(tmp_path: Path, repo_root: Path, *, rerun_failed: bool = False) -> dict[str, Any]:
    """Send ``runtime.delivery.close_landed`` and return its answer."""
    call = {"repo_root": str(repo_root), "actor": ACTOR, "rerun_failed": rerun_failed}
    return asyncio.run(
        methods.dispatch(DELIVERY_CLOSE_LANDED_METHOD, method_context(tmp_path / "rt"), call)
    )


def test_a_request_during_a_proof_answers_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(seconds=5.0)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING)},
    )
    head = git(repo, "rev-parse", "HEAD")

    timings: list[float] = []
    answers: list[dict[str, Any]] = []
    for _ in range(2):
        started = time.monotonic()
        answers.append(close_landed(tmp_path, canary.root))
        timings.append(time.monotonic() - started)

    reasons = [answer["closures"][0]["reason"] for answer in answers]
    assert reasons == [f"proving 1 criteria at {head[:12]}"] * 2
    assert max(timings) < 2.0, timings
    assert runner.calls == 1


def crash(leg: VerificationLeg, *, cwd: Path, gate_context: GateExecutionContext) -> CheckResult:
    """Stand in for a gate whose child died before it produced a result."""
    raise DaemonValidationError("validation_failed: proof_gate_crashed: the child died")


def test_a_crashed_gate_files_a_failing_receipt_and_the_sweep_does_not_rerun_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(real=crash)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING)},
    )

    kept = settle(canary, tmp_path)
    again = close(canary, tmp_path)

    assert kept.proof == "crashed"
    assert kept.reason == f"CR-0042-1 crashed at {git(repo, 'rev-parse', 'HEAD')[:12]}"
    assert again.reason == kept.reason
    (filed,) = receipts(canary)
    assert filed["receipt"]["result"] == "fail"
    assert filed["receipt"]["exit_status"] is None
    assert runner.calls == 1
    close_landed(tmp_path, canary.root, rerun_failed=True)
    assert settle(canary, tmp_path).proof == "crashed"
    assert runner.calls == 2


def test_one_checkout_serves_a_head_and_goes_when_the_head_moves_or_the_batch_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(real=delivery_proof.run_leg)
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", pytest_of("later"), PASSING)},
    )

    assert settle(canary, tmp_path).proof == "unmet"
    close_landed(tmp_path, canary.root, rerun_failed=True)
    assert settle(canary, tmp_path).proof == "unmet"
    first = set(runner.cwds)
    (repo / "src" / "later.py").write_text("# later\n", encoding="utf-8")
    git(repo, "add", "src/later.py")
    git(repo, "commit", "-q", "-m", "feat: later")
    close_landed(tmp_path, canary.root)
    closure = settle(canary, tmp_path)

    assert closure.closed, closure.reason
    (old,) = first
    (new,) = set(runner.cwds) - first
    assert runner.calls == 2 + 1 + 2
    assert not old.exists()
    assert not new.exists()
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_proving_and_its_outcome_are_announced_as_they_change(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING, FAILING)},
    )
    bus = EventBus()
    heard = bus.register(connection_id="console")
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "rt" / "wal",
        bus=bus,
    )
    context = ctx.native_root_context(canary.root / ".ea")
    urn = parse_qualified_urn(BATCH_URN)
    short = git(repo, "rev-parse", "HEAD")[:12]

    closure, _ = close_landed_batch(context, urn, actor=ACTOR, now=AT)
    while closure.proof == "proving":
        time.sleep(0.05)
        closure, _ = close_landed_batch(context, urn, actor=ACTOR, now=AT)

    notices = [item for item in heard.queue if item.kind is StoreKind.BATCH_PROOF_NOTED]
    assert [item.payload["note"] for item in notices] == [
        f"proving 2 criteria at {short}",
        f"CR-0042-2 unmet at {short}",
    ]
    assert {item.payload["batch_ref"] for item in notices} == {BATCH_URN}
    (frame,) = _projection_frames(notices[0])
    assert b'"method":"event.push"' in frame
    assert b"batch_proof_noted" in frame
