"""The native dispatch meters a capped Run on every usage reading it relays.

The in-flight cap verb was built and tested, but nothing on the dispatch
path ever fed it a reading, so a sealed token ceiling bounded nothing in
flight. These cases drive the real ``dispatch_run`` with a launcher that
stands in for a provider stream: it starts a real child in its own process
group -- so the kill ladder reaps something that exists -- and relays a
scripted run of cumulative usage readings through the request's sink. No
model is called.

A reaper thread waits on the child the way a real launcher awaits its
process, so the ladder observes the group die as soon as it exits rather
than finding a zombie and sitting out the grace window.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, effective_records
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import (
    DispatchParams,
    DispatchRefusal,
    DispatchStage,
    compile_launchers,
)
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
    compose_worker_hello,
)
from eawf.runtime.runtimes.metering import UsageSample
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.test_native_dispatch import (
    PROVIDER_KIND,
    RUN_KEY,
    capsule_request,
    dispatch_params,
    ledger_records,
    make_canary,
    method_ctx,
)

pytestmark = pytest.mark.integration

#: The sealed ceiling every capped case is measured against.
CAP: Final = 1_000

#: The input tokens each reading carries; the output tally alone moves a
#: reading across the cap, so each case's arithmetic is one addition.
BASE_INPUT: Final = 100

#: A child that runs until its stdin closes, which is how the launcher ends
#: a turn that completed, or until a signal reaps its group.
_CHILD_SOURCE: Final = "import sys\nsys.stdin.readline()\n"


def readings(*output_totals: int) -> tuple[UsageSample, ...]:
    """Return cumulative readings whose output tallies are *output_totals*."""
    return tuple(
        UsageSample(input_tokens=BASE_INPUT, output_tokens=total) for total in output_totals
    )


class StreamingLauncher:
    """A launcher that relays a scripted usage stream from a live child.

    It keeps relaying after the sink reports a termination, because a real
    stream flushes buffered readings after the reap; the meter must answer
    those without opening a second control.

    Attributes:
        provider_kind: Which provider this launcher stands in for.
        answers: What the sink answered for each relayed reading.
        sink_present: Whether the request carried a usage sink at all.
        cap_tokens: The token ceiling the sealed capsule carried.
        child_alive_after_stream: Whether the child survived the stream.
    """

    def __init__(self, samples: tuple[UsageSample, ...]) -> None:
        self.provider_kind = PROVIDER_KIND
        self._samples = samples
        self.answers: list[bool] = []
        self.sink_present = False
        self.cap_tokens: int | None = None
        self.child_alive_after_stream: bool | None = None

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Start the child, relay the stream, and answer as the child ended.

        Raises:
            RuntimeSpawnError: The child was reaped mid-turn, which is how a
                real launcher sees a killed process.
        """
        self.sink_present = request.usage_sink is not None
        self.cap_tokens = request.capsule.budget.tokens
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD_SOURCE],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        reaper = threading.Thread(target=process.wait, daemon=True)
        reaper.start()
        try:
            for sample in self._samples:
                if request.usage_sink is not None:
                    self.answers.append(await request.usage_sink(sample, process.pid))
            self.child_alive_after_stream = process.poll() is None
        finally:
            if process.stdin is not None:
                process.stdin.close()
            await asyncio.to_thread(reaper.join, 30.0)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30.0)
        if not self.child_alive_after_stream:
            raise RuntimeSpawnError("the child was reaped mid-turn", exit_status=process.returncode)
        session_ref = f"codex-session-{request.hello_sequence}"
        return NativeLaunchOutcome(
            provider_session_ref=session_ref,
            subprocess_pid=process.pid,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session_ref,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
        )


def dispatch(
    tmp_path: Path, launcher: StreamingLauncher, *, token_budget: int | None = CAP
) -> tuple[CanaryProvision, dict[str, Any]]:
    """Dispatch the seeded Run with *launcher* and a sealed *token_budget*."""
    canary = make_canary(tmp_path / "repo")
    capsule = capsule_request()
    if token_budget is not None:
        capsule["token_budget"] = token_budget
    supplied = dispatch_params(canary, capsule=capsule)
    args = DispatchParams.model_validate(
        {key: value for key, value in supplied.items() if key != "repo_root"}
    )
    context = method_ctx(tmp_path / "runtime").native_root_context(canary.root / ".ea")
    answer = asyncio.run(
        native_dispatch.dispatch_run(
            context,
            args,
            now=datetime.now(UTC),
            launchers=dict(compile_launchers((launcher,))),
        )
    )
    return canary, answer.model_dump(mode="json")


def dispatch_refused(
    tmp_path: Path, launcher: StreamingLauncher
) -> tuple[CanaryProvision, DaemonValidationError]:
    """Dispatch expecting a refusal and return the canary it left behind."""
    canary = make_canary(tmp_path / "repo")
    capsule = capsule_request(token_budget=CAP)
    supplied = dispatch_params(canary, capsule=capsule)
    args = DispatchParams.model_validate(
        {key: value for key, value in supplied.items() if key != "repo_root"}
    )
    context = method_ctx(tmp_path / "runtime").native_root_context(canary.root / ".ea")
    with pytest.raises(DaemonValidationError) as caught:
        asyncio.run(
            native_dispatch.dispatch_run(
                context,
                args,
                now=datetime.now(UTC),
                launchers=dict(compile_launchers((launcher,))),
            )
        )
    return canary, caught.value


def kinds(records: tuple[LedgerRecord, ...], kind: str) -> list[dict[str, Any]]:
    """Return every ledger payload of *kind*, in ledger order."""
    return [item.payload for item in records if item.payload.get("payload_kind") == kind]


def run_status(canary: CanaryProvision, tmp_path: Path) -> str:
    """Return the Run's status from the document or its compacted ledger line."""
    rows = read_document(document_path(canary)).get("run", {})
    if RUN_KEY in rows:
        return str(rows[RUN_KEY]["status"])
    for item in effective_records(ledger_records(canary, tmp_path / "runtime")):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return str(item.payload["status"])
    raise AssertionError(f"neither tier holds a run record keyed {RUN_KEY!r}")


# ---- under the cap ----------------------------------------------------------


def test_dispatch_run_under_cap_meters_every_reading_and_continues(tmp_path: Path) -> None:
    """Readings that stay one token under the cap write nothing and stop nothing."""
    launcher = StreamingLauncher(readings(100, 400, CAP - BASE_INPUT - 1))

    canary, answer = dispatch(tmp_path, launcher)

    assert launcher.sink_present
    assert launcher.cap_tokens == CAP
    assert launcher.answers == [False, False, False]
    assert launcher.child_alive_after_stream is True
    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    records = ledger_records(canary, tmp_path / "runtime")
    assert kinds(records, "budget_notice") == []
    assert kinds(records, "control") == []


def test_dispatch_run_without_cap_attaches_no_meter(tmp_path: Path) -> None:
    """An uncapped Run carries no sink, however much its stream reports."""
    launcher = StreamingLauncher(readings(CAP * 10))

    canary, answer = dispatch(tmp_path, launcher, token_budget=None)

    assert not launcher.sink_present
    assert launcher.cap_tokens is None
    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    assert kinds(ledger_records(canary, tmp_path / "runtime"), "budget_notice") == []


# ---- crossing the cap -------------------------------------------------------


def test_dispatch_run_crossing_cap_terminates_once_with_one_notice(tmp_path: Path) -> None:
    """The first crossing reaps the child; later readings open nothing more."""
    over = CAP - BASE_INPUT + 50
    launcher = StreamingLauncher(readings(200, over, over + 500, over + 900))

    canary, error = dispatch_refused(tmp_path, launcher)

    assert DispatchRefusal.BUDGET_EXHAUSTED.value in str(error)
    assert launcher.answers == [False, True, True, True]
    assert launcher.child_alive_after_stream is False
    records = ledger_records(canary, tmp_path / "runtime")
    notices = kinds(records, "budget_notice")
    assert len(notices) == 1
    assert notices[0]["cap_tokens"] == CAP
    assert notices[0]["observed_tokens"] == CAP + 50
    phases = [fact["phase"] for fact in kinds(records, "control")]
    assert phases == ["requested", "acknowledged", "effected"]
    assert run_status(canary, tmp_path) == "CANCELLED"


def test_dispatch_run_reading_exactly_at_cap_terminates(tmp_path: Path) -> None:
    """A reading that meets the cap is a crossing, not a reading under it."""
    launcher = StreamingLauncher(readings(CAP - BASE_INPUT))

    canary, error = dispatch_refused(tmp_path, launcher)

    assert DispatchRefusal.BUDGET_EXHAUSTED.value in str(error)
    assert launcher.answers == [True]
    assert len(kinds(ledger_records(canary, tmp_path / "runtime"), "budget_notice")) == 1


# ---- the request grammar ----------------------------------------------------


def test_dispatch_params_refuse_a_zero_token_budget(tmp_path: Path) -> None:
    """A ceiling of zero tokens is no ceiling a Run could run under."""
    canary = make_canary(tmp_path / "repo")
    supplied = dispatch_params(canary, capsule=capsule_request(token_budget=0))

    with pytest.raises(ValueError, match="token_budget"):
        DispatchParams.model_validate(
            {key: value for key, value in supplied.items() if key != "repo_root"}
        )
