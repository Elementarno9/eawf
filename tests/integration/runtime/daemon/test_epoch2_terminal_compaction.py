"""A terminal transition moves its record out of the document, or not at all.

Compaction destroys data, so nothing here settles for counting rows. The
line the ledger receives is compared against the record the pure reducer
says the transition produces, field for field, so "the record moved" is
only accepted once the bytes that moved are shown to be the bytes that
were owed.

The torn state is made by killing the real compaction between its two
durable writes -- the ledger append and the document rewrite -- rather
than by hand-writing a ledger. The step is replaced with one that raises,
the production transaction is run, and whatever it leaves on disk is what
the daemon's boot is handed. That is the only way to know the tree the
recovery reads is the tree the commit path writes.

The ordering the whole thing rests on is that the ledger is appended
first. A kill after the append leaves the record in two places and
recovery finishes the move by dropping the document row; a kill before it
leaves the record only in the document, which is where the transaction
found it. Neither loses the record, and that asymmetry is why the
destructive write is second.
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

from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.store import compaction
from eawf.kernel.store.compaction import (
    RecordInTwoPlacesError,
    RecordLocation,
    locate_record,
    read_document,
)
from eawf.kernel.store.ledger import line_digest, read_ledger_records, render_ledger_line
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.wal import WalStatus, list_records
from eawf.workflow.lifecycle.epoch2 import GuardContext, TransitionAccepted, apply_transition
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
KEY = "req-0001"
REASON = "operator-cancelled"
MILESTONE_KEY = "MLS-0030"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"

_BOOT_SKIP_REASON = "daemon boot path is POSIX-only in this suite"


class _CrashError(RuntimeError):
    """Stands in for the kill signal a torn compaction really takes."""


def _explode(*_args: object, **_kwargs: object) -> None:
    raise _CrashError("simulated kill")


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise OSError("the ledger file system refused the write")


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
    seed(provisioned, {"milestone": {MILESTONE_KEY: seed_row("milestone", "PLANNED")}})
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
        "to_status": "CANCELLED",
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
        "reason_code": REASON,
    }
    payload.update(overrides)
    return TransitionRequest.model_validate(payload)


def _milestone_ledger(canary: CanaryProvision) -> Path:
    return ledger_path(document_path(canary), Epoch2Collection.MILESTONE)


def _ledger_lines(canary: CanaryProvision, collection: Epoch2Collection) -> list[Any]:
    path = ledger_path(document_path(canary), collection)
    return list(read_ledger_records(path))


def _rows(canary: CanaryProvision, collection: Epoch2Collection) -> dict[str, Any]:
    document = read_document(document_path(canary))
    rows = document.get(collection.value, {})
    assert isinstance(rows, dict)
    return rows


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _statuses(wal_dir: Path) -> list[str]:
    return sorted(path.name.split(".")[-2] for path in list_records(wal_dir))


def _expected_successor() -> dict[str, Any]:
    """Return the record the pure reducer says this transition produces."""
    outcome = apply_transition(
        Milestone.model_validate(seed_row("milestone", "PLANNED")),
        to=MilestoneStatus.CANCELLED,
        at=AT,
        ctx=GuardContext(observations=frozenset()),
        updates={},
    )
    assert isinstance(outcome, TransitionAccepted)
    return outcome.record.model_dump(mode="json")


def _drive_cancel(canary: CanaryProvision, runtime_root: Path, *, key: str = KEY) -> dict[str, Any]:
    """Cancel the seeded Milestone through its registered lifecycle verb."""
    return asyncio.run(
        methods.dispatch(
            "domain.milestone.cancel",
            method_context(runtime_root),
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "expected_revision": 1,
                "idempotency_key": key,
                "actor": ACTOR,
                "reason_code": REASON,
            },
        )
    )


def _boot(runtime_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run the daemon's boot prologue with its listener stubbed out."""
    project = tmp_path / "project" / ".ea"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.json").write_bytes(orjson.dumps({"placeholder": True}))
    monkeypatch.setenv("EA_STATE", str(project / "state.json"))
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: runtime_root)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: runtime_root / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: runtime_root / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: runtime_root / "eawfd.sock")
    monkeypatch.setattr(
        daemon_main.asyncio, "run", lambda coro: coro.close() if hasattr(coro, "close") else None
    )
    return daemon_main.run(foreground=True)


# ---------------------------------------------------------------------------
# The move a terminal transition makes
# ---------------------------------------------------------------------------


def test_a_terminal_transition_moves_the_record_into_its_ledger(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)

    assert _rows(canary, Epoch2Collection.MILESTONE) == {}
    assert [line.record_key for line in _ledger_lines(canary, Epoch2Collection.MILESTONE)] == [
        MILESTONE_KEY
    ]
    assert (
        locate_record(
            document_path(canary),
            collection=Epoch2Collection.MILESTONE,
            record_key=MILESTONE_KEY,
        )
        is RecordLocation.LEDGER
    )


def test_the_registered_verb_compacts_through_the_same_transaction(
    canary: CanaryProvision, runtime_root: Path
) -> None:
    """The production surface, not the transaction called directly."""
    answer = _drive_cancel(canary, runtime_root)

    assert answer["status"] == "ok"
    assert answer["result"]["event_name"] == "domain.milestone.cancelled"
    assert _rows(canary, Epoch2Collection.MILESTONE) == {}
    assert len(_ledger_lines(canary, Epoch2Collection.MILESTONE)) == 1


def test_the_compacted_line_carries_the_successor_field_for_field(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """What the ledger holds is exactly what the reducer produced."""
    run_transaction(context=context, request=_request(), now=AT)

    line = _ledger_lines(canary, Epoch2Collection.MILESTONE)[0]
    assert line.payload == _expected_successor()
    assert (line.status, line.record_key) == ("CANCELLED", MILESTONE_KEY)
    assert line.recorded_at == AT
    assert line.supersedes is None


def test_no_field_of_the_stored_record_is_dropped_by_the_move(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Everything the transition did not move survives the compaction.

    The oracle is the record as the document stored it, not the raw seed:
    a document row is a model dump, so a field the model defaults is part
    of the stored bytes and has to come through the move as well.
    """
    stored = Milestone.model_validate(seed_row("milestone", "PLANNED")).model_dump(mode="json")
    run_transaction(context=context, request=_request(), now=AT)

    payload = _ledger_lines(canary, Epoch2Collection.MILESTONE)[0].payload
    assert set(payload) == set(stored)
    moved = {"status", "revision", "updated_at"}
    assert {key: value for key, value in payload.items() if key not in moved} == {
        key: value for key, value in stored.items() if key not in moved
    }
    assert (payload["status"], payload["revision"]) == ("CANCELLED", 2)


def test_the_move_leaves_the_high_water_mark_alone(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The sequence counter is a top-level key, not a row the move touches."""
    committed = run_transaction(context=context, request=_request(), now=AT)

    document = read_document(document_path(canary))
    assert document[CANONICAL_SEQUENCE_KEY] == committed.receipt.canonical_sequence == 1


def test_a_nonterminal_transition_leaves_the_record_in_the_document(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Work in flight is read and rewritten; only a finished record moves."""
    run_transaction(context=context, request=_request(to_status="ACTIVE"), now=AT)

    assert list(_rows(canary, Epoch2Collection.MILESTONE)) == [MILESTONE_KEY]
    assert not _milestone_ledger(canary).exists()


def test_a_retired_track_stays_in_the_document(
    tmp_path: Path, runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETIRED is terminal, but a Track is declared at the document tier."""
    scratch = tmp_path / "trackscratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    canary = provision(tmp_path / "trackrepo", code="TRACK")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    context = root_context(canary, runtime_root)

    run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": TRACK_URN,
                "to_status": "RETIRED",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            }
        ),
        now=AT,
    )

    assert list(_rows(canary, Epoch2Collection.TRACK)) == ["TRK-RUNTIME"]
    assert not (document_path(canary).parent / "ledger").exists()


def test_a_retry_of_a_compacted_transition_replays_its_receipt(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The row is gone, so only the receipt can answer the retry."""
    run_transaction(context=context, request=_request(), now=AT)

    replayed = run_transaction(context=context, request=_request(), now=AT)

    assert replayed.replayed is True
    assert replayed.envelope is None
    assert replayed.receipt.canonical_sequence == 1
    assert len(_firehose_rows(canary)) == 1
    assert len(_ledger_lines(canary, Epoch2Collection.MILESTONE)) == 1


def test_a_compaction_that_cannot_finish_leaves_the_mutation_committed(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move is housekeeping; the transition it follows is durable."""
    monkeypatch.setattr("eawf.runtime.daemon.epoch2_transaction.compact_terminal_record", _refuse)

    committed = run_transaction(context=context, request=_request(), now=AT)

    assert committed.receipt.event_name == "domain.milestone.cancelled"
    assert _rows(canary, Epoch2Collection.MILESTONE)[MILESTONE_KEY]["status"] == "CANCELLED"
    assert not _milestone_ledger(canary).exists()
    assert len(_firehose_rows(canary)) == 1
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]


# ---------------------------------------------------------------------------
# A kill between the ledger append and the document rewrite
# ---------------------------------------------------------------------------


def _tear(context: Epoch2RootContext, monkeypatch: pytest.MonkeyPatch, *, key: str = KEY) -> None:
    """Kill the real compaction after its ledger append, keeping the debris."""
    monkeypatch.setattr(compaction, "write_document", _explode)
    with pytest.raises(_CrashError):
        run_transaction(context=context, request=_request(idempotency_key=key), now=AT)
    monkeypatch.undo()


def test_a_kill_between_the_two_writes_leaves_the_record_in_both_places(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tear(context, monkeypatch)

    assert list(_rows(canary, Epoch2Collection.MILESTONE)) == [MILESTONE_KEY]
    assert len(_ledger_lines(canary, Epoch2Collection.MILESTONE)) == 1
    with pytest.raises(RecordInTwoPlacesError):
        locate_record(
            document_path(canary),
            collection=Epoch2Collection.MILESTONE,
            record_key=MILESTONE_KEY,
        )


def test_a_torn_compaction_still_left_the_mutation_durable(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is on disk before the destructive write is attempted."""
    _tear(context, monkeypatch)

    rows = _firehose_rows(canary)
    assert [row["payload"]["name"] for row in rows] == ["domain.milestone.cancelled"]
    assert _statuses(context.wal_dir) == [WalStatus.APPLIED.value]
    assert _rows(canary, Epoch2Collection.MILESTONE)[MILESTONE_KEY]["revision"] == 2


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_daemon_start_recovers_a_torn_compaction_to_one_canonical_location(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tear(context, monkeypatch)
    committed = render_ledger_line(_ledger_lines(canary, Epoch2Collection.MILESTONE)[0])

    assert _boot(runtime_root, tmp_path, monkeypatch) == 0

    assert (
        locate_record(
            document_path(canary),
            collection=Epoch2Collection.MILESTONE,
            record_key=MILESTONE_KEY,
        )
        is RecordLocation.LEDGER
    )
    assert _rows(canary, Epoch2Collection.MILESTONE) == {}
    assert render_ledger_line(_ledger_lines(canary, Epoch2Collection.MILESTONE)[0]) == committed
    assert len(_firehose_rows(canary)) == 1
    assert _statuses(context.wal_dir) == [WalStatus.FSYNCED.value]


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_the_recovered_record_is_the_one_the_reducer_produced(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery drops a document row; it must drop nothing the ledger lacks."""
    _tear(context, monkeypatch)

    _boot(runtime_root, tmp_path, monkeypatch)

    assert _ledger_lines(canary, Epoch2Collection.MILESTONE)[0].payload == _expected_successor()


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_recovering_a_torn_compaction_is_idempotent_across_boots(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second boot must not re-append the line or re-drop the row."""
    _tear(context, monkeypatch)
    _boot(runtime_root, tmp_path, monkeypatch)
    document = document_path(canary).read_bytes()
    ledger = _milestone_ledger(canary).read_bytes()

    _boot(runtime_root, tmp_path, monkeypatch)

    assert document_path(canary).read_bytes() == document
    assert _milestone_ledger(canary).read_bytes() == ledger
    assert len(_firehose_rows(canary)) == 1


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_a_boot_over_an_untorn_tree_changes_nothing(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single boundary: a tree whose compaction already finished."""
    run_transaction(context=context, request=_request(), now=AT)
    document = document_path(canary).read_bytes()
    ledger = _milestone_ledger(canary).read_bytes()

    assert _boot(runtime_root, tmp_path, monkeypatch) == 0

    assert document_path(canary).read_bytes() == document
    assert _milestone_ledger(canary).read_bytes() == ledger


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_a_boot_with_no_native_tree_recovers_nothing(
    runtime_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty boundary: a daemon that has written to no epoch-2 tree."""
    assert _boot(runtime_root, tmp_path, monkeypatch) == 0


def test_a_torn_compaction_of_one_record_leaves_its_sibling_alone(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery drops only the rows the ledger names, not the collection."""
    sibling = rekeyed(seed_row("milestone", "PLANNED"), key="MLS-0031")
    seed(canary, {"milestone": {"MLS-0031": sibling}})
    _tear(context, monkeypatch)

    report = compaction.recover_store_tree(document_path(canary))

    assert report.document_rows_dropped == (f"{Epoch2Collection.MILESTONE.value}/{MILESTONE_KEY}",)
    assert list(_rows(canary, Epoch2Collection.MILESTONE)) == ["MLS-0031"]


def test_the_ledger_line_a_recovery_keeps_is_byte_identical(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The append-only tier forbids rewriting; recovery must not touch it."""
    _tear(context, monkeypatch)
    before = _milestone_ledger(canary).read_bytes()

    compaction.recover_store_tree(document_path(canary))

    after = _milestone_ledger(canary).read_bytes()
    assert after == before
    assert line_digest(
        render_ledger_line(_ledger_lines(canary, Epoch2Collection.MILESTONE)[0])
    ).startswith("sha256:")
