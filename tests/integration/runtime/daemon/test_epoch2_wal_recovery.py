"""A native mutation killed mid-stride recovers to exactly one of everything.

Every torn state here is made by killing the real transaction between
two of its own steps, not by hand-writing a WAL file: the step is
replaced with one that raises, the transaction is run, and whatever it
left on disk is what the replay is handed. That is the only way to know
the digests the replay reads are the digests the commit path writes.

Four kills cover the stride. Killing the document write leaves a
journalled intent over an untouched document, and the replay must
abandon it -- the mutation never happened and re-running the mutator
would invent a new one. Killing the mark that follows the document write
leaves the same pending record over a document that HAS moved, and the
replay must finish it instead. Killing the firehose append leaves an
applied record with no event, and killing the durability mark leaves an
applied record whose event is already there. Both end at one event row,
which is the whole point: the replay appends only what the log does not
already hold, so running it on every boot is safe.

The last case is the one the commit cannot undo. A publish that fails
after the commit has no bearing on the commit: the caller is answered
with its receipt and the answer carries ``projection_degraded``, because
telling a client a durable change did not happen would be a lie.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.io import state_version
from eawf.kernel.store.compaction import read_document, write_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import epoch2_transaction as transaction
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import methods, wal
from eawf.runtime.daemon.epoch2_recovery import (
    PROJECTION_DEGRADED,
    REASON_DOCUMENT_DIVERGED,
    REASON_INTENT_ABANDONED,
    REASON_ROOT_UNRESOLVED,
    publish_projection,
    replay_native_wal,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods.domain_envelope import DOMAIN_TRANSITION_METHOD
from eawf.runtime.daemon.wal import WalStatus, list_poisoned, list_records, read_record
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
KEY = "req-0001"

_BOOT_SKIP_REASON = "daemon boot path is POSIX-only in this suite"


class _CrashError(RuntimeError):
    """Stands in for the kill signal a torn transaction really takes."""


def _explode(*_args: object, **_kwargs: object) -> None:
    raise _CrashError("simulated kill")


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one planned Milestone."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})
    return provisioned


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def context(canary: CanaryProvision, runtime_root: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, runtime_root)


def _request(**overrides: Any) -> TransitionRequest:
    payload: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "to_status": "ACTIVE",
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
    }
    payload.update(overrides)
    return TransitionRequest.model_validate(payload)


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _tear(
    context: Epoch2RootContext,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str,
    owner: Any = transaction,
) -> None:
    """Run one transaction with *target* replaced by a kill, and keep the debris."""
    monkeypatch.setattr(owner, target, _explode)
    with pytest.raises(_CrashError):
        run_transaction(context=context, request=_request(), now=AT)
    monkeypatch.undo()


def _statuses(wal_dir: Path) -> list[str]:
    return sorted(path.name.split(".")[-2] for path in list_records(wal_dir))


def _poison_reasons(wal_dir: Path) -> list[str]:
    return sorted(str(read_record(path).poison_reason) for path in list_poisoned(wal_dir))


# ---------------------------------------------------------------------------
# A kill before the document write: the intent is abandoned
# ---------------------------------------------------------------------------


def test_a_kill_before_the_document_write_leaves_a_pending_intent(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = document_path(canary).read_bytes()

    _tear(context, monkeypatch, target="write_document", owner=RootSession)

    assert _statuses(context.wal_dir) == [WalStatus.PENDING.value]
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []


def test_an_abandoned_intent_is_poisoned_and_emits_no_event(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document that never moved means the mutation never happened."""
    _tear(context, monkeypatch, target="write_document", owner=RootSession)
    before = document_path(canary).read_bytes()

    report = replay_native_wal(runtime_root / "wal")

    assert (report.root_count, report.pending_count) == (1, 1)
    assert (report.abandoned_count, report.completed_count) == (1, 0)
    assert report.replayed_event_count == 0
    assert _poison_reasons(context.wal_dir) == [REASON_INTENT_ABANDONED]
    assert _firehose_rows(canary) == []
    assert document_path(canary).read_bytes() == before


# ---------------------------------------------------------------------------
# A kill after the document write: the mutation is finished
# ---------------------------------------------------------------------------


def test_a_kill_after_the_document_write_leaves_a_durable_change_untold(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tear(context, monkeypatch, target="mark_applied")

    document = read_document(document_path(canary))
    assert _statuses(context.wal_dir) == [WalStatus.PENDING.value]
    assert document["milestone"]["MLS-0030"]["status"] == "ACTIVE"
    assert _firehose_rows(canary) == []


def test_replay_finishes_a_pending_record_whose_document_landed(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tear(context, monkeypatch, target="mark_applied")

    report = replay_native_wal(runtime_root / "wal")

    assert (report.completed_count, report.abandoned_count) == (1, 0)
    assert report.replayed_event_count == 1
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]
    rows = _firehose_rows(canary)
    assert len(rows) == 1
    assert rows[0]["payload"]["name"] == "domain.milestone.activated"


def test_replay_appends_one_event_for_an_applied_record(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill between the durable mark and the firehose append loses the row."""
    _tear(context, monkeypatch, target="append_json_line")
    assert _statuses(context.wal_dir) == [WalStatus.APPLIED.value]
    assert _firehose_rows(canary) == []

    report = replay_native_wal(runtime_root / "wal")

    assert (report.applied_count, report.replayed_event_count) == (1, 1)
    assert len(_firehose_rows(canary)) == 1
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]


def test_replay_does_not_duplicate_an_event_the_log_already_holds(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill after the append must not make the boot write the row twice."""
    _tear(context, monkeypatch, target="mark_fsynced")
    assert _statuses(context.wal_dir) == [WalStatus.APPLIED.value]
    assert len(_firehose_rows(canary)) == 1

    report = replay_native_wal(runtime_root / "wal")

    assert (report.applied_count, report.replayed_event_count) == (1, 0)
    assert len(_firehose_rows(canary)) == 1
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]


def test_replay_is_idempotent_across_boots(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tear(context, monkeypatch, target="mark_applied")
    replay_native_wal(runtime_root / "wal")

    second = replay_native_wal(runtime_root / "wal")

    assert (second.pending_count, second.applied_count) == (0, 0)
    assert second.replayed_event_count == 0
    assert len(_firehose_rows(canary)) == 1


def test_a_recovered_mutation_keeps_its_canonical_sequence(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay re-issues the committed envelope, never a fresh one."""
    _tear(context, monkeypatch, target="mark_applied")
    journalled = read_record(list_records(context.wal_dir)[0])

    replay_native_wal(runtime_root / "wal")

    row = _firehose_rows(canary)[0]
    assert row["id"] == journalled.envelope.id
    assert row["payload"]["canonical_sequence"] == 1


# ---------------------------------------------------------------------------
# Records the replay refuses to act on
# ---------------------------------------------------------------------------


def test_a_wal_with_no_native_namespace_replays_to_a_zero_report(runtime_root: Path) -> None:
    """A daemon that has taken no native mutation has nothing to reconcile."""
    report = replay_native_wal(runtime_root / "wal")

    assert report.root_count == 0
    assert report.replayed_event_count == 0
    assert report.poisoned_count == 0


def test_an_empty_native_namespace_replays_to_a_zero_report(runtime_root: Path) -> None:
    (runtime_root / "wal" / "native" / "root-0000000000000000").mkdir(parents=True)

    report = replay_native_wal(runtime_root / "wal")

    assert (report.root_count, report.pending_count, report.applied_count) == (1, 0, 0)


def test_a_tampered_record_is_poisoned_rather_than_replayed(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tear(context, monkeypatch, target="append_json_line")
    record_path = list_records(context.wal_dir)[0]
    payload = orjson.loads(record_path.read_bytes())
    payload["envelope"]["summary"] = "a summary nobody committed"
    record_path.write_bytes(orjson.dumps(payload))

    report = replay_native_wal(runtime_root / "wal")

    assert report.replayed_event_count == 0
    assert _poison_reasons(context.wal_dir) == [wal.WAL_DIGEST_MISMATCH_REASON]
    assert _firehose_rows(canary) == []


def test_a_record_naming_another_tree_is_poisoned(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The namespace a record sits in must be the one its document belongs to.

    A root is named by a digest of its resolved path, so a tree that was
    moved leaves records in a namespace its new id no longer matches. The
    record is intact -- its digest recomputes clean -- and is still
    refused, because the replay cannot say whose firehose it belongs to.
    """
    _tear(context, monkeypatch, target="append_json_line")
    record_path = list_records(context.wal_dir)[0]
    body = read_record(record_path).model_dump(mode="json")
    body["state_path"] = str(tmp_path / "elsewhere" / "generations" / "gen-1" / "state.json")
    body["digest"] = None
    moved = wal.WalRecord.model_validate(body)
    record_path.write_bytes(orjson.dumps(moved.model_dump(mode="json")))
    assert wal.verify_record_digest(moved)

    report = replay_native_wal(runtime_root / "wal")

    assert report.replayed_event_count == 0
    assert _poison_reasons(context.wal_dir) == [REASON_ROOT_UNRESOLVED]
    assert _firehose_rows(canary) == []


def test_a_pending_record_over_a_diverged_document_is_poisoned(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither digest matching means no replay can say what the crash left."""
    _tear(context, monkeypatch, target="mark_applied")
    path = document_path(canary)
    document = read_document(path)
    document["milestone"]["MLS-0030"]["title"] = "moved by something else"
    write_document(path, document)

    report = replay_native_wal(runtime_root / "wal")

    assert (report.abandoned_count, report.completed_count) == (0, 0)
    assert _poison_reasons(context.wal_dir) == [REASON_DOCUMENT_DIVERGED]
    assert _firehose_rows(canary) == []


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(b"{truncated", id="not-json"),
        pytest.param(b'{"record_id": "x"}', id="json-that-is-not-a-record"),
    ],
)
def test_an_unreadable_record_is_poisoned(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: bytes,
) -> None:
    """Bytes no record can be read out of are moved aside, never acted on."""
    _tear(context, monkeypatch, target="append_json_line")
    list_records(context.wal_dir)[0].write_bytes(corrupt)

    report = replay_native_wal(runtime_root / "wal")

    assert report.replayed_event_count == 0
    poisoned = list_poisoned(context.wal_dir)
    assert len(poisoned) == 1
    assert poisoned[0].read_bytes() == corrupt
    assert _firehose_rows(canary) == []


def test_a_degenerate_file_name_is_moved_aside_without_crashing(
    context: Epoch2RootContext, runtime_root: Path
) -> None:
    context.wal_dir.mkdir(parents=True, exist_ok=True)
    (context.wal_dir / "nostatus.pending.json").write_bytes(b"{}")
    (context.wal_dir / "bare.json").write_bytes(b"{}")

    report = replay_native_wal(runtime_root / "wal")

    assert report.root_count == 1
    assert report.replayed_event_count == 0


# ---------------------------------------------------------------------------
# The replay runs at daemon start
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_daemon_start_finishes_a_torn_native_transaction(
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot prologue replays the native WAL before the listener binds."""
    context = root_context(canary, runtime_root)
    monkeypatch.setattr(transaction, "mark_applied", _explode)
    with pytest.raises(_CrashError):
        run_transaction(context=context, request=_request(), now=AT)
    monkeypatch.undo()
    assert _firehose_rows(canary) == []

    project = tmp_path / "project" / ".ea"
    project.mkdir(parents=True)
    (project / "state.json").write_bytes(orjson.dumps({"placeholder": True}))
    monkeypatch.setenv("EA_STATE", str(project / "state.json"))
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: runtime_root)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: runtime_root / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: runtime_root / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: runtime_root / "eawfd.sock")
    monkeypatch.setattr(
        daemon_main.asyncio, "run", lambda coro: coro.close() if hasattr(coro, "close") else None
    )

    assert daemon_main.run(foreground=True) == 0

    rows = _firehose_rows(canary)
    assert len(rows) == 1
    assert rows[0]["payload"]["name"] == "domain.milestone.activated"
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]


# ---------------------------------------------------------------------------
# A projection that refuses the publish does not unmake the commit
# ---------------------------------------------------------------------------


def test_publish_projection_reports_a_bus_that_took_the_envelope(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)
    taken: list[Any] = []

    assert (
        publish_projection(
            type("_Bus", (), {"publish": staticmethod(taken.append)})(), committed.envelope
        )
        is True
    )
    assert len(taken) == 1


def test_publish_projection_reports_a_bus_that_raised(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    assert (
        publish_projection(
            type("_Bus", (), {"publish": staticmethod(_explode)})(), committed.envelope
        )
        is False
    )


def test_publish_projection_treats_an_absent_bus_as_nothing_to_lose(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    assert publish_projection(None, committed.envelope) is True


def test_rpc_returns_the_committed_receipt_flagged_projection_degraded(
    canary: CanaryProvision, runtime_root: Path
) -> None:
    ctx = method_context(runtime_root)
    ctx.bus = type("_Bus", (), {"publish": staticmethod(_explode)})()

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["status"] == "ok"
    assert answer["errors"] == []
    assert answer["warnings"] == [PROJECTION_DEGRADED]
    assert answer["result"]["event_name"] == "domain.milestone.activated"
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)


def test_a_degraded_publish_leaves_the_commit_durable(
    canary: CanaryProvision, runtime_root: Path
) -> None:
    """The document, the WAL record and the firehose row all stand."""
    ctx = method_context(runtime_root)
    ctx.bus = type("_Bus", (), {"publish": staticmethod(_explode)})()

    asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    document = read_document(document_path(canary))
    wal_dir = ctx.native_root_context(canary.root / ".ea").wal_dir
    assert document["milestone"]["MLS-0030"]["revision"] == 2
    assert _statuses(wal_dir) == [WalStatus.FSYNCED.value]
    assert len(_firehose_rows(canary)) == 1


def test_the_document_digest_the_wal_records_matches_the_document_on_disk(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The replay reads ``after_state_version`` off disk, so the two must agree."""
    run_transaction(context=context, request=_request(), now=AT)

    record = read_record(list_records(context.wal_dir)[0])
    assert record.after_state_version == state_version(read_document(document_path(canary)))
