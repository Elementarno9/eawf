"""A ledger line committed before a crash is still in the firehose after it.

A ledger-only mutation -- here the budget notice and the control facts the
in-flight cap verb files -- commits through the same seven steps a
transition does: a WAL intent, the document's sequence bump, the ledger
line, the firehose row, the durable mark, and only then, with the locks
released, the projection publish. Each test kills the verb at one of those
seams and then asks the question a subscriber that reconnects asks: what
does the firehose say the run ledger holds?

The kill is a ``BaseException`` rather than an ``Exception``, so no
handler on the way out can mistake it for a refusal and tidy up after it;
what the disk holds when it propagates is what a killed process leaves.
The replay is the daemon's own boot pass, and the rebuild is the bus's
own catch-up read, so both halves are the production paths.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import epoch2_transaction, methods
from eawf.runtime.daemon.bus import EventBus, catch_up
from eawf.runtime.daemon.epoch2_recovery import LEDGER_LINE_KEY, replay_native_wal
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY, LEDGER_EVENT_NAMES
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import run_budget as run_budget_module
from eawf.runtime.daemon.methods.run_budget import RUN_BUDGET_METER_METHOD
from eawf.runtime.daemon.wal import WalStatus, list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
BUDGET_REF: Final = "CTL-0000000a"

#: A reading well over the cap, with no process group to signal, so the
#: verb files the notice, the request and the acknowledgement and stops
#: there: three ledger lines and no transition.
CAP: Final = 1_000
OVER_CAP_OUTPUT: Final = 5_000


class _Kill(BaseException):
    """The process dying at the seam a test chose."""


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
def bus() -> EventBus:
    """The projection fan-out the verb publishes into."""
    return EventBus()


@pytest.fixture
def ctx(tmp_path: Path, bus: EventBus) -> MethodContext:
    """A daemon context with a WAL directory and a bus of its own."""
    return dataclasses.replace(method_context(tmp_path / "runtime"), bus=bus)


def _meter(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Drive the in-flight cap verb over one reading past the cap."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "control_request_ref": BUDGET_REF,
        "actor": "OP-0001",
        "samples": [{"input_tokens": 100, "output_tokens": OVER_CAP_OUTPUT}],
        "base_budget": CAP,
        "enforce": "hard",
        "multiplier": 1.0,
        "idempotency_key": "idem-budget",
    }
    return asyncio.run(methods.dispatch(RUN_BUDGET_METER_METHOD, ctx, params))


def _run_ledger(canary: CanaryProvision) -> list[LedgerRecord]:
    """Return the run ledger's lines, in file order."""
    return list(read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN)))


def _rebuilt_run_ledger(bus: EventBus, canary: CanaryProvision) -> list[LedgerRecord]:
    """Rebuild the run ledger's view from the firehose alone.

    A fresh subscriber catches up from the top of the firehose, the way a
    reconnecting client does, and every ledger-append row is folded back
    into the line it carries.
    """
    subscriber = bus.register(connection_id=f"rebuild-{uuid.uuid4().hex}")
    rows: list[Envelope] = catch_up(subscriber, firehose_path(canary))
    return [
        LedgerRecord.model_validate_json(row.payload[LEDGER_LINE_KEY])
        for row in rows
        if row.payload.get("name") == "ledger.run.appended"
    ]


def _wal_statuses(ctx: MethodContext) -> list[str]:
    """Return the status of every record in the canary's one WAL namespace."""
    assert isinstance(ctx.wal_dir, Path)
    namespaces = sorted((ctx.wal_dir / "native").iterdir())
    assert len(namespaces) == 1
    return [path.name.split(".")[1] for path in list_records(namespaces[0])]


def _kinds(records: list[LedgerRecord]) -> list[str]:
    """Return what each line is, notice or control phase."""
    return [
        f"control:{item.payload['phase']}"
        if item.payload.get("payload_kind") == "control"
        else str(item.payload.get("payload_kind"))
        for item in records
    ]


def test_uncrashed_meter_publishes_every_committed_line(
    ctx: MethodContext, canary: CanaryProvision, bus: EventBus
) -> None:
    """The baseline: each line reaches the ledger, the firehose and the bus."""
    live = bus.register(connection_id="live")

    answer = _meter(ctx, canary)

    assert answer["notice"] is not None
    assert _kinds(_run_ledger(canary)) == [
        "budget_notice",
        "control:requested",
        "control:acknowledged",
    ]
    published = list(live.queue)
    assert [row.payload["name"] for row in published] == ["ledger.run.appended"] * 3
    assert all(row.payload["name"] in LEDGER_EVENT_NAMES for row in published)
    assert [row.payload["canonical_sequence"] for row in published] == [1, 2, 3]
    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == 3


def test_crash_before_the_projection_write_rebuilds_from_the_firehose(
    ctx: MethodContext,
    canary: CanaryProvision,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit is durable and the publish never happened."""
    live = bus.register(connection_id="live")

    def killed(_bus: Any, _envelope: Envelope) -> bool:
        raise _Kill

    monkeypatch.setattr(run_budget_module, "publish_projection", killed)

    with pytest.raises(_Kill):
        _meter(ctx, canary)

    assert list(live.queue) == []
    ledger = _run_ledger(canary)
    assert _kinds(ledger) == ["budget_notice", "control:requested", "control:acknowledged"]
    assert _rebuilt_run_ledger(bus, canary) == ledger
    assert _wal_statuses(ctx) == [WalStatus.FSYNCED.value] * 3


def test_crash_after_the_append_and_before_the_row_is_replayed(
    ctx: MethodContext,
    canary: CanaryProvision,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line landed and its row did not; the boot replay appends the row."""

    def killed(_path: Path, _line: str, **_kwargs: Any) -> None:
        raise _Kill

    monkeypatch.setattr(epoch2_transaction, "append_json_line", killed)

    with pytest.raises(_Kill):
        _meter(ctx, canary)
    monkeypatch.undo()

    ledger = _run_ledger(canary)
    assert _kinds(ledger) == ["budget_notice"]
    assert _rebuilt_run_ledger(bus, canary) == []
    assert _wal_statuses(ctx) == [WalStatus.APPLIED.value]

    report = replay_native_wal(ctx.wal_dir)

    assert (report.replayed_event_count, report.replayed_ledger_count) == (1, 0)
    assert _run_ledger(canary) == ledger
    assert _rebuilt_run_ledger(bus, canary) == ledger


def test_crash_after_the_document_and_before_the_append_is_replayed(
    ctx: MethodContext,
    canary: CanaryProvision,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sequence bump landed and the line did not; the replay files both."""

    def killed(_path: Path, _record: LedgerRecord, **_kwargs: Any) -> int:
        raise _Kill

    monkeypatch.setattr(epoch2_transaction, "append_ledger_record", killed)

    with pytest.raises(_Kill):
        _meter(ctx, canary)
    monkeypatch.undo()

    assert _run_ledger(canary) == []
    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == 1

    first = replay_native_wal(ctx.wal_dir)
    second = replay_native_wal(ctx.wal_dir)

    assert (first.applied_count, first.replayed_ledger_count) == (1, 1)
    assert first.replayed_event_count == 1
    assert (second.replayed_ledger_count, second.replayed_event_count) == (0, 0)
    ledger = _run_ledger(canary)
    assert _kinds(ledger) == ["budget_notice"]
    assert _rebuilt_run_ledger(bus, canary) == ledger


def test_retry_after_a_replayed_append_files_no_second_notice(
    ctx: MethodContext,
    canary: CanaryProvision,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replayed notice is the one the retry finds standing."""

    def killed(_path: Path, _record: LedgerRecord, **_kwargs: Any) -> int:
        raise _Kill

    monkeypatch.setattr(epoch2_transaction, "append_ledger_record", killed)
    with pytest.raises(_Kill):
        _meter(ctx, canary)
    monkeypatch.undo()
    replay_native_wal(ctx.wal_dir)

    answer = _meter(ctx, canary)

    ledger = _run_ledger(canary)
    assert _kinds(ledger) == ["budget_notice", "control:requested", "control:acknowledged"]
    assert answer["notice"] == ledger[0].payload
    assert _rebuilt_run_ledger(bus, canary) == ledger


def test_crash_before_the_document_write_leaves_nothing(
    ctx: MethodContext,
    canary: CanaryProvision,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intent with no document write behind it is abandoned, not finished."""
    before = document_path(canary).read_bytes()

    def killed(_session: RootSession, _document: dict[str, Any]) -> None:
        raise _Kill

    monkeypatch.setattr(RootSession, "write_document", killed)

    with pytest.raises(_Kill):
        _meter(ctx, canary)
    monkeypatch.undo()

    report = replay_native_wal(ctx.wal_dir)

    assert (report.abandoned_count, report.replayed_ledger_count) == (1, 0)
    assert report.replayed_event_count == 0
    assert document_path(canary).read_bytes() == before
    assert _run_ledger(canary) == []
    assert _rebuilt_run_ledger(bus, canary) == []
