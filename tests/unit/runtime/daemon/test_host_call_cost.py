"""What one host tool call costs the daemon, counted in decodes, writes and round trips.

A hook's daemon call must answer inside its deadline on a document of many
megabytes, so the work it does under the document lock is bounded: the Runs of
a host session are read from the status projection, one observation reads and
writes the document once for all its ledger appends, finding the call's receipt
validates only the receipts carrying its id, and the hook pings the daemon at
most once before its call.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the hook's exit status is the contract

from eawf import __version__
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.semantic import TOOL_SCHEMA_VERSION, SemanticResult
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.store import compaction
from eawf.kernel.store.compaction import (
    document_rows,
    read_document,
    read_status_rows,
    write_document,
)
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records, render_ledger_line
from eawf.kernel.store.paths import (
    collection_ledger_paths,
    record_ledger_path,
    status_projection_path,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import epoch2_root, epoch2_transaction, host_calls
from eawf.runtime.daemon.epoch2_recovery import replay_native_wal
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootFencedError
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append, ledger_append_batch
from eawf.runtime.daemon.host_calls import HostCallReceipt, host_call_id, observe_host_call
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.permission import host_session_runs
from eawf.runtime.hooks import daemon_start
from eawf.runtime.hooks import runner as runner_module
from eawf.runtime.hooks.context import HookContext
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.cli import _daemon_client
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonNotRunningError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import DaemonVersionMismatch
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    firehose_path,
    provision,
    root_context,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    RUN_URN,
    SESSION,
)

_NOW: Final = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
_OTHER_SESSION: Final = "host-session-0002"
_HOLDING: Final = frozenset({RunStatus.RUNNING.value, RunStatus.SUSPENDED.value})
_CALL_KEY: Final = "toolu_01bash"


def _run(key: str, status: RunStatus, session: str | None) -> dict[str, Any]:
    row = seed_row("run", status.value)
    row["key"] = key
    row["urn"] = RUN_URN.replace(RUN_KEY, key)
    if session is not None:
        row["vendor_session"] = {
            "harness": "claude-code",
            "session_digest": hash_vendor_session_id(session),
        }
    return row


def _canary(root: Path, runs: dict[str, dict[str, Any]]) -> CanaryProvision:
    provisioned = provision(root)
    seed(provisioned, {"run": runs})
    return provisioned


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run on the host session."""
    return _canary(tmp_path / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)})


def _decodes(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[Path]:
    """Count every decode of the committed document at *path*."""
    seen: list[Path] = []
    original = compaction._decode_object

    def counting(raw: bytes, where: Path) -> dict[str, Any]:
        if where == path:
            seen.append(where)
        return original(raw, where)

    monkeypatch.setattr(compaction, "_decode_object", counting)
    return seen


def _expected_runs(path: Path, session: str) -> list[str]:
    digest = hash_vendor_session_id(session)
    return sorted(
        row["urn"]
        for row in document_rows(read_document(path), Epoch2Collection.RUN).values()
        if row.get("status") in _HOLDING
        and (row.get("vendor_session") or {}).get("session_digest") == digest
    )


_SEVERAL: Final = {
    "RUN-00000010": _run("RUN-00000010", RunStatus.RUNNING, SESSION),
    "RUN-00000011": _run("RUN-00000011", RunStatus.SUSPENDED, SESSION),
    "RUN-00000012": _run("RUN-00000012", RunStatus.RUNNING, SESSION),
    "RUN-00000013": _run("RUN-00000013", RunStatus.COMPLETED, SESSION),
    "RUN-00000014": _run("RUN-00000014", RunStatus.RUNNING, _OTHER_SESSION),
    "RUN-00000015": _run("RUN-00000015", RunStatus.RUNNING, None),
}


@pytest.mark.parametrize(
    ("case", "runs", "count"),
    [
        ("zero", {"RUN-00000014": _SEVERAL["RUN-00000014"]}, 0),
        ("one", {RUN_KEY: _SEVERAL[RUN_KEY]}, 1),
        ("several", _SEVERAL, 3),
        ("after-committed-write", _SEVERAL, 3),
    ],
)
def test_session_runs_without_document_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    runs: dict[str, dict[str, Any]],
    count: int,
) -> None:
    provisioned = _canary(tmp_path / "repo", runs)
    path = document_path(provisioned)
    if case == "after-committed-write":
        # A write that moves the committed file leaves the replaced projection
        # beside the new one, so the reader has to tell which one belongs.
        document = read_document(path)
        document["canonical_sequence"] = document.get("canonical_sequence", 0) + 3
        write_document(path, document, appends_to=Epoch2Collection.MILESTONE)
        assert json.loads(status_projection_path(path).read_text())["previous"] is not None
    authority = require_native_authority(tree_root(provisioned))
    decodes = _decodes(monkeypatch, path)

    live = host_session_runs(authority, SESSION)

    assert decodes == []
    assert live == _expected_runs(path, SESSION)
    assert len(live) == count


def test_status_rows_of_a_tree_with_no_projection_come_from_the_document(
    canary: CanaryProvision,
) -> None:
    path = document_path(canary)
    expected = document_rows(read_document(path), Epoch2Collection.RUN)
    status_projection_path(path).unlink()
    committed = json.loads(path.read_text())
    committed["run"] = expected
    path.write_text(json.dumps(committed))

    assert read_status_rows(path, Epoch2Collection.RUN) == expected


@pytest.mark.parametrize(("held", "rows"), [({}, {}), ({"run": {"RUN-1": {"k": 1}}}, None)])
def test_status_rows_of_a_projection_from_before_the_split(
    canary: CanaryProvision, held: dict[str, Any], rows: dict[str, Any] | None
) -> None:
    path = document_path(canary)
    status_projection_path(path).write_text(json.dumps(held))

    assert read_status_rows(path, Epoch2Collection.RUN) == (held["run"] if rows is None else rows)
    assert read_status_rows(path, Epoch2Collection.RUN) == document_rows(
        read_document(path), Epoch2Collection.RUN
    )


def test_status_rows_refuse_a_projection_holding_no_object(canary: CanaryProvision) -> None:
    path = document_path(canary)
    status_projection_path(path).write_text(json.dumps({"run": ["RUN-1"]}))

    with pytest.raises(ValueError, match="not an object"):
        read_status_rows(path, Epoch2Collection.RUN)


# ---- one observation, one document read and one write ------------------------------------------


def _counted_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"read": 0, "write": 0}
    # The session binds the store's reader and writer under its own module's names.

    def counting_read(path: Path) -> dict[str, Any]:
        counts["read"] += 1
        return read_document(path)

    def counting_write(path: Path, document: dict[str, Any], **kwargs: Any) -> None:
        counts["write"] += 1
        write_document(path, document, **kwargs)

    monkeypatch.setattr(epoch2_root, "read_document", counting_read)
    monkeypatch.setattr(epoch2_root, "write_document", counting_write)
    return counts


def _observe(context: Epoch2RootContext, *, key: str = _CALL_KEY, output: str | None) -> Any:
    return observe_host_call(
        context,
        run_ref=parse_qualified_urn(RUN_URN),
        tool_name="Bash",
        host_call_key=key,
        output=output,
        failed=False,
        actor=HARNESS_ACTORS["claude-code"],
        now=_NOW,
    )


def _ledgers(provisioned: CanaryProvision) -> dict[str, bytes]:
    path = document_path(provisioned)
    held: dict[str, bytes] = {}
    for collection in (Epoch2Collection.RUN, Epoch2Collection.RECEIPT, Epoch2Collection.ARTIFACT):
        for ledger in collection_ledger_paths(path, collection):
            if ledger.is_file():
                held[f"{collection.value}/{ledger.parent.name}"] = ledger.read_bytes()
    return held


def _firehose(provisioned: CanaryProvision) -> list[tuple[Any, ...]]:
    path = firehose_path(provisioned)
    text = path.read_text() if path.is_file() else ""
    rows = [json.loads(line) for line in text.splitlines()]
    return [
        (
            row["payload"]["name"],
            row["payload"].get("record_key"),
            row["payload"]["canonical_sequence"],
            row["payload"].get("ledger_line"),
        )
        for row in rows
    ]


def _one_commit_per_append(_session: Any) -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


@pytest.mark.parametrize("output", ["built 3 targets\nall green", None])
def test_observation_single_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str | None
) -> None:
    # The receipt id is the one random part of the rows compared.
    monkeypatch.setattr(secrets, "token_hex", lambda size=8: "0" * (2 * size))
    trees = {}
    for name in ("batched", "earlier"):
        provisioned = _canary(
            tmp_path / name / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)}
        )
        trees[name] = provisioned
    batched = root_context(trees["batched"], tmp_path / "batched" / "runtime")
    earlier = root_context(trees["earlier"], tmp_path / "earlier" / "runtime")

    counts = _counted_io(monkeypatch)
    answer = _observe(batched, output=output)
    io = dict(counts)
    with monkeypatch.context() as patch:
        patch.setattr(host_calls, "ledger_append_batch", _one_commit_per_append)
        counts.update(read=0, write=0)
        earlier_answer = _observe(earlier, output=output)
        earlier_io = dict(counts)

    assert io == {"read": 1, "write": 1}
    assert earlier_io["write"] == (5 if output is not None else 1)
    assert answer[0] == earlier_answer[0]
    assert (answer[1] is None) == (earlier_answer[1] is None)
    assert _ledgers(trees["batched"]) == _ledgers(trees["earlier"])
    assert _firehose(trees["batched"]) == _firehose(trees["earlier"])
    batched_document = read_document(document_path(trees["batched"]))
    earlier_document = read_document(document_path(trees["earlier"]))
    assert batched_document["canonical_sequence"] == earlier_document["canonical_sequence"]
    assert batched_document["run"] == earlier_document["run"]
    durable = {}
    for name in ("batched", "earlier"):
        wal = tmp_path / name / "runtime" / "wal"
        assert [*wal.rglob("*.pending.json"), *wal.rglob("*.applied.json")] == []
        durable[name] = len([*wal.rglob("*.fsynced.json")])
    assert durable["batched"] == durable["earlier"] == len(_firehose(trees["batched"]))


def test_a_retried_observation_appends_nothing_and_writes_nothing(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    first = _observe(context, output="ok")
    before = (_ledgers(canary), _firehose(canary))
    counts = _counted_io(monkeypatch)

    again = _observe(context, output="ok")

    assert again[1] is not None and first[1] is not None
    assert again[1].receipt_id == first[1].receipt_id
    assert (_ledgers(canary), _firehose(canary)) == before
    assert counts == {"read": 1, "write": 0}


def test_a_batch_that_raises_still_lands_the_lines_it_appended(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    path = document_path(canary)
    sequence = read_document(path).get("canonical_sequence", 0)
    record = _receipt_record(host_call_id(parse_qualified_urn(RUN_URN), "toolu_raise"), 1)

    with (
        pytest.raises(RuntimeError, match="after the append"),
        context.session([RUN_URN]) as session,
        ledger_append_batch(session),
    ):
        commit_ledger_append(session, record)
        raise RuntimeError("the block failed after the append")

    assert read_document(path)["canonical_sequence"] == sequence + 1
    assert _firehose(canary)[-1][1] == record.record_key
    ledger = record_ledger_path(path, Epoch2Collection.RECEIPT, record.payload)
    assert [item.record_key for item in read_ledger_records(ledger)] == [record.record_key]


class _Faults:
    """Fails the *nth* call of one writer; a died process also fails every write after."""

    def __init__(self, nth: int, *, dies: bool) -> None:
        self.nth, self.dies, self.calls, self.dead = nth, dies, 0, False

    def wrap(self, writer: Callable[..., Any], *, counted: bool) -> Callable[..., Any]:
        def faulty(*args: Any, **kwargs: Any) -> Any:
            if self.dead:
                raise OSError("the process is gone")
            if counted:
                self.calls += 1
                if self.calls == self.nth:
                    self.dead = self.dies
                    raise OSError("the writer failed")
            return writer(*args, **kwargs)

        return faulty


def _inject(patch: pytest.MonkeyPatch, fault: str) -> None:
    """Fail the writer *fault* names inside one observation."""
    name, nth, dies = {
        "write_pending-dies": ("write_pending", 3, True),
        "write_document-dies": ("write_document", 1, True),
        "write_document-raises": ("write_document", 1, False),
        "line-unlanded": ("append_ledger_record", 2, False),
    }[fault]
    faults = _Faults(nth, dies=dies)
    for writer, module in (
        ("write_pending", epoch2_transaction),
        ("append_ledger_record", epoch2_transaction),
        ("write_document", epoch2_root),
    ):
        patch.setattr(module, writer, faults.wrap(getattr(module, writer), counted=writer == name))


def _wal_files(wal: Path) -> list[str]:
    return sorted(path.name for path in wal.rglob("*.json"))


@pytest.mark.parametrize(
    "fault",
    ["write_pending-dies", "write_document-dies", "write_document-raises", "line-unlanded"],
)
def test_a_failed_batch_converges_with_the_per_append_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda size=8: "0" * (2 * size))
    trees: dict[str, CanaryProvision] = {}
    for name in ("batched", "earlier"):
        provisioned = _canary(
            tmp_path / name / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)}
        )
        trees[name] = provisioned
        context = root_context(provisioned, tmp_path / name / "runtime")
        with monkeypatch.context() as patch:
            if name == "earlier":
                patch.setattr(host_calls, "ledger_append_batch", _one_commit_per_append)
            _inject(patch, fault)
            with pytest.raises(OSError):
                _observe(context, output="ok")
        # Before any replay, no firehose row may name a line its ledger lacks.
        lines = b"".join(_ledgers(provisioned).values())
        assert all(row[3].encode() in lines for row in _firehose(provisioned))
        wal = tmp_path / name / "runtime" / "wal"
        if name == "batched" and fault == "write_document-raises":
            # The daemon keeps running with its writer healthy again: the root
            # stays shut, so the next call cannot draw the stranded sequences.
            before = (_ledgers(provisioned), _firehose(provisioned), _wal_files(wal))
            with pytest.raises(RootFencedError, match="restart the daemon"):
                _observe(context, key="toolu_02", output="next")
            assert (_ledgers(provisioned), _firehose(provisioned), _wal_files(wal)) == before
        replay_native_wal(wal)
        restarted = root_context(provisioned, tmp_path / name / "runtime")
        _observe(restarted, output="ok")
        _observe(restarted, key="toolu_02", output="next")

    assert _ledgers(trees["batched"]) == _ledgers(trees["earlier"])
    assert _firehose(trees["batched"]) == _firehose(trees["earlier"])
    sequences = [row[2] for row in _firehose(trees["batched"])]
    assert sequences == sorted(set(sequences))
    held = [read_document(document_path(trees[name]))["canonical_sequence"] for name in trees]
    assert held == [sequences[-1], sequences[-1]]
    for name in trees:
        wal = tmp_path / name / "runtime" / "wal"
        assert [*wal.rglob("*.pending.json"), *wal.rglob("*.applied.json")] == []


def _mentions(provisioned: CanaryProvision, wal: Path, text: str) -> bool:
    """Return whether a ledger, the firehose or a WAL record of the tree names *text*."""
    held = [*_ledgers(provisioned).values(), *(path.read_bytes() for path in wal.rglob("*.json"))]
    path = firehose_path(provisioned)
    if path.is_file():
        held.append(path.read_bytes())
    return any(text.encode() in item for item in held)


def test_a_session_waiting_on_the_lock_is_refused_once_the_root_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda size=8: "0" * (2 * size))
    trees: dict[str, CanaryProvision] = {}
    for name in ("batched", "earlier"):
        trees[name] = _canary(
            tmp_path / name / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)}
        )
    batched, wal = trees["batched"], tmp_path / "batched" / "runtime" / "wal"
    context = root_context(batched, tmp_path / "batched" / "runtime")
    writing, waiting = threading.Event(), threading.Event()
    outcome: dict[str, BaseException | None] = {}
    early = context._refuse_if_fenced

    def checked() -> None:
        early()
        if threading.current_thread().name == "waiter":
            waiting.set()

    def failing_write(*_args: object, **_kwargs: object) -> None:
        writing.set()
        assert waiting.wait(5), "the second session never reached the lock"
        raise OSError("no space left on device")

    def run(name: str, key: str, output: str) -> None:
        try:
            _observe(context, key=key, output=output)
            outcome[name] = None
        except BaseException as error:
            outcome[name] = error

    second = threading.Thread(target=run, args=("waiter", "toolu_02", "next"), name="waiter")
    with monkeypatch.context() as patch:
        patch.setattr(context, "_refuse_if_fenced", checked)
        patch.setattr(epoch2_root, "write_document", failing_write)
        first = threading.Thread(target=run, args=("holder", _CALL_KEY, "ok"), name="holder")
        first.start()
        assert writing.wait(5)
        second.start()
        first.join(10)
        second.join(10)

    assert isinstance(outcome["holder"], OSError)
    assert isinstance(outcome["waiter"], RootFencedError)
    other = host_call_id(parse_qualified_urn(RUN_URN), "toolu_02")
    assert not _mentions(batched, wal, other)
    replay_native_wal(wal)
    restarted = root_context(batched, tmp_path / "batched" / "runtime")
    _observe(restarted, output="ok")
    _observe(restarted, key="toolu_02", output="next")
    earlier = root_context(trees["earlier"], tmp_path / "earlier" / "runtime")
    _observe(earlier, output="ok")
    _observe(earlier, key="toolu_02", output="next")

    assert _ledgers(batched) == _ledgers(trees["earlier"])
    assert _firehose(batched) == _firehose(trees["earlier"])
    sequences = [row[2] for row in _firehose(batched)]
    assert sequences == sorted(set(sequences))


def test_a_refused_single_append_is_never_finished_by_the_replay(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    refused = _receipt_record(host_call_id(parse_qualified_urn(RUN_URN), "toolu_refused"), 1)
    good = _receipt_record(host_call_id(parse_qualified_urn(RUN_URN), "toolu_good"), 2)

    def failing_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(epoch2_root, "write_document", failing_write)
        with pytest.raises(OSError), context.session([RUN_URN]) as session:
            commit_ledger_append(session, refused)
    with context.session([RUN_URN]) as session:
        commit_ledger_append(session, good)
    wal = tmp_path / "runtime" / "wal"

    report = replay_native_wal(wal)

    assert report.completed_count == 0
    assert len([*wal.rglob("*.poisoned.json")]) == 1
    published = [*_ledgers(canary).values(), firehose_path(canary).read_bytes()]
    assert all(refused.record_key.encode() not in item for item in published)
    sequences = [row[2] for row in _firehose(canary)]
    assert sequences == sorted(set(sequences))


def test_the_replay_never_finishes_a_record_whose_sequence_another_row_holds(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    record = _receipt_record(host_call_id(parse_qualified_urn(RUN_URN), "toolu_dup"), 1)
    with monkeypatch.context() as patch:
        # The record lands its document write, then dies before its mark and line.
        patch.setattr(epoch2_transaction, "mark_applied", _crash)
        with pytest.raises(OSError), context.session([RUN_URN]) as session:
            commit_ledger_append(session, record)
    sequence = read_document(document_path(canary))["canonical_sequence"]
    row = {
        "id": "evt-other",
        "payload": {"name": "ledger.run.appended", "canonical_sequence": sequence},
    }
    path = firehose_path(canary)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(row)}\n")

    report = replay_native_wal(tmp_path / "runtime" / "wal")

    assert (report.completed_count, report.replayed_event_count) == (0, 0)
    assert [*(tmp_path / "runtime" / "wal").rglob("*.poisoned.json")] != []
    assert [r[2] for r in _firehose_raw(canary)].count(sequence) == 1


def _crash(*_args: object, **_kwargs: object) -> None:
    raise OSError("the process died here")


def _firehose_raw(provisioned: CanaryProvision) -> list[tuple[Any, ...]]:
    lines = firehose_path(provisioned).read_text().splitlines()
    return [
        (row["id"], row["payload"].get("name"), row["payload"]["canonical_sequence"])
        for row in map(json.loads, lines)
    ]


class _Clock:
    """Stands in for ``datetime`` in the transaction: each reading steps by *step* seconds."""

    def __init__(self, step: int) -> None:
        self._step, self._at = step, datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def now(self, _tz: object = None) -> datetime:
        self._at = self._at + timedelta(seconds=self._step)
        return self._at


@pytest.mark.parametrize("step", [-1, 0], ids=["reversed", "identical"])
def test_a_chain_replays_in_sequence_order_whatever_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    provisioned = _canary(tmp_path / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)})
    context = root_context(provisioned, tmp_path / "runtime")
    with monkeypatch.context() as patch:
        patch.setattr(epoch2_transaction, "datetime", _Clock(step))
        _inject(patch, "write_document-dies")
        with pytest.raises(OSError):
            _observe(context, output="ok")
    wal = tmp_path / "runtime" / "wal"
    assert len([*wal.rglob("*.pending.json")]) == 5

    report = replay_native_wal(wal)

    assert (report.completed_count, report.abandoned_count, report.poisoned_count) == (5, 0, 0)
    sequences = [row[2] for row in _firehose(provisioned)]
    held = read_document(document_path(provisioned))["canonical_sequence"]
    assert sequences == list(range(held - 4, held + 1))


@pytest.mark.parametrize("crash_at", ["mark_applied", "append_json_line"])
def test_a_crash_after_the_batch_write_is_finished_by_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    provisioned = _canary(tmp_path / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)})
    context = root_context(provisioned, tmp_path / "runtime")
    before = read_document(document_path(provisioned)).get("canonical_sequence", 0)

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("the process died here")

    with monkeypatch.context() as patch:
        patch.setattr(epoch2_transaction, crash_at, crash)
        with pytest.raises(OSError, match="died"):
            _observe(context, output="ok")
    ledgers = _ledgers(provisioned)

    report = replay_native_wal(tmp_path / "runtime" / "wal")

    assert report.abandoned_count == 0
    assert report.replayed_event_count == 5
    assert report.replayed_ledger_count == 0
    assert _ledgers(provisioned) == ledgers
    after = read_document(document_path(provisioned))["canonical_sequence"]
    assert [row[2] for row in _firehose(provisioned)] == list(range(before + 1, after + 1))
    wal = tmp_path / "runtime" / "wal"
    assert [*wal.rglob("*.pending.json"), *wal.rglob("*.applied.json")] == []


def test_an_empty_batch_writes_nothing(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    counts = _counted_io(monkeypatch)

    with context.session([RUN_URN]) as session, ledger_append_batch(session):
        assert session.read_document() is session.read_document()

    assert counts == {"read": 1, "write": 0}


def test_a_committed_line_in_a_batch_takes_its_own_write(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = root_context(canary, tmp_path / "runtime")
    path = document_path(canary)
    committed_before = json.loads(path.read_text()).get("canonical_sequence", 0)
    local = _receipt_record(host_call_id(parse_qualified_urn(RUN_URN), "toolu_local"), 1)
    committed = LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key="receipt-committed",
        status="recorded",
        recorded_at=_NOW,
        payload={"payload_kind": "manual_receipt", "note": "kept"},
    )
    counts = _counted_io(monkeypatch)

    with context.session([RUN_URN]) as session, ledger_append_batch(session):
        commit_ledger_append(session, local)
        commit_ledger_append(session, committed)
        commit_ledger_append(session, local.model_copy(update={"record_key": "call-again"}))

    assert counts["write"] == 3
    live = read_document(path)["canonical_sequence"]
    assert json.loads(path.read_text())["canonical_sequence"] == live - 1 > committed_before
    assert [row[2] for row in _firehose(canary)[-3:]] == [live - 2, live - 1, live]


def test_a_closed_session_refuses_a_batch(canary: CanaryProvision, tmp_path: Path) -> None:
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        pass

    with pytest.raises(RuntimeError, match="closed"), ledger_append_batch(session):
        pass


# ---- the receipt lookup reads only the call's own receipts -------------------------------------


def _receipt_payload(call_id: str, ordinal: int) -> dict[str, Any]:
    receipt_id = f"receipt-{ordinal:016x}"
    run_ref = parse_qualified_urn(RUN_URN)
    return HostCallReceipt(
        receipt_id=receipt_id,
        call_id=call_id,
        run_ref=run_ref,
        tool_id="bash",
        result=_result(call_id, receipt_id),
        recorded_at=_NOW,
    ).model_dump(mode="json")


def _result(call_id: str, receipt_id: str) -> SemanticResult:
    return SemanticResult(
        call_id=call_id,
        run_ref=parse_qualified_urn(RUN_URN),
        receipt_id=receipt_id,
        status="succeeded",
        result_schema_version=TOOL_SCHEMA_VERSION,
        output_ref=f"artifact://content/run-00000010/{'a' * 64}",
        completed_at=_NOW,
    )


def _receipt_record(call_id: str, ordinal: int) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=call_id,
        status="succeeded",
        recorded_at=_NOW,
        payload=_receipt_payload(call_id, ordinal),
    )


def _file_receipts(provisioned: CanaryProvision, others: int, *, own: bool) -> None:
    template = _receipt_payload(f"call-{0:016x}", 0)
    lines = []
    for ordinal in range(1, others + 1):
        payload = copy.deepcopy(template)
        call_id, receipt_id = f"call-{ordinal:016x}", f"receipt-{ordinal:016x}"
        payload.update(call_id=call_id, receipt_id=receipt_id)
        payload["result"].update(call_id=call_id, receipt_id=receipt_id)
        lines.append(
            render_ledger_line(
                LedgerRecord(
                    collection=Epoch2Collection.RECEIPT,
                    record_key=call_id,
                    status="succeeded",
                    recorded_at=_NOW,
                    payload=payload,
                )
            )
        )
    if own:
        own_id = host_call_id(parse_qualified_urn(RUN_URN), _CALL_KEY)
        lines.append(render_ledger_line(_receipt_record(own_id, others + 1)))
    path = record_ledger_path(document_path(provisioned), Epoch2Collection.RECEIPT, template)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _validations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    original = HostCallReceipt.model_validate

    def counting(payload: Any, *args: Any, **kwargs: Any) -> HostCallReceipt:
        seen.append(1)
        return original(payload, *args, **kwargs)

    monkeypatch.setattr(HostCallReceipt, "model_validate", counting)
    return seen


@pytest.mark.parametrize("own", [False, True])
def test_receipt_lookup_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: bool) -> None:
    counted: dict[int, int] = {}
    for others in (10, 10_000):
        provisioned = _canary(
            tmp_path / str(others) / "repo", {RUN_KEY: _run(RUN_KEY, RunStatus.RUNNING, SESSION)}
        )
        _file_receipts(provisioned, others, own=own)
        context = root_context(provisioned, tmp_path / str(others) / "runtime")
        with monkeypatch.context() as patch:
            seen = _validations(patch)
            _, receipt = _observe(context, output="ok")
        counted[others] = len(seen)
        assert receipt is not None
        if own:
            assert receipt.receipt_id == f"receipt-{others + 1:016x}"

    assert counted[10] == counted[10_000] == (1 if own else 0)


def test_a_corrupt_receipt_of_the_call_is_still_refused(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    own_id = host_call_id(parse_qualified_urn(RUN_URN), _CALL_KEY)
    payload = _receipt_payload(own_id, 1)
    payload["tool_id"] = "Not A Tool Id"
    path = record_ledger_path(document_path(canary), Epoch2Collection.RECEIPT, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _receipt_record(own_id, 1).model_copy(update={"payload": payload})
    path.write_text(f"{render_ledger_line(record)}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="tool_id"):
        _observe(root_context(canary, tmp_path / "runtime"), output="ok")


# ---- a hook's daemon call pings once ----------------------------------------------------------


class _FakeDaemon:
    """A socket that answers JSON-RPC as a daemon of *version* and records each method."""

    def __init__(self, directory: Path, version: str) -> None:
        self.methods: list[str] = []
        self._version = version
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(directory / "eawfd.sock"))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb") as stream:
                for line in stream:
                    request = json.loads(line)
                    self.methods.append(request["method"])
                    result = (
                        {"pid": os.getpid(), "version": self._version}
                        if request["method"] == "daemon.ping"
                        else {}
                    )
                    answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                    stream.write(json.dumps(answer).encode() + b"\n")
                    stream.flush()

    def close(self) -> None:
        self._listener.close()


@contextlib.contextmanager
def _daemon(monkeypatch: pytest.MonkeyPatch, version: str) -> Iterator[_FakeDaemon]:
    # AF_UNIX paths are capped near 104 bytes, past what a pytest tmp_path leaves.
    directory = Path(tempfile.mkdtemp(prefix="eawf-hc-", dir="/tmp"))
    monkeypatch.setattr(_daemon_client, "default_runtime_dir", lambda: directory)
    daemon = _FakeDaemon(directory, version)
    try:
        yield daemon
    finally:
        daemon.close()
        shutil.rmtree(directory, ignore_errors=True)


def _pings_before_call(methods: list[str]) -> int:
    first = next((i for i, name in enumerate(methods) if name != "daemon.ping"), len(methods))
    return first


def _hook_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "eawf.runtime.hooks.context.resolve_hook_context",
        lambda _cwd: HookContext(kind="managed", top=root, main=root),
    )
    monkeypatch.setattr(daemon_start, "Popen", _no_launch)
    return root


def _no_launch(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("a reached daemon asks for no start")


def _hook_run(root: Path) -> Any:
    payload = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_use_id": "toolu_01",
        "tool_response": {"stdout": "ok"},
        "cwd": str(root),
    }
    return CliRunner().invoke(
        app,
        ["--workspace", str(root), "hook", "run", "post_tool_use", "--runtime", "claude"],
        input=json.dumps(payload),
    )


@pytest.mark.parametrize("version", [__version__, "0.0.1"])
def test_hook_call_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    root = _hook_tree(tmp_path, monkeypatch)
    with _daemon(monkeypatch, version) as daemon:
        factory: Callable[[], Any] = runner_module._default_daemon_client_factory
        if version == __version__:
            with factory() as client:
                client.call("runtime.host.tool.observe", {})
            assert daemon.methods == ["daemon.ping", "runtime.host.tool.observe"]
            daemon.methods.clear()

        result = _hook_run(root)

    assert result.exit_code == 0, result.output
    assert daemon.methods, "the hook never reached the daemon"
    assert _pings_before_call(daemon.methods) <= 1
    if version != __version__:
        assert daemon.methods == ["daemon.ping"]


def test_an_attach_with_no_listener_is_no_daemon(tmp_path: Path) -> None:
    directory = Path(tempfile.mkdtemp(prefix="eawf-hc-", dir="/tmp"))
    try:
        client = DaemonClient(runtime_dir=directory, spawn=False, readiness_ping=False)
        with pytest.raises(DaemonNotRunningError):
            client.__enter__()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(directory / "eawfd.sock"))
        stale.close()
        with pytest.raises(DaemonNotRunningError):
            client.__enter__()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_attach_to_a_silent_daemon_times_out(tmp_path: Path) -> None:
    directory = Path(tempfile.mkdtemp(prefix="eawf-hc-", dir="/tmp"))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(directory / "eawfd.sock"))
        listener.listen(8)
        client = DaemonClient(
            runtime_dir=directory, spawn=False, readiness_ping=False, call_timeout_seconds=0.2
        )
        with pytest.raises(TimeoutError):
            client.__enter__()
        with pytest.raises(RuntimeError, match="not connected"):
            client.call("daemon.ping")
    finally:
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_an_attach_to_another_release_is_refused_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _daemon(monkeypatch, "0.0.1") as daemon:
        client = DaemonClient(spawn=False, readiness_ping=False)
        with pytest.raises(DaemonVersionMismatch, match="eawf daemon restart"):
            client.__enter__()
        with pytest.raises(RuntimeError, match="not connected"):
            client.call("daemon.ping")
    assert daemon.methods == ["daemon.ping"]


def test_an_attach_names_the_daemon_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        _daemon(monkeypatch, __version__),
        DaemonClient(spawn=False, readiness_ping=False) as client,
    ):
        assert client.pid == os.getpid()
