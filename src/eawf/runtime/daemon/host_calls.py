"""The gateway's door for a tool call the host harness made and ran itself.

A Claude Code session calls its own tools, so the daemon never answers them. It can
still account for them the way it accounts for a brokered call: the host's hooks report
each call as it is requested and again once it has run, and this module gives it a real
gateway identity -- a :data:`~eawf.kernel.runtime.semantic.CallId` -- and, once it has
run, a receipt whose result names the call's bounded, scrubbed output as a stored
artifact. Nothing is executed here: the receipt records an answer the host already gave.

The call id is derived from the Run and the host's own id of the call, so the report
before the call and the report after it name the same call without the daemon keeping
anything between them, and a retried hook restates what is already standing: the lines
it would append are named by the call and the phase, and a second result is answered
with the receipt the first one earned.

The hook waits on its answer, and many hooks report at once, so a report is queued and
answered at once; one drainer per root lands every queued report in one session and one
document write. A call's receipt is found through an index of the receipt ledger keyed by
call id, kept in process memory and extended by the lines appended since it was read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal, Self

from pydantic import model_validator

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.events import ToolPayload
from eawf.kernel.runtime.provider import RuntimeRecord, ToolCapabilityId
from eawf.kernel.runtime.semantic import (
    TOOL_SCHEMA_VERSION,
    CallId,
    ReceiptId,
    SemanticResult,
    SemanticToolErrorCode,
    error_for,
)
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerSnapshotError,
    compose_ledger_content,
    ledger_snapshot,
    read_collection_records,
    snapshot_digest_mismatch,
    split_ledger_lines,
)
from eawf.kernel.store.paths import collection_ledger_paths
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import (
    Epoch2RootContext,
    RootSession,
    canonical_entity_urn,
    session_subjects,
)
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append, ledger_append_batch
from eawf.runtime.daemon.tool_events import state_tool_phase
from eawf.runtime.lock.portalock import LockTimeout

logger = logging.getLogger(__name__)

#: The discriminator of a host call receipt on the receipt ledger.
HOST_RECEIPT_PAYLOAD_KIND: Final = "host_call_receipt"

#: How many hex characters of a digest the minted ids carry.
_ID_HEX: Final = 16

#: The code a host call that ran and failed ends with: a changed request is what
#: would get past it.
HOST_FAILURE_CODE: Final = SemanticToolErrorCode.PAYLOAD_INVALID


class HostCallReceipt(RuntimeRecord):
    """The durable record of what one host tool call was answered with.

    Attributes:
        payload_kind: The discriminator separating it from a brokered receipt.
        schema_version: Version of this record's shape.
        receipt_id: The receipt's own identifier.
        call_id: The call it answers.
        run_ref: The Run the call was made in.
        tool_id: The host tool, in the tool-id grammar.
        result: The answer: the output artifact on success, the error with the
            output as its diagnostic on failure.
        recorded_at: When the daemon filed it.
    """

    payload_kind: Literal["host_call_receipt"] = HOST_RECEIPT_PAYLOAD_KIND
    schema_version: Literal["host-call-receipt/v1"] = "host-call-receipt/v1"
    receipt_id: ReceiptId
    call_id: CallId
    run_ref: RunUrn
    tool_id: ToolCapabilityId
    result: SemanticResult
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _receipt_answers_the_call_it_names(self) -> Self:
        """Refuse a receipt whose result answers another call.

        Raises:
            ValueError: The result's call, Run or receipt differs from the receipt's.
        """
        result = self.result
        if (result.call_id, result.receipt_id) != (self.call_id, self.receipt_id):
            raise ValueError("the result does not answer the receipt's call")
        if result.run_ref != self.run_ref:
            raise ValueError("result.run_ref does not match the receipt's run_ref")
        return self


def host_tool_id(tool_name: str) -> str:
    """Return a host tool's name in the tool-id grammar: lower snake case, letter first."""
    slug = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return (slug if slug[:1].isalpha() else f"tool_{slug}")[:64]


def host_call_id(run_ref: QualifiedUrn, host_call_key: str) -> str:
    """Return the call id one host call is known by within its Run."""
    body = f"{canonical_entity_urn(run_ref)}:{host_call_key}"
    return f"call-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:_ID_HEX]}"


def host_receipts(session: RootSession, run_ref: str | QualifiedUrn) -> tuple[HostCallReceipt, ...]:
    """Return one Run's host call receipts, in ledger order.

    Only the lines naming the asked Run, or naming no Run as a string, are
    validated, so a corrupt receipt of another Run is not reported here.

    Raises:
        ValidationError: A host call receipt of the asked Run, or one with no
            string ``run_ref``, does not validate, so the ledger is corrupt.
        IdentityError: A receipt's ``run_ref`` is not a qualified URN.
    """
    wanted = canonical_entity_urn(run_ref)
    # The ledger holds every Run's receipts and is read under the document
    # lock, so validating the other Runs' lines would hold it for each of them.
    return tuple(
        HostCallReceipt.model_validate(item.payload)
        for item in read_collection_records(session.document_path, Epoch2Collection.RECEIPT)
        if item.payload.get("payload_kind") == HOST_RECEIPT_PAYLOAD_KIND
        and _names_run(item.payload.get("run_ref"), wanted)
    )


def _call_receipt(
    session: RootSession, run_ref: QualifiedUrn, call_id: str
) -> HostCallReceipt | None:
    """Return the receipt one host call already earned, ``None`` when it has none.

    The receipt ledger is read through an index keyed by call id, and only the
    lines carrying the call's id are validated: the ledger holds every Run's
    receipts and is read under the document lock.

    Raises:
        ValidationError: A host call receipt carrying the call's id does not
            validate, so the ledger is corrupt.
        IdentityError: Such a receipt's ``run_ref`` is not a qualified URN.
        LedgerTornTailError: A receipt ledger ends mid-line.
        LedgerSnapshotError: A receipt ledger's snapshot does not match its digest.
    """
    wanted = canonical_entity_urn(run_ref)
    for path in collection_ledger_paths(session.document_path, Epoch2Collection.RECEIPT):
        for line in _indexed_receipt_lines(path).get(call_id, ()):
            payload = LedgerRecord.model_validate_json(line).payload
            if _names_run(payload.get("run_ref"), wanted):
                return HostCallReceipt.model_validate(payload)
    return None


@dataclass(frozen=True, slots=True)
class _ReceiptIndex:
    """What one read of a receipt ledger left behind for the next.

    Attributes:
        snapshot: The sealed snapshot's name, size and modification time,
            ``None`` when the ledger had none.
        inode: The live file's (dev, inode).
        offset: Bytes of the live file read, always ending on a newline.
        head: The live file's first line read, with its newline.
        tail: The live file's last line read, with its newline.
        lines: The host call receipt lines read, keyed by the call id they
            carry, each call's in ledger order.
    """

    snapshot: tuple[str, int, int] | None
    inode: tuple[int, int]
    offset: int
    head: bytes
    tail: bytes
    lines: Mapping[str, tuple[str, ...]]


#: How many receipt ledgers keep an index: a committed and a local one per tree.
_RECEIPT_INDEX_CAPACITY: Final = 8

#: Each receipt ledger's index, keyed by its live path, least recently read first.
#: An entry is replaced whole, never mutated, so two readers at once at worst
#: both rebuild; the lock only keeps the order and the bound whole.
_RECEIPT_INDEXES: OrderedDict[Path, _ReceiptIndex] = OrderedDict()
_RECEIPT_INDEXES_LOCK: Final = threading.Lock()


def _indexed_receipt_lines(path: Path) -> Mapping[str, tuple[str, ...]]:
    """Return the host call receipt lines of the ledger at *path*, keyed by call id.

    A ledger only grows, and every sanctioned rewrite replaces the file through a
    rename, so the index of the last read is extended by the bytes appended since
    while the live file is the same inode, at least as long, and still holds the
    first and last line read at their places, and the snapshot is unchanged;
    anything else rebuilds it from the whole ledger.

    Args:
        path: The live ledger file. A missing file reads as empty.

    Returns:
        Each call id's lines, in ledger order.

    Raises:
        LedgerTornTailError: The file ends mid-line; the index stays where it
            was, so the next read retries the tail.
        LedgerSnapshotError: The snapshot does not match its digest.
    """
    snapshot = ledger_snapshot(path)
    stamp = None
    if snapshot is not None:
        sealed = snapshot.stat()
        stamp = (snapshot.name, sealed.st_size, sealed.st_mtime_ns)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        with _RECEIPT_INDEXES_LOCK:
            _RECEIPT_INDEXES.pop(path, None)
        return _whole_index(path, snapshot, stamp, inode=(-1, -1), live=b"").lines
    with handle:
        stat = os.fstat(handle.fileno())
        inode = (stat.st_dev, stat.st_ino)
        with _RECEIPT_INDEXES_LOCK:
            cached = _RECEIPT_INDEXES.get(path)
        if cached is not None and cached.snapshot == stamp and cached.inode == inode:
            appended = _appended(handle, cached, size=stat.st_size)
            if appended is not None:
                return _keep(path, _extended(cached, appended)).lines
        handle.seek(0)
        live = handle.read()
    return _keep(path, _whole_index(path, snapshot, stamp, inode=inode, live=live)).lines


def _keep(path: Path, index: _ReceiptIndex) -> _ReceiptIndex:
    """Keep *index* as the most recently read, dropping the gone and the least recent."""
    with _RECEIPT_INDEXES_LOCK:
        _RECEIPT_INDEXES[path] = index
        _RECEIPT_INDEXES.move_to_end(path)
        for gone in [held for held in _RECEIPT_INDEXES if held != path and not held.exists()]:
            del _RECEIPT_INDEXES[gone]
        while len(_RECEIPT_INDEXES) > _RECEIPT_INDEX_CAPACITY:
            _RECEIPT_INDEXES.popitem(last=False)
    return index


def _appended(handle: BinaryIO, cached: _ReceiptIndex, *, size: int) -> bytes | None:
    """Return the live bytes past *cached*'s offset, ``None`` when the prefix it read moved."""
    if size < cached.offset:
        return None
    handle.seek(0)
    if handle.read(len(cached.head)) != cached.head:
        return None
    handle.seek(cached.offset - len(cached.tail))
    if handle.read(len(cached.tail)) != cached.tail:
        return None
    return handle.read()


def _extended(cached: _ReceiptIndex, appended: bytes) -> _ReceiptIndex:
    """Return *cached* grown by the lines *appended* past its offset.

    Raises:
        LedgerTornTailError: *appended* ends mid-line.
    """
    if not appended:
        return cached
    lines = split_ledger_lines(appended)
    return _ReceiptIndex(
        snapshot=cached.snapshot,
        inode=cached.inode,
        offset=cached.offset + len(appended),
        head=cached.head or f"{lines[0]}\n".encode(),
        tail=f"{lines[-1]}\n".encode(),
        lines=_by_call(lines, into=cached.lines),
    )


def _whole_index(
    path: Path,
    snapshot: Path | None,
    stamp: tuple[str, int, int] | None,
    *,
    inode: tuple[int, int],
    live: bytes,
) -> _ReceiptIndex:
    """Index the whole ledger, its snapshot first.

    Raises:
        LedgerTornTailError: The live bytes end mid-line.
        LedgerSnapshotError: The snapshot does not match its digest.
    """
    content = live
    if snapshot is not None:
        sealed = snapshot.read_bytes()
        reason = snapshot_digest_mismatch(snapshot.name, sealed)
        if reason is not None:
            raise LedgerSnapshotError(f"{reason}; refusing to read ledger {path.name}")
        content = compose_ledger_content(sealed, live)
    live_lines = live.split(b"\n")[:-1]
    return _ReceiptIndex(
        snapshot=stamp,
        inode=inode,
        offset=len(live),
        head=live_lines[0] + b"\n" if live_lines else b"",
        tail=live_lines[-1] + b"\n" if live_lines else b"",
        lines=_by_call(split_ledger_lines(content), into={}),
    )


def _by_call(
    lines: tuple[str, ...], *, into: Mapping[str, tuple[str, ...]]
) -> Mapping[str, tuple[str, ...]]:
    """Return *into* with each host call receipt line of *lines* added under its call id.

    A line is decoded once, here, to learn its call; it is validated only when its
    call is looked up. A line that does not decode, or is not a host call receipt
    naming a call id, is never a receipt the lookup would return, so it is left out.
    """
    grouped = dict(into)
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        payload = raw.get("payload") if isinstance(raw, dict) else None
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if payload.get("payload_kind") == HOST_RECEIPT_PAYLOAD_KIND and isinstance(call_id, str):
            grouped[call_id] = (*grouped.get(call_id, ()), line)
    return grouped


def _names_run(run_ref: object, wanted: str) -> bool:
    """Return whether a stored receipt's raw ``run_ref`` names the Run ``wanted``.

    A value that is not a string is let through, so validating the line
    reports the corrupt receipt instead of skipping it.

    Raises:
        IdentityError: The string is not a qualified URN.
    """
    return not isinstance(run_ref, str) or canonical_entity_urn(run_ref) == wanted


@dataclass(frozen=True, slots=True)
class HostCallObservation:
    """One report of a host tool call, waiting to be accounted on its Run.

    Attributes:
        run_ref: The Run the host session runs as.
        tool_name: The host's name for the tool.
        host_call_key: The host's own id of the call.
        output: What the call returned, raw; ``None`` while it has not run. It is
            bounded and scrubbed before it is stored.
        failed: Whether the call that ran failed.
        actor: The principal the host's reports are attributed to.
        now: The daemon's recording clock when the report arrived.
    """

    run_ref: QualifiedUrn
    tool_name: str
    host_call_key: str
    output: str | None
    failed: bool
    actor: str
    now: datetime

    @property
    def call_id(self) -> str:
        """Return the gateway call id the call is known by."""
        return host_call_id(self.run_ref, self.host_call_key)


#: What a drainer leaves for one observation: its receipt once the call has run.
HostCallLanding = Future[HostCallReceipt | None]

#: How long a stopping daemon keeps trying to land calls queued behind a held
#: document lock. A wrapped commit holds that lock for its whole hook run, so a
#: stop can begin mid-commit; past this the calls are logged as lost. One lock
#: wait may be under way when the stop begins and one more may start just
#: before the grace ends, so the drainer ends within this plus two lock waits
#: (``EA_LOCK_TIMEOUT``, 5 s by default): 25 s, inside the 30 s a default
#: ``eawf daemon stop`` waits for the daemon to exit.
HOST_CALL_STOP_GRACE_SECONDS: Final = 15.0


class HostCallQueue:
    """The host call observations of one root, waiting to land together.

    A report is queued and its caller answered at once. One drainer at a time
    takes every observation queued by the time it holds the document lock, lands
    them in that one session and one document write, then takes what arrived
    meanwhile, and ends only once the queue is empty: a drainer the daemon awaits
    before it exits leaves nothing queued behind. A batch that finds the lock
    held past the lock timeout stays queued and is tried again, in order and
    joined by later arrivals, until it lands; once the daemon is stopping it is
    tried for :data:`HOST_CALL_STOP_GRACE_SECONDS` at most.
    """

    def __init__(self, context: Epoch2RootContext, *, stopping: Callable[[], bool]) -> None:
        """Bind an empty queue to the root it lands on.

        Args:
            context: The native context of the root the observations belong to.
            stopping: Whether the daemon has begun to stop; read by the drainer's
                thread before each try.
        """
        self._context = context
        self._stopping = stopping
        self._guard = threading.Lock()
        self._waiting: list[tuple[HostCallObservation, HostCallLanding]] = []
        self._draining = False

    def put(
        self, observation: HostCallObservation, *, start: Callable[[Callable[[], None]], object]
    ) -> HostCallLanding:
        """Queue *observation*, starting a drainer when none runs.

        Args:
            observation: The report to account for.
            start: Runs the drainer it is given, on a thread the daemon awaits
                before it exits; called only when no drainer runs.

        Returns:
            The landing, resolved once the observation's batch is on disk: its
            receipt, ``None`` before the call has run, or the error that kept
            it from landing.
        """
        landing: HostCallLanding = Future()
        with self._guard:
            self._waiting.append((observation, landing))
            idle, self._draining = not self._draining, True
        if idle:
            start(self.drain)
        return landing

    def drain(self) -> None:
        """Land queued observations, a batch per document write, until none is left.

        A batch locked out by another holder of the document lock is tried again
        at once: each try already waits out the lock timeout, so the retries do
        not spin. Once the daemon is stopping, the queue is given up as lost
        after :data:`HOST_CALL_STOP_GRACE_SECONDS`.
        """
        stop_seen: float | None = None
        while True:
            with self._guard:
                if not self._waiting:
                    self._draining = False
                    return
                first = self._waiting[0][0].run_ref
            if stop_seen is None and self._stopping():
                stop_seen = time.monotonic()
            if stop_seen is not None and time.monotonic() - stop_seen >= (
                HOST_CALL_STOP_GRACE_SECONDS
            ):
                self._give_up()
                continue
            self._land(first)

    def _give_up(self) -> None:
        """Fail every queued observation as lost to a lock held through the stop."""
        lost = self._take()
        error = LockTimeout(
            f"the document lock stayed held through the daemon's stop; "
            f"{len(lost)} host calls were not recorded"
        )
        calls = ",".join(observation.call_id for observation, _ in lost)
        logger.error(
            f"HostCallQueue lost root={self._context.identity.root_id} count={len(lost)} "
            f"calls={calls}"
        )
        for _, landing in lost:
            landing.set_exception(error)

    def _take(self) -> list[tuple[HostCallObservation, HostCallLanding]]:
        """Return every observation queued now, leaving the queue empty."""
        with self._guard:
            taken, self._waiting = self._waiting, []
        return taken

    def _land(self, first: QualifiedUrn) -> None:
        """Land every observation queued once the document lock is held, in one write.

        A batch that finds the document lock held past the lock timeout takes
        nothing and writes nothing, so it is left queued for the next try. A batch
        that cannot open its session for another reason, or cannot write its
        document, fails every observation it holds, so a fenced root never keeps
        the drainer turning.

        Args:
            first: The Run the session is opened about; each observation is
                filed against its own Run.
        """
        taken: list[tuple[HostCallObservation, HostCallLanding]] = []
        outcomes: list[HostCallReceipt | None | Exception] = []
        try:
            with self._context.session([first]) as session, ledger_append_batch(session):
                taken = self._take()
                outcomes = [_outcome(session, observation) for observation, _ in taken]
        except Exception as error:
            if isinstance(error, LockTimeout) and not taken:
                with self._guard:
                    waiting = len(self._waiting)
                logger.warning(
                    f"HostCallQueue lock busy root={self._context.identity.root_id} "
                    f"queued={waiting} retrying"
                )
                return
            taken = taken or self._take()
            logger.error(
                f"HostCallQueue batch failed root={self._context.identity.root_id} "
                f"calls={len(taken)} error={error!r}"
            )
            for _, landing in taken:
                landing.set_exception(error)
            return
        logger.info(
            f"HostCallQueue batch landed root={self._context.identity.root_id} calls={len(taken)}"
        )
        for (_, landing), outcome in zip(taken, outcomes, strict=True):
            if isinstance(outcome, Exception):
                landing.set_exception(outcome)
            else:
                landing.set_result(outcome)


def _outcome(
    session: RootSession, observation: HostCallObservation
) -> HostCallReceipt | None | Exception:
    """Account for *observation*, returning the error that refused it instead of raising.

    One refused observation, a corrupt receipt of its call or a Run that ended,
    must not cost the others in its batch their landing.
    """
    try:
        return _account(session, observation)
    except Exception as error:
        logger.error(
            f"observe_host_call refused call={observation.call_id} run={observation.run_ref} "
            f"error={error!r}"
        )
        return error


def _account(session: RootSession, observation: HostCallObservation) -> HostCallReceipt | None:
    """Account for one host tool call on its Run's transcript, and receipt it once run.

    Args:
        session: The open session, inside a ledger append batch.
        observation: The report to account for.

    Returns:
        The receipt once the call has run, ``None`` before.
    """
    run_ref, now = observation.run_ref, observation.now
    call_id = observation.call_id
    tool_id = host_tool_id(observation.tool_name)
    # The lock is the whole document's, so only the subject each ledger line
    # is filed against follows the observation's own Run.
    session.locked_urns = session_subjects([run_ref])

    def state(payload: ToolPayload) -> None:
        state_tool_phase(
            session,
            run_ref=run_ref,
            payload=payload,
            provenance="provider_native",
            actor=observation.actor,
            now=now,
        )

    state(ToolPayload(call_ref=call_id, tool_id=tool_id, phase="requested"))
    if observation.output is None:
        return None
    receipt = _call_receipt(session, run_ref, call_id)
    if receipt is None:
        receipt = _receipt(
            session,
            run_ref=run_ref,
            call_id=call_id,
            tool_id=tool_id,
            output=observation.output,
            failed=observation.failed,
            now=now,
        )
    state(ToolPayload(call_ref=call_id, tool_id=tool_id, phase="accepted"))
    error = receipt.result.error
    state(
        ToolPayload(
            call_ref=call_id,
            tool_id=tool_id,
            phase="result",
            result_ref=receipt.receipt_id if error is None else None,
            error_code=None if error is None else error.code,
        )
    )
    logger.info(f"observe_host_call tool={tool_id} call={call_id} status={receipt.result.status}")
    return receipt


def _receipt(
    session: RootSession,
    *,
    run_ref: QualifiedUrn,
    call_id: str,
    tool_id: str,
    output: str,
    failed: bool,
    now: datetime,
) -> HostCallReceipt:
    """File the call's output as an artifact and the receipt that names it."""
    artifact = file_content(session, run_ref=run_ref, text=output, now=now)
    receipt_id = f"receipt-{secrets.token_hex(_ID_HEX // 2)}"
    result = SemanticResult(
        call_id=call_id,
        run_ref=run_ref,
        receipt_id=receipt_id,
        status="failed" if failed else "succeeded",
        result_schema_version=TOOL_SCHEMA_VERSION,
        output_ref=None if failed else artifact,
        error=(
            error_for(
                HOST_FAILURE_CODE,
                message=f"the host's {tool_id} call ran and failed",
                diagnostic_ref=artifact,
            )
            if failed
            else None
        ),
        completed_at=now,
    )
    receipt = HostCallReceipt(
        receipt_id=receipt_id,
        call_id=call_id,
        run_ref=run_ref,
        tool_id=tool_id,
        result=result,
        recorded_at=now,
    )
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key=call_id,
            status=result.status,
            recorded_at=now,
            payload=receipt.model_dump(mode="json"),
        ),
    )
    return receipt


__all__ = [
    "HOST_FAILURE_CODE",
    "HOST_RECEIPT_PAYLOAD_KIND",
    "HostCallLanding",
    "HostCallObservation",
    "HostCallQueue",
    "HostCallReceipt",
    "host_call_id",
    "host_receipts",
    "host_tool_id",
]
