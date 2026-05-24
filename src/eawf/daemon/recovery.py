"""Startup WAL replay for the daemon.

Run once at daemon boot, after the socket is bound but before
accepting any RPC traffic. Walks ``<wal_dir>/`` and applies the
post-apply outcome-WAL recovery algorithm:

- ``.pending.json`` — daemon crashed before the state + event were
  durably written. Per the outcome-WAL design we never re-execute the
  mutator; the record is moved under ``poisoned/`` with a recorded
  reason so the operator can inspect (``eawf daemon replay-wal
  --inspect``) and either retry the originating mutation or abandon.
- ``.applied.json`` — state.json + event.jsonl writes happened but the
  fsync rename never landed. Verify the event row exists in
  ``event.jsonl`` (by envelope id); if missing, append + fsync, then
  rename to ``.fsynced.json``. If present, just rename — the rest of
  the transaction is already durable.

The result is the typed :class:`ReplayReport` — the daemon emits it
on the subscription bus as a ``wal_recovery`` envelope (W06 wires the
emit) and tests/CLI use it directly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.daemon import wal
from eawf.daemon.wal import WalRecord, WalStatus

logger = logging.getLogger(__name__)


_REASON_DAEMON_CRASHED_PRE_APPLY: str = "daemon_crashed_pre_apply"


class ReplayReport(BaseModel):
    """Outcome of a single :func:`replay_wal` pass.

    Attributes:
        pending_count: Number of ``.pending.json`` records found at
            boot. Each one was moved under ``poisoned/`` (never
            re-executed).
        applied_count: Number of ``.applied.json`` records found at
            boot. Each one was either replayed (event row appended)
            or rename-only no-op'd (event row already present).
        fsynced_count: Number of ``.fsynced.json`` records found at
            boot. These are durable; reported for completeness only.
        poisoned_count: Number of records that ended the run under
            ``poisoned/`` (sum of pre-existing + freshly poisoned).
        replayed_event_count: Number of envelopes the replay appended
            to ``event.jsonl`` (subset of ``applied_count`` — the
            ones whose row was missing from the log).
    """

    model_config = ConfigDict(extra="forbid")

    pending_count: int = 0
    applied_count: int = 0
    fsynced_count: int = 0
    poisoned_count: int = 0
    replayed_event_count: int = 0


def _existing_envelope_ids(event_path: Path) -> set[str]:
    """Scan *event_path* once and return the set of envelope ids."""
    if not event_path.exists():
        return set()
    ids: set[str] = set()
    with event_path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            env_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(env_id, str):
                ids.add(env_id)
    return ids


def _append_event_fsynced(event_path: Path, record: WalRecord) -> None:
    """Append *record*'s envelope to *event_path* and fsync.

    Mirrors :func:`eawf.kernel.store.append.append_envelope` semantics but
    does not acquire the JSONL sibling lock — replay runs single-
    threaded at boot before any RPC traffic is accepted, so no
    concurrent appender can race us. Keeping the lock-free path
    avoids importing the CLI's ``StateConflict`` (``kind="LockConflict"``)
    surface into the daemon recovery loop.
    """
    event_path.parent.mkdir(parents=True, exist_ok=True)
    line = record.envelope.model_dump_json() + "\n"
    with event_path.open("ab") as fh:
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def replay_wal(wal_dir: Path, state_path: Path, event_path: Path) -> ReplayReport:
    """Walk the WAL once and reconcile against state + event log.

    Args:
        wal_dir: Daemon WAL directory (``<runtime_dir>/wal/``).
            Missing directory is a clean boot — returns a zero report.
        state_path: Path to ``state.json``. Read only; replay never
            mutates state (the on-disk state already reflects every
            ``.applied.json`` record by the outcome-WAL invariant).
        event_path: Path to ``event.jsonl``. Replay appends any
            envelope whose id is absent from the log.

    Returns:
        :class:`ReplayReport` summarising counts. Idempotent on
        repeat invocation against the same on-disk state: a record
        whose envelope is already in the log is renamed once, then
        skipped on every subsequent call.
    """
    pending_count = 0
    applied_count = 0
    replayed_event_count = 0
    if not wal_dir.exists():
        return ReplayReport()

    # Step 1: pending → poisoned (never re-execute the mutator).
    for path in wal.list_records(wal_dir, status=WalStatus.PENDING):
        record_id = _record_id_from_path(path)
        if record_id is None:
            # Degenerate filename; move bytes verbatim so the operator
            # can inspect under poisoned/ without crashing replay.
            poisoned_dir = wal_dir / "poisoned"
            poisoned_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, poisoned_dir / path.name)
            continue
        wal.mark_poisoned(wal_dir, record_id, reason=_REASON_DAEMON_CRASHED_PRE_APPLY)
        pending_count += 1
        logger.warning(
            f"replay_wal pending->poisoned record={record_id!r} "
            f"reason={_REASON_DAEMON_CRASHED_PRE_APPLY}"
        )

    # Step 2: applied → maybe replay event row → fsynced.
    known_ids = _existing_envelope_ids(event_path)
    for path in wal.list_records(wal_dir, status=WalStatus.APPLIED):
        record_id = _record_id_from_path(path)
        if record_id is None:
            poisoned_dir = wal_dir / "poisoned"
            poisoned_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, poisoned_dir / path.name)
            continue
        try:
            record = wal.read_record(path)
        except ValueError, OSError:
            wal.mark_poisoned(wal_dir, record_id, reason="wal_record_unreadable")
            continue
        envelope_id = record.envelope.id
        if envelope_id not in known_ids:
            _append_event_fsynced(event_path, record)
            known_ids.add(envelope_id)
            replayed_event_count += 1
            logger.info(
                f"replay_wal applied->fsynced replayed=true "
                f"record={record_id!r} envelope_id={envelope_id!r}"
            )
        else:
            logger.info(
                f"replay_wal applied->fsynced replayed=false "
                f"record={record_id!r} envelope_id={envelope_id!r}"
            )
        wal.mark_fsynced(wal_dir, record_id)
        applied_count += 1

    # Steps 3 + 4: snapshot the post-replay directory state so the
    # report reflects truth-on-disk rather than a running tally that
    # may drift if a step short-circuits.
    fsynced_count = len(wal.list_records(wal_dir, status=WalStatus.FSYNCED))
    poisoned_count = len(wal.list_poisoned(wal_dir))

    report = ReplayReport(
        pending_count=pending_count,
        applied_count=applied_count,
        fsynced_count=fsynced_count,
        poisoned_count=poisoned_count,
        replayed_event_count=replayed_event_count,
    )
    logger.info(
        f"replay_wal report pending={report.pending_count} "
        f"applied={report.applied_count} fsynced={report.fsynced_count} "
        f"poisoned={report.poisoned_count} replayed={report.replayed_event_count}"
    )
    return report


def _record_id_from_path(path: Path) -> str | None:
    """Extract the record id from a ``<id>.<status>.json`` filename.

    Returns ``None`` when the filename does not match the expected
    two-suffix shape so the caller can route the file to ``poisoned/``
    without crashing replay.
    """
    name = path.name
    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if "." not in stem:
        return None
    record_id, _, _ = stem.rpartition(".")
    if not record_id:
        return None
    return record_id


__all__ = [
    "ReplayReport",
    "replay_wal",
]
