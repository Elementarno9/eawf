"""A Run over its cap is stopped mid-turn, and the ledger says so first.

The shipped interlock classified a dispatch's burn only once the dispatch
had returned, so the group it signalled had already exited and no Run had
ever been terminated at its cap. Everything below drives the live
``runtime.run.budget.meter`` verb against a real child process that is
still running when the reading crosses.

The ordering is proven by the child, not by a clock. The child installs a
SIGTERM handler that reads the Run's own ledger and writes down what it
found before exiting, so "the ledger recorded the termination before the
child exited" is a fact the dying process observed rather than a race the
test hopes to win. The stream itself is deterministic too: the child
writes a fixed number of lines and then blocks on stdin, and the test
folds one cumulative usage reading per line it reads back, so which
reading the cap is tested against is decided by the fold and never by
timing.

The lease race uses a :class:`threading.Barrier`: an operator cancel and
the budget termination are both provably in flight before either reaches
the root's entity lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
)
from eawf.runtime.daemon.methods.run_budget import RUN_BUDGET_METER_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
BUDGET_REF: Final = "CTL-0000000a"
OPERATOR_REF: Final = "CTL-0000000b"

#: The cap every scenario is measured against, and the input tokens each
#: reading carries. The output tally alone decides which side of the cap a
#: stream lands on, so the arithmetic in each test is one subtraction.
CAP: Final = 1_000
BASE_INPUT: Final = 100

#: How many lines the child writes before it blocks. The test reads exactly
#: this many, so no read can outrun the writer and no sleep is needed.
CHUNKS: Final = 4

#: A child that streams, then reads its own Run ledger as it dies.
#:
#: The SIGTERM handler is the whole point: it runs while the process is
#: alive, so whatever it finds in the ledger was written before the exit.
_CHILD_SOURCE: Final = """
import json
import signal
import sys

ledger_path, result_path, chunks = sys.argv[1], sys.argv[2], int(sys.argv[3])


def observe(signum, _frame):
    kinds = []
    try:
        with open(ledger_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                payload = row.get("payload") or {}
                kind = payload.get("payload_kind")
                if kind == "control":
                    kinds.append(f"control:{payload.get('phase')}")
                elif kind:
                    kinds.append(kind)
    except OSError:
        kinds = ["ledger-unreadable"]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump({"signal": signum, "ledger": kinds}, handle)
    sys.exit(0)


signal.signal(signal.SIGTERM, observe)
for index in range(chunks):
    sys.stdout.write(json.dumps({"chunk": index}) + "\\n")
    sys.stdout.flush()
sys.stdin.readline()
"""


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


class LiveChild:
    """One real child process in its own group, streaming under a meter.

    Attributes:
        process: The running child.
        result: Where the child writes what it saw in the ledger as it died.
    """

    def __init__(self, process: subprocess.Popen[str], result: Path) -> None:
        self.process = process
        self.result = result

    @property
    def pgid(self) -> int:
        """Return the child's process-group id; it leads its own group."""
        return os.getpgid(self.process.pid)

    def alive(self) -> bool:
        """Return whether the child has not exited yet."""
        return self.process.poll() is None

    def read_chunks(self, count: int) -> int:
        """Read *count* streamed lines and return how many arrived.

        Each read blocks until the child's line lands, so the stream is
        driven by the writer rather than by a wait.
        """
        assert self.process.stdout is not None
        return sum(1 for _ in range(count) if self.process.stdout.readline())

    def observed_ledger(self) -> list[str]:
        """Return the ledger line kinds the child saw as it was dying.

        Raises:
            AssertionError: The child exited without writing its
                observation, so it never ran the handler and the ordering
                it was asked about was not observed at all.
        """
        self.process.wait(timeout=30.0)
        assert self.result.exists(), "the child exited without observing its own ledger"
        payload = json.loads(self.result.read_text(encoding="utf-8"))
        kinds: list[str] = payload["ledger"]
        return kinds


@pytest.fixture
def child(tmp_path: Path, canary: CanaryProvision) -> Iterator[LiveChild]:
    """Start the streaming child and reap it however the test ends."""
    result = tmp_path / "child-observation.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_SOURCE,
            str(run_ledger(canary)),
            str(result),
            str(CHUNKS),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    live = LiveChild(process, result)
    try:
        yield live
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30.0)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def run_ledger(canary: CanaryProvision) -> Path:
    """Return the canary's run ledger, which the child reads as it dies."""
    return ledger_path(document_path(canary), Epoch2Collection.RUN)


def usage_stream(*, output_totals: tuple[int, ...]) -> list[dict[str, int]]:
    """Return cumulative readings whose output tallies are *output_totals*.

    Each reading carries a third of its output as reasoning, so a meter
    that added the slice instead of counting it inside would land a third
    higher and cross a cap these streams stay under.
    """
    return [
        {
            "input_tokens": BASE_INPUT,
            "output_tokens": total,
            "reasoning_output_tokens": total // 3,
        }
        for total in output_totals
    ]


def meter(
    ctx: MethodContext,
    canary: CanaryProvision,
    *,
    output_totals: tuple[int, ...],
    pgid: int | None = None,
    base_budget: int | None = CAP,
    enforce: str = "hard",
    ref: str = BUDGET_REF,
    key: str = "idem-budget",
) -> dict[str, Any]:
    """Drive the in-flight cap verb over one turn's readings."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "control_request_ref": ref,
        "actor": ACTOR,
        "samples": usage_stream(output_totals=output_totals),
        "enforce": enforce,
        "multiplier": 1.0,
        "idempotency_key": key,
    }
    if base_budget is not None:
        params["base_budget"] = base_budget
    if pgid is not None:
        params["pgid"] = pgid
    return call(RUN_BUDGET_METER_METHOD, ctx, **params)


def ledger_kinds(canary: CanaryProvision) -> list[str]:
    """Return every payload kind the canary's run ledger holds, in order."""
    kinds: list[str] = []
    for item in read_ledger_records(run_ledger(canary)):
        kind = item.payload.get("payload_kind")
        if kind == "control":
            kinds.append(f"control:{item.payload.get('phase')}")
        elif kind:
            kinds.append(str(kind))
    return kinds


def control_facts(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every control fact the canary's run ledger holds, in order."""
    return [
        item.payload
        for item in read_ledger_records(run_ledger(canary))
        if item.payload.get("payload_kind") == "control"
    ]


def acknowledgement_dispositions(canary: CanaryProvision) -> list[str]:
    """Return the disposition of every acknowledgement the ledger recorded."""
    return [
        str(fact["disposition"])
        for fact in control_facts(canary)
        if fact["phase"] == "acknowledged"
    ]


def notices(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every budget notice the canary's run ledger holds."""
    return [
        item.payload
        for item in read_ledger_records(run_ledger(canary))
        if item.payload.get("payload_kind") == "budget_notice"
    ]


def stored_status(canary: CanaryProvision) -> str:
    """Return the status the canonical Run record carries right now.

    A terminated Run is exactly the case this suite drives, and a Run that
    reaches a terminal state is compacted out of the document and into the
    run ledger, beside the notice and control lines the same ledger
    carries. The Run's own line is the one with no payload discriminator,
    which is how the daemon's own reader tells it apart.

    Raises:
        AssertionError: Neither tier holds the record, so the Run was lost
            by the move rather than relocated by it.
    """
    rows = read_document(document_path(canary)).get("run", {})
    if RUN_KEY in rows:
        return str(rows[RUN_KEY]["status"])
    for item in effective_records(read_ledger_records(run_ledger(canary))):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return str(item.payload["status"])
    raise AssertionError(f"neither tier holds a run record keyed {RUN_KEY!r}")


# ---------------------------------------------------------------------------
# RUN-058 / ECON-052: terminated mid-turn, recorded before the child exits
# ---------------------------------------------------------------------------


def test_a_run_crossing_its_cap_is_terminated_while_its_child_still_runs(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """The child reads its own ledger as it dies and finds the termination there."""
    assert child.read_chunks(CHUNKS) == CHUNKS
    assert child.alive()

    answer = meter(ctx, canary, output_totals=(100, 500, 900), pgid=child.pgid)

    assert answer["terminated"] is True
    assert answer["led"] is True
    observed = child.observed_ledger()
    assert "budget_notice" in observed
    assert "control:requested" in observed
    assert "control:acknowledged" in observed
    assert child.process.poll() is not None
    assert answer["run_status"] == "CANCELLED"
    assert stored_status(canary) == "CANCELLED"


def test_the_confirmed_effect_is_appended_only_after_the_reap_was_observed(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """A confirmed effect names what was seen, so it cannot precede the reap."""
    child.read_chunks(CHUNKS)

    meter(ctx, canary, output_totals=(900,), pgid=child.pgid)

    observed = child.observed_ledger()
    assert "control:effected" not in observed
    assert ledger_kinds(canary) == [
        "budget_notice",
        "control:requested",
        "control:acknowledged",
        "control:effected",
    ]


def test_the_notice_the_termination_recorded_blocks_nothing(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(900,), pgid=child.pgid)

    assert answer["notice"]["blocking"] is False
    assert notices(canary)[0]["blocking"] is False
    assert notices(canary)[0]["observed_tokens"] == 1_000
    assert notices(canary)[0]["cap_tokens"] == CAP


# ---------------------------------------------------------------------------
# The cap boundary: exact on both sides, not approximately right
# ---------------------------------------------------------------------------


def test_a_reading_one_token_under_the_cap_terminates_nothing(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """899 output plus 100 input is 999, and 999 is not over 1000."""
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(400, 899), pgid=child.pgid)

    assert answer["observed_tokens"] == CAP - 1
    assert answer["over_cap"] is False
    assert answer["terminated"] is False
    assert answer["notice"] is None
    assert ledger_kinds(canary) == []
    assert child.alive()
    assert stored_status(canary) == "RUNNING"


def test_a_reading_exactly_at_the_cap_terminates(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """The cap is met, not merely exceeded, so the boundary token counts."""
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(400, 900), pgid=child.pgid)

    assert answer["observed_tokens"] == CAP
    assert answer["over_cap"] is True
    assert answer["terminated"] is True
    assert child.observed_ledger()


def test_reasoning_is_metered_inside_output_so_a_subset_never_crosses_the_cap(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """Adding the reasoning slice would meter 1199 and kill a Run at 899."""
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(899,), pgid=child.pgid)

    assert answer["output_tokens"] == 899
    assert answer["observed_tokens"] == 999
    assert answer["terminated"] is False
    assert child.alive()


def test_soft_enforce_never_reaps_however_far_over_the_cap(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """The default mode warns; only an opt-in hard cap may stop a Run."""
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(9_000,), pgid=child.pgid, enforce="soft")

    assert answer["action"] == "warn"
    assert answer["over_cap"] is True
    assert answer["terminated"] is False
    assert answer["notice"] is None
    assert child.alive()


def test_a_run_with_no_configured_budget_is_never_terminated(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    child.read_chunks(CHUNKS)

    answer = meter(ctx, canary, output_totals=(9_000,), pgid=child.pgid, base_budget=None)

    assert answer["cap_tokens"] is None
    assert answer["over_cap"] is False
    assert answer["terminated"] is False
    assert child.alive()


def test_an_empty_stream_meters_zero_and_terminates_nothing(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """A turn that disclosed nothing is not a turn that spent everything."""
    answer = meter(ctx, canary, output_totals=(), pgid=child.pgid)

    assert answer["observed_tokens"] == 0
    assert answer["terminated"] is False
    assert child.alive()


# ---------------------------------------------------------------------------
# The Run's one control lease still decides who acts
# ---------------------------------------------------------------------------


def test_a_budget_termination_racing_an_operator_cancel_is_superseded(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """Both threads are provably in flight before either takes the lock.

    Two answers to one question: the operator's cancel and the cap both
    stop the Run. The lease grants one of them, and the loser records its
    notice -- which blocks nothing -- and signals nothing, because the
    holder's own control ends the Run.
    """
    child.read_chunks(CHUNKS)
    call(
        RUN_CONTROL_REQUEST_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=OPERATOR_REF,
        control="cancel",
        actor=ACTOR,
    )
    barrier = threading.Barrier(2)
    answers: dict[str, dict[str, Any]] = {}

    def operator_cancel() -> None:
        barrier.wait(timeout=30.0)
        answers["operator"] = call(
            RUN_CONTROL_ACKNOWLEDGE_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=OPERATOR_REF,
            actor=ACTOR,
            decision="accepted",
        )

    def budget_cap() -> None:
        barrier.wait(timeout=30.0)
        answers["budget"] = meter(ctx, canary, output_totals=(900,), pgid=child.pgid)

    threads = [threading.Thread(target=target) for target in (operator_cancel, budget_cap)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)

    # Whichever thread won, exactly one control holds the lease and the
    # other carries no receipt -- and the breach is recorded either way.
    assert sorted(acknowledgement_dispositions(canary)) == ["accepted", "superseded"]
    assert answers["budget"]["notice"]["blocking"] is False
    assert answers["budget"]["led"] is (answers["operator"]["disposition"] == "superseded")
    assert answers["budget"]["terminated"] is answers["budget"]["led"]
    for fact in control_facts(canary):
        if fact["disposition"] == "superseded":
            assert fact["receipt_ref"] is None
            assert fact["effect_ref"] is None


def test_a_budget_termination_that_lost_the_lease_still_records_its_notice(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """The breach is a reading about the Run, not a claim about the control."""
    child.read_chunks(CHUNKS)
    call(
        RUN_CONTROL_REQUEST_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=OPERATOR_REF,
        control="cancel",
        actor=ACTOR,
    )
    call(
        RUN_CONTROL_ACKNOWLEDGE_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=OPERATOR_REF,
        actor=ACTOR,
        decision="accepted",
    )

    answer = meter(ctx, canary, output_totals=(900,), pgid=child.pgid)

    assert answer["led"] is False
    assert answer["terminated"] is False
    assert answer["notice"]["blocking"] is False
    assert notices(canary)[0]["observed_tokens"] == CAP
    assert "control:effected" not in ledger_kinds(canary)
    assert child.alive()
    assert stored_status(canary) == "RUNNING"


def test_a_cap_with_no_addressable_group_records_the_breach_and_signals_nothing(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """You cannot reap a process you cannot address, and saying so is honest."""
    answer = meter(ctx, canary, output_totals=(900,), pgid=None)

    assert answer["over_cap"] is True
    assert answer["led"] is True
    assert answer["terminated"] is False
    assert notices(canary)[0]["cap_tokens"] == CAP
    assert stored_status(canary) == "RUNNING"


# ---------------------------------------------------------------------------
# Replay and error paths
# ---------------------------------------------------------------------------


def test_replaying_the_meter_verb_writes_no_second_notice(
    ctx: MethodContext, canary: CanaryProvision, child: LiveChild
) -> None:
    """A retry is answered from the ledger it already wrote, not appended to.

    The retry also runs entirely against a Run the first call compacted out
    of the document, so the status it answers with is the verb resolving
    the record from the tier that now holds it.
    """
    child.read_chunks(CHUNKS)
    first = meter(ctx, canary, output_totals=(900,), pgid=child.pgid)
    child.observed_ledger()

    second = meter(ctx, canary, output_totals=(900,), pgid=None)

    assert first["terminated"] is True
    assert second["notice"] == first["notice"]
    assert second["run_status"] == "CANCELLED"
    assert len(notices(canary)) == 1
    assert ledger_kinds(canary).count("control:effected") == 1
    assert stored_status(canary) == "CANCELLED"


def test_an_unknown_meter_parameter_is_refused_before_anything_is_written(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_BUDGET_METER_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=BUDGET_REF,
            actor=ACTOR,
            idempotency_key="idem-bad",
            enforcement="hard",
        )
    assert ledger_kinds(canary) == []


def test_a_non_positive_multiplier_is_refused(ctx: MethodContext, canary: CanaryProvision) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_BUDGET_METER_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=BUDGET_REF,
            actor=ACTOR,
            base_budget=CAP,
            multiplier=0.0,
            idempotency_key="idem-zero",
        )
    assert ledger_kinds(canary) == []


def test_a_negative_usage_reading_is_refused(ctx: MethodContext, canary: CanaryProvision) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_BUDGET_METER_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=BUDGET_REF,
            actor=ACTOR,
            samples=[{"input_tokens": -1, "output_tokens": 0}],
            idempotency_key="idem-negative",
        )
    assert ledger_kinds(canary) == []


def test_a_reading_adding_reasoning_to_output_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The double-counted summand is refused at the fence, not metered."""
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_BUDGET_METER_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=BUDGET_REF,
            actor=ACTOR,
            samples=[{"output_tokens": 10, "reasoning_output_tokens": 11}],
            idempotency_key="idem-summand",
        )
    assert ledger_kinds(canary) == []


def test_metering_a_run_the_document_does_not_hold_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            RUN_BUDGET_METER_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000099",
            control_request_ref=BUDGET_REF,
            actor=ACTOR,
            samples=usage_stream(output_totals=(900,)),
            base_budget=CAP,
            multiplier=1.0,
            idempotency_key="idem-missing",
        )
