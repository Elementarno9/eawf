"""What makes a native mutation happen exactly once, retry or crash.

Two halves of one promise live here.

The first is the idempotency receipt. A client that never sees an answer
cannot tell a lost reply from a lost request, so it retries, and the
retry must not move the record a second time. Every committed mutation
therefore leaves a receipt keyed by the client's idempotency key inside
this root's namespace, carrying a digest of the exact parameters that
produced it. A retry whose parameters digest the same is answered with
the receipt it would have produced and writes nothing; a retry whose
parameters differ is refused, because one key naming two effects is a
client bug the daemon must surface rather than pick a winner for.

Receipts live in the root's local store. They describe one machine's
in-flight requests -- a clone has no retries of ours to answer -- and
keeping them out of the document means the promise costs no state
schema version.

The second half is the torn transaction. The commit writes four things
in a fixed order, and a process killed between any two of them leaves
the tree mid-stride. The WAL record is what makes the stride readable:
it carries the document digest before the mutation and the digest after,
so a replay can ask the document itself which side of the write the
crash fell on. A document still reading as ``before`` means the intent
never landed and is abandoned, exactly as the epoch-1 replay abandons a
pending record: the mutator is never re-executed, because re-running it
would re-derive fresh ids and a different answer. A document reading as
``after`` means the mutation is durable and only its tail is missing, so
the replay appends the firehose row if the log does not already hold that
envelope id, and marks the record durable.

A ledger-only mutation journals its ledger line inside the envelope and
writes the line only after the document, so a document that landed may
still be missing the line. The replay appends it before the firehose row
unless the ledger already holds that exact line, which keeps a line from
ever being written twice and keeps the row from naming a line that is not
there.

The envelope-id check is what makes the replay safe to run on every boot:
a row already in the log is never appended twice, so a torn transaction
recovers to exactly one event and a recovered one stays at one.

Native WAL records live one directory per root under the daemon's WAL, so
the epoch-1 replay -- which lists only the WAL's top level -- never sees
them, and this replay never reaches an epoch-1 record.

One thing the WAL cannot settle is the move a terminal record makes out
of the document and into its ledger. That move is two durable writes of
its own, taken after the mutation is durable, so a kill between them
leaves the record in both places at once. Its repair reads the tree
rather than the journal: a document row the ledger has already committed
is dropped, never the other way round, because deleting a committed
ledger line is what the append-only tier forbids. The pass runs at boot
after the replay, so a still-pending intent is judged against the
document it wrote before that document is reconciled against a ledger.

A ledger killed mid-append is the one repair that cannot wait for a WAL
record to name its tree. A swept record, or an append that never had
one, still leaves a tail with no newline, and a torn ledger refuses every
read -- the replay's own "is this line already there" check included. So
the first boot pass takes its trees from every source the daemon has, WAL
or not, and cuts each torn tail back to its last complete line before the
replay runs, which lets a replayed line land on a clean boundary. Each cut
is journalled to the tree's firehose ahead of the cut, under an id derived
from the bytes it drops, so a crash between the two re-cuts without a
second row and a clean ledger journals nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.fsync import fsync_parent_dir
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.io import state_version
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.compaction import read_document, recover_store_tree
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerError,
    LedgerRecord,
    append_ledger_record_once,
    split_torn_tail,
    truncate_torn_tail,
)
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, Epoch2Collection
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.epoch2_root import (
    NATIVE_WAL_DIRNAME,
    Epoch2RootContext,
    root_id_for,
)
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.errors import StateConflict

logger = logging.getLogger(__name__)


#: Where one root keeps the receipts of its committed mutations, relative
#: to the tree root. Under the local store, because a receipt answers a
#: retry of a request this machine made and a clone has none of ours.
RECEIPT_LOCATOR: Final = "local/epoch2/receipts"

#: Version of the receipt record shape.
RECEIPT_SCHEMA_VERSION: Final = "1"

#: The file name the firehose path resolver is anchored on, matching the
#: anchor the commit path writes its rows through.
TREE_ANCHOR_FILENAME: Final = "state.json"

#: The warning a committed mutation carries when its post-commit publish
#: did not reach the projection. The mutation stands: the caller holds a
#: receipt for a durable change and only the fan-out was lost.
PROJECTION_DEGRADED: Final = "projection_degraded"

#: A pending record whose document still reads as it did before the
#: mutation. The intent was journalled and nothing followed it.
REASON_INTENT_ABANDONED: Final = "native_intent_abandoned"

#: A record whose document matches neither the digest before the mutation
#: nor the digest after, so no replay can say what the crash left behind.
REASON_DOCUMENT_DIVERGED: Final = "native_document_diverged"

#: A record whose stored document path does not resolve to the root its
#: WAL namespace is named for.
REASON_ROOT_UNRESOLVED: Final = "native_root_unresolved"

#: A record whose bytes do not decode or do not validate.
REASON_RECORD_UNREADABLE: Final = "native_record_unreadable"

#: The envelope payload key holding the exact ledger line a ledger-only
#: mutation committed. It lives here rather than beside the commit path,
#: because the replay reads it and the commit path already depends on
#: this module.
LEDGER_LINE_KEY: Final = "ledger_line"

#: Version of the payload a ledger-tail repair row carries.
TAIL_REPAIR_EVENT_SCHEMA_VERSION: Final = "1"

#: The last segment of a ledger-tail repair event name. The row shares the
#: ``ledger`` namespace with the append rows, so a reader filtering one
#: collection's ledger events sees the cut beside the lines it follows.
TAIL_REPAIR_EVENT_SUFFIX: Final = "tail_truncated"


class IdempotencyReceipt(BaseModel):
    """One committed mutation, filed under the key that asked for it.

    Attributes:
        schema_version: Version of this record's shape.
        idempotency_key: The client's key inside this root's namespace.
            Stored as well as hashed into the file name, so a replay
            verifies it read the record it meant to.
        params_digest: Digest of the canonical request parameters that
            produced the receipt. A retry digesting differently is a
            reused key, not a retry.
        recorded_at: When the receipt was filed, which is the moment the
            mutation it describes became durable.
        receipt: The committed mutation's receipt, as the JSON payload
            the original caller was answered with.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16)]
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    params_digest: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    recorded_at: UtcDatetime
    receipt: dict[str, Any]


class NativeReplayReport(BaseModel):
    """What one pass of the native replay found and did.

    Attributes:
        root_count: Native WAL namespaces walked, one per root.
        pending_count: Records found journalled but not marked applied.
        applied_count: Records found applied but not marked durable.
        completed_count: Pending records whose document write had landed,
            so the replay carried them the rest of the way.
        abandoned_count: Pending records whose document had not moved, so
            the intent was poisoned and the mutation never happened.
        replayed_event_count: Firehose rows the replay appended. Always a
            row the log did not already hold.
        replayed_ledger_count: Ledger lines the replay appended for a
            ledger-only mutation whose document landed before its line.
        poisoned_count: Records under ``poisoned/`` once the pass ended,
            pre-existing ones included.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_count: int = 0
    pending_count: int = 0
    applied_count: int = 0
    completed_count: int = 0
    abandoned_count: int = 0
    replayed_event_count: int = 0
    replayed_ledger_count: int = 0
    poisoned_count: int = 0


class NativeStoreRecoveryReport(BaseModel):
    """What one boot's compaction recovery found across the native trees.

    Attributes:
        tree_count: Documents reconciled against their ledgers, one per
            native tree the WAL still names.
        repaired_ledgers: Ledgers whose torn trailing line was dropped.
        document_rows_dropped: Document rows removed because a standing
            ledger line had already committed the record.
        regenerated_indexes: Derived offset indexes rebuilt. Every ledger
            of every reconciled tree is rebuilt, because the index is a
            pure function of the ledger's bytes and rebuilding one that
            was already current reproduces it exactly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tree_count: int = 0
    repaired_ledgers: int = 0
    document_rows_dropped: int = 0
    regenerated_indexes: int = 0


class LedgerTailRepairReport(BaseModel):
    """What one boot's torn-tail pass found across the native trees.

    Attributes:
        tree_count: Distinct epoch-2 documents whose ledgers were scanned.
        truncated_ledgers: Ledgers cut back to their last complete line.
        journaled_rows: Repair rows appended to a firehose. Lower than
            ``truncated_ledgers`` only when a crash had already journalled
            a cut the previous boot did not finish.
        dropped_bytes: Torn bytes removed, summed over every ledger.
        skipped_trees: Trees whose scan failed and were left untouched,
            so a boot still starts when one tree is unreadable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tree_count: int = 0
    truncated_ledgers: int = 0
    journaled_rows: int = 0
    dropped_bytes: int = 0
    skipped_trees: int = 0


@dataclass
class _Tally:
    """The running counts one replay pass builds its report from."""

    root_count: int = 0
    pending_count: int = 0
    applied_count: int = 0
    completed_count: int = 0
    abandoned_count: int = 0
    replayed_event_count: int = 0
    replayed_ledger_count: int = 0
    poisoned_count: int = 0


def canonical_params_digest(params: Mapping[str, Any]) -> str:
    """Return the digest that decides whether two requests are the same one.

    Every submitted parameter is digested, without exception. A parameter
    that survives into the stored record or the emitted event -- the
    correlation id does, for instance -- changes what the request leaves
    behind, so a retry that changed it is asking for a different effect
    under a key that already named one.

    Args:
        params: The request's JSON-mode parameters.

    Returns:
        The sha256 hex digest of the sorted-key encoding.

    Raises:
        TypeError: A parameter value cannot be encoded as JSON, so no
            stable digest of it exists.
    """
    return hashlib.sha256(orjson.dumps(params, option=orjson.OPT_SORT_KEYS)).hexdigest()


def idempotency_receipt_path(context: Epoch2RootContext, *, namespaced_key: str) -> Path:
    """Return the file one root files a key's receipt in.

    The key is hashed into the name because a client key may hold
    characters a file name may not, and the key itself is stored in the
    body so the read can tell a hit from a digest collision.

    Args:
        context: The native context of the root the receipt belongs to.
        namespaced_key: The client key inside the root's namespace.

    Returns:
        The declared path of the receipt file, whether or not it exists.

    Raises:
        UndeclaredPathError: The commit policy declares no row for the
            receipt family, so nothing says whether a clone carries it.
    """
    digest = hashlib.sha256(namespaced_key.encode()).hexdigest()
    return context.declared_path(context.identity.tree_root / RECEIPT_LOCATOR / f"{digest}.json")


def read_idempotency_receipt(
    context: Epoch2RootContext, *, namespaced_key: str
) -> IdempotencyReceipt | None:
    """Return the receipt filed under *namespaced_key*, if one was.

    Args:
        context: The native context of the root the receipt belongs to.
        namespaced_key: The client key inside the root's namespace.

    Returns:
        The stored receipt, or ``None`` when this key has committed
        nothing on this root.

    Raises:
        ValueError: The receipt exists but does not decode, does not
            validate, or was filed under another key. Every case means
            the store cannot answer whether this request already ran,
            and answering "no" would run it twice.
    """
    path = idempotency_receipt_path(context, namespaced_key=namespaced_key)
    if not path.exists():
        return None
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as error:
        raise ValueError(f"the receipt filed under {namespaced_key!r} is not valid JSON") from error
    stored = IdempotencyReceipt.model_validate(payload)
    if stored.idempotency_key != namespaced_key:
        raise ValueError(
            f"the receipt file of {namespaced_key!r} holds a receipt filed under another key"
        )
    return stored


def record_idempotency_receipt(
    context: Epoch2RootContext,
    *,
    namespaced_key: str,
    params_digest: str,
    receipt: dict[str, Any],
    recorded_at: datetime,
) -> Path:
    """File the receipt of one committed mutation under its client key.

    Call this only once the document mutation the receipt describes is
    durable. A receipt is a promise that the change happened, so a
    receipt that outlives its mutation would answer a retry with a
    commit nobody made; the reverse gap costs only a revision conflict
    on the retry, which is a safe answer.

    Args:
        context: The native context of the root the receipt belongs to.
        namespaced_key: The client key inside the root's namespace.
        params_digest: The digest of the request that produced it.
        receipt: The mutation receipt, as its JSON payload.
        recorded_at: When the mutation became durable.

    Returns:
        The path the receipt was written to.

    Raises:
        UndeclaredPathError: The commit policy declares no row for it.
        ValidationError: The record does not satisfy its own contract.
    """
    record = IdempotencyReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        idempotency_key=namespaced_key,
        params_digest=params_digest,
        recorded_at=recorded_at,
        receipt=receipt,
    )
    path = idempotency_receipt_path(context, namespaced_key=namespaced_key)
    _atomic_write_bytes(path, orjson.dumps(record.model_dump(mode="json")))
    logger.info(
        f"record_idempotency_receipt root={context.identity.root_id} key={namespaced_key!r}"
    )
    return path


def publish_projection(bus: Any, envelope: Envelope) -> bool:
    """Publish one committed envelope and report whether the fan-out took it.

    The publish happens after the commit and outside the locks, so a
    failure here cannot unmake the mutation. The caller keeps the receipt
    and flags the answer :data:`PROJECTION_DEGRADED` rather than telling
    the client a durable change did not happen.

    Args:
        bus: The subscription bus, or ``None`` on a context that has
            none. A context with no bus has no fan-out to lose.
        envelope: The firehose row the commit produced.

    Returns:
        ``True`` when the projection took the envelope, or when there was
        no bus to take it; ``False`` when the publish raised.
    """
    if bus is None:
        return True
    try:
        bus.publish(envelope)
    except Exception:
        logger.exception(f"publish_projection failed event_id={envelope.id!r}")
        return False
    return True


def repair_native_ledger_tails(
    daemon_wal_dir: Path, *, tree_roots: Iterable[Path]
) -> LedgerTailRepairReport:
    """Cut every torn ledger tail back to its last complete line.

    Run once at daemon start, before :func:`replay_native_wal`: the replay
    reads a ledger to learn whether its journalled line already landed,
    and a torn ledger refuses that read. Safe to run on every boot: a
    ledger ending on a newline is left alone and journals nothing.

    Args:
        daemon_wal_dir: The daemon's WAL directory. Every tree a native
            record still names is scanned, whatever *tree_roots* holds.
        tree_roots: Tree roots to scan beyond the WAL's, such as the tree
            the daemon is bound to and every registered repository's. A
            root that is not epoch 2 is skipped without being written.

    Returns:
        What the pass cut and journalled, per :class:`LedgerTailRepairReport`.
    """
    found = list(_selected_documents(tree_roots))
    native = daemon_wal_dir / NATIVE_WAL_DIRNAME
    if native.is_dir():
        found.extend(_native_documents(native))
    documents = dict.fromkeys(path.resolve() for path in found)
    truncated = journaled = dropped = skipped = 0
    for document in documents:
        firehose = store_path(document.parents[2] / TREE_ANCHOR_FILENAME, StoreKind.EVENT)
        try:
            ids = _envelope_ids(firehose)
            for collection in LEDGER_COLLECTIONS:
                cut = _repair_tail(ledger_path(document, collection), collection, firehose, ids)
                if cut is None:
                    continue
                truncated += 1
                dropped += cut[0]
                journaled += int(cut[1])
        except LedgerError, OSError, ValueError, StateConflict, portalock.LockTimeout:
            skipped += 1
            logger.warning(
                f"repair_native_ledger_tails unreadable tree={document.parents[2].name!r}",
                exc_info=True,
            )
    report = LedgerTailRepairReport(
        tree_count=len(documents),
        truncated_ledgers=truncated,
        journaled_rows=journaled,
        dropped_bytes=dropped,
        skipped_trees=skipped,
    )
    logger.info(
        f"repair_native_ledger_tails trees={report.tree_count} "
        f"truncated={report.truncated_ledgers} journaled={report.journaled_rows} "
        f"dropped_bytes={report.dropped_bytes} skipped={report.skipped_trees}"
    )
    return report


def _selected_documents(tree_roots: Iterable[Path]) -> tuple[Path, ...]:
    """Return the selected generation's document of each epoch-2 root."""
    documents: list[Path] = []
    for root in tree_roots:
        authority = resolve_authority(root)
        if authority.target is None or authority.generation_id is None:
            continue
        documents.append(
            authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
        )
    return tuple(documents)


def _repair_tail(
    ledger: Path, collection: Epoch2Collection, firehose: Path, ids: set[str]
) -> tuple[int, bool] | None:
    """Journal and cut one ledger's torn tail, under the ledger's append lock.

    The lock is the one every append holds while it writes and fsyncs, so
    a live writer in another process is never mistaken for a torn one.

    Returns:
        ``None`` when the ledger ends on a line boundary; otherwise the
        bytes dropped and whether a repair row was appended now, which is
        ``False`` when a crashed earlier boot had journalled this same cut.
    """
    if not ledger.exists():
        return None
    with portalock.acquire(ledger):
        kept, torn = split_torn_tail(ledger.read_bytes())
        if not torn:
            return None
        envelope = _tail_repair_envelope(collection, kept=kept, torn=torn)
        journaled = envelope.id not in ids
        if journaled:
            append_json_line(firehose, envelope.model_dump_json())
            ids.add(envelope.id)
        truncate_torn_tail(ledger)
    logger.warning(
        f"_repair_tail cut collection={collection.value} dropped_bytes={len(torn)} "
        f"journaled={journaled}"
    )
    return len(torn), journaled


def _tail_repair_envelope(collection: Epoch2Collection, *, kept: bytes, torn: bytes) -> Envelope:
    """Return the firehose row that records one torn-tail cut.

    The id is derived from the kept prefix and the dropped bytes, so the
    same cut re-attempted after a crash names the row already written.
    The row carries no canonical sequence: the cut moves no record, so no
    projection has anything to patch from it.
    """
    kept_digest = hashlib.sha256(kept).hexdigest()
    torn_digest = hashlib.sha256(torn).hexdigest()
    seed = f"{collection.value}:{kept_digest}:{torn_digest}"
    event_id = f"evt-{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
    name = f"ledger.{collection.value}.{TAIL_REPAIR_EVENT_SUFFIX}"
    now = datetime.now(UTC)
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=now,
        summary=f"{name} dropped {len(torn)} bytes after byte {len(kept)}",
        payload={
            "schema_version": TAIL_REPAIR_EVENT_SCHEMA_VERSION,
            "name": name,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "collection": collection.value,
            "kept_bytes": len(kept),
            "dropped_bytes": len(torn),
            "dropped_digest": f"sha256:{torn_digest}",
        },
    )


def replay_native_wal(daemon_wal_dir: Path) -> NativeReplayReport:
    """Reconcile every native root's WAL against the tree it wrote to.

    Run once at daemon start, before any RPC is accepted, so a client
    never reads a tree whose last mutation is still mid-stride. Safe to
    run on every boot: a record whose envelope the firehose already holds
    is marked durable without a second append.

    Args:
        daemon_wal_dir: The daemon's WAL directory. Its ``native/``
            subdirectory holds one namespace per root; a WAL with no such
            subdirectory is a tree that has taken no native mutation.

    Returns:
        What the pass found and did, per :class:`NativeReplayReport`.
    """
    native = daemon_wal_dir / NATIVE_WAL_DIRNAME
    if not native.is_dir():
        return NativeReplayReport()
    tally = _Tally()
    for root_dir in sorted(entry for entry in native.iterdir() if entry.is_dir()):
        tally.root_count += 1
        _replay_root(root_dir, tally=tally)
        tally.poisoned_count += len(wal.list_poisoned(root_dir))
    report = NativeReplayReport(
        root_count=tally.root_count,
        pending_count=tally.pending_count,
        applied_count=tally.applied_count,
        completed_count=tally.completed_count,
        abandoned_count=tally.abandoned_count,
        replayed_event_count=tally.replayed_event_count,
        replayed_ledger_count=tally.replayed_ledger_count,
        poisoned_count=tally.poisoned_count,
    )
    logger.info(
        f"replay_native_wal roots={report.root_count} pending={report.pending_count} "
        f"applied={report.applied_count} completed={report.completed_count} "
        f"abandoned={report.abandoned_count} replayed={report.replayed_event_count} "
        f"ledger={report.replayed_ledger_count} poisoned={report.poisoned_count}"
    )
    return report


def recover_native_store_trees(daemon_wal_dir: Path) -> NativeStoreRecoveryReport:
    """Finish every interrupted compaction the native trees carry.

    Run once at daemon start, after :func:`replay_native_wal` and before
    any RPC is accepted, so a client never reads a tree holding one
    record in two canonical places. Safe to run on every boot: a tree
    with nothing half-moved is only re-indexed, and the index is a pure
    function of the ledger it is taken over.

    A tree is found through the WAL, because the WAL is what the daemon
    knows about the roots it has written to. A tree whose records have
    all been swept has taken no mutation since the sweep, so its ledgers
    and its document already agree.

    Args:
        daemon_wal_dir: The daemon's WAL directory. Its ``native/``
            subdirectory holds one namespace per root; a WAL with no such
            subdirectory is a daemon that has written to no native tree.

    Returns:
        What the pass reconciled, per :class:`NativeStoreRecoveryReport`.
    """
    native = daemon_wal_dir / NATIVE_WAL_DIRNAME
    if not native.is_dir():
        return NativeStoreRecoveryReport()
    trees = repaired = dropped = indexed = 0
    for document in _native_documents(native):
        try:
            repair = recover_store_tree(document)
        except LedgerError, OSError, ValueError:
            logger.warning(
                f"recover_native_store_trees unreadable tree={document.parent.name!r}",
                exc_info=True,
            )
            continue
        trees += 1
        repaired += len(repair.repaired_ledgers)
        dropped += len(repair.document_rows_dropped)
        indexed += len(repair.regenerated_indexes)
        if repair.document_rows_dropped:
            logger.warning(
                f"recover_native_store_trees finished-compaction tree={document.parent.name!r} "
                f"dropped={','.join(repair.document_rows_dropped)}"
            )
    report = NativeStoreRecoveryReport(
        tree_count=trees,
        repaired_ledgers=repaired,
        document_rows_dropped=dropped,
        regenerated_indexes=indexed,
    )
    logger.info(
        f"recover_native_store_trees trees={report.tree_count} "
        f"repaired={report.repaired_ledgers} dropped={report.document_rows_dropped} "
        f"indexed={report.regenerated_indexes}"
    )
    return report


def _native_documents(native: Path) -> tuple[Path, ...]:
    """Return each native tree's document once, in root order.

    A record is trusted for its path only once the tree that path lands
    in proves to be the root whose namespace the record was found in,
    which is the same check the replay makes before it touches a tree.

    Args:
        native: The ``native/`` directory holding one namespace per root.

    Returns:
        The distinct document paths, in the order their roots sort.
    """
    documents: dict[Path, None] = {}
    for root_dir in sorted(entry for entry in native.iterdir() if entry.is_dir()):
        for path in wal.list_records(root_dir):
            try:
                record = wal.read_record(path)
            except ValueError, OSError:
                continue
            state_path = record.state_path
            if state_path is None or _tree_root_of(record, root_id=root_dir.name) is None:
                continue
            documents[Path(state_path)] = None
    return tuple(documents)


def _replay_root(root_dir: Path, *, tally: _Tally) -> None:
    """Walk one root's namespace, pending records first."""
    known: dict[Path, set[str]] = {}
    for path in wal.list_records(root_dir, status=wal.WalStatus.PENDING):
        _replay_pending(root_dir, path, known=known, tally=tally)
    for path in wal.list_records(root_dir, status=wal.WalStatus.APPLIED):
        _replay_applied(root_dir, path, known=known, tally=tally)


def _replay_pending(
    root_dir: Path, path: Path, *, known: dict[Path, set[str]], tally: _Tally
) -> None:
    """Finish or abandon one journalled intent, by what the document says."""
    loaded = _load_record(root_dir, path)
    if loaded is None:
        return
    record_id, record = loaded
    tally.pending_count += 1
    tree_root = _tree_root_of(record, root_id=root_dir.name)
    if tree_root is None:
        wal.mark_poisoned(root_dir, record_id, reason=REASON_ROOT_UNRESOLVED)
        return
    landed = _document_landed(record)
    if landed is None:
        wal.mark_poisoned(root_dir, record_id, reason=REASON_DOCUMENT_DIVERGED)
        return
    if not landed:
        wal.mark_poisoned(root_dir, record_id, reason=REASON_INTENT_ABANDONED)
        tally.abandoned_count += 1
        logger.warning(
            f"_replay_pending abandoned record={record_id!r} reason={REASON_INTENT_ABANDONED}"
        )
        return
    wal.mark_applied(root_dir, record_id)
    tally.completed_count += 1
    _finish(root_dir, record_id, record, tree_root=tree_root, known=known, tally=tally)


def _replay_applied(
    root_dir: Path, path: Path, *, known: dict[Path, set[str]], tally: _Tally
) -> None:
    """Carry one applied record's tail: the firehose row and the durable mark."""
    loaded = _load_record(root_dir, path)
    if loaded is None:
        return
    record_id, record = loaded
    tally.applied_count += 1
    tree_root = _tree_root_of(record, root_id=root_dir.name)
    if tree_root is None:
        wal.mark_poisoned(root_dir, record_id, reason=REASON_ROOT_UNRESOLVED)
        return
    _finish(root_dir, record_id, record, tree_root=tree_root, known=known, tally=tally)


def _finish(
    root_dir: Path,
    record_id: str,
    record: wal.WalRecord,
    *,
    tree_root: Path,
    known: dict[Path, set[str]],
    tally: _Tally,
) -> None:
    """Append the missing ledger line and firehose row, then mark durable."""
    _finish_ledger_line(record, record_id=record_id, tally=tally)
    firehose = store_path(tree_root / TREE_ANCHOR_FILENAME, StoreKind.EVENT)
    ids = known.get(firehose)
    if ids is None:
        ids = _envelope_ids(firehose)
        known[firehose] = ids
    envelope_id = record.envelope.id
    if envelope_id not in ids:
        append_json_line(firehose, record.envelope.model_dump_json())
        ids.add(envelope_id)
        tally.replayed_event_count += 1
        logger.info(f"_finish replayed record={record_id!r} envelope_id={envelope_id!r}")
    wal.mark_fsynced(root_dir, record_id)


def _finish_ledger_line(record: wal.WalRecord, *, record_id: str, tally: _Tally) -> None:
    """Append the ledger line a ledger-only mutation journalled, if missing.

    Raises:
        ValidationError: The journalled line is not a ledger record. The
            record's digest already verified, so this is a writer defect
            rather than a torn file, and it is left to fail loudly.
    """
    payload = record.envelope.payload
    line = payload.get(LEDGER_LINE_KEY)
    if not isinstance(line, str) or record.state_path is None:
        return
    ledger_record = LedgerRecord.model_validate_json(line)
    target = ledger_path(Path(record.state_path), ledger_record.collection)
    if append_ledger_record_once(target, ledger_record):
        tally.replayed_ledger_count += 1
        logger.info(
            f"_finish_ledger_line replayed record={record_id!r} "
            f"collection={ledger_record.collection.value} key={ledger_record.record_key!r}"
        )


def _load_record(root_dir: Path, path: Path) -> tuple[str, wal.WalRecord] | None:
    """Return one readable, untampered record and its id, or poison it.

    Returns ``None`` when the file could not be turned into a record the
    replay may act on; in every such case the bytes have been moved under
    ``poisoned/`` for the operator to inspect.
    """
    record_id = _record_id_of(path)
    if record_id is None:
        poisoned = root_dir / "poisoned"
        poisoned.mkdir(parents=True, exist_ok=True)
        os.replace(path, poisoned / path.name)
        logger.warning(f"_load_record degenerate-name path={path.name!r}")
        return None
    try:
        record = wal.read_record(path)
    except ValueError, OSError:
        wal.mark_poisoned(root_dir, record_id, reason=REASON_RECORD_UNREADABLE)
        return None
    if not wal.verify_record_digest(record):
        wal.mark_poisoned(root_dir, record_id, reason=wal.WAL_DIGEST_MISMATCH_REASON)
        logger.warning(
            f"_load_record tampered record={record_id!r} reason={wal.WAL_DIGEST_MISMATCH_REASON}"
        )
        return None
    return record_id, record


def _document_landed(record: wal.WalRecord) -> bool | None:
    """Report which side of the document write the crash fell on.

    Returns ``True`` when the document reads as the mutation left it,
    ``False`` when it still reads as the mutation found it, and ``None``
    when it reads as neither and no replay can say.
    """
    if record.state_path is None:
        return None
    try:
        document = read_document(Path(record.state_path))
    except FileNotFoundError, ValueError, OSError:
        return None
    version = state_version(document)
    if version == record.after_state_version:
        return True
    if version == record.before_state_version:
        return False
    return None


def _tree_root_of(record: wal.WalRecord, *, root_id: str) -> Path | None:
    """Return the tree a record wrote to, once it proves to be that root's.

    The document sits at ``<tree_root>/generations/<generation>/state.json``,
    so the tree root is three levels up. The derivation is checked rather
    than trusted: the candidate's own root id must be the one naming the
    namespace the record was found in, which a record carrying another
    tree's path cannot satisfy.
    """
    if record.state_path is None:
        return None
    document = Path(record.state_path)
    if len(document.parents) < 3:
        return None
    candidate = document.parents[2]
    if root_id_for(candidate) != root_id:
        return None
    return candidate


def _envelope_ids(event_path: Path) -> set[str]:
    """Return every envelope id the firehose at *event_path* already holds."""
    if not event_path.exists():
        return set()
    ids: set[str] = set()
    with event_path.open("rb") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                continue
            envelope_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(envelope_id, str):
                ids.add(envelope_id)
    return ids


def _record_id_of(path: Path) -> str | None:
    """Return the record id a ``<id>.<status>.json`` file name carries."""
    name = path.name
    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if "." not in stem:
        return None
    record_id, _, _ = stem.rpartition(".")
    return record_id or None


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* through a temp file, replace and fsync."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        fsync_parent_dir(target)
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
    "LEDGER_LINE_KEY",
    "PROJECTION_DEGRADED",
    "REASON_DOCUMENT_DIVERGED",
    "REASON_INTENT_ABANDONED",
    "REASON_RECORD_UNREADABLE",
    "REASON_ROOT_UNRESOLVED",
    "RECEIPT_LOCATOR",
    "RECEIPT_SCHEMA_VERSION",
    "TAIL_REPAIR_EVENT_SCHEMA_VERSION",
    "TAIL_REPAIR_EVENT_SUFFIX",
    "TREE_ANCHOR_FILENAME",
    "IdempotencyReceipt",
    "LedgerTailRepairReport",
    "NativeReplayReport",
    "NativeStoreRecoveryReport",
    "canonical_params_digest",
    "idempotency_receipt_path",
    "publish_projection",
    "read_idempotency_receipt",
    "record_idempotency_receipt",
    "recover_native_store_trees",
    "repair_native_ledger_tails",
    "replay_native_wal",
]
