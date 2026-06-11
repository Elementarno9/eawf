"""Library-level state-write primitives for the in-process fallback path.

These helpers used to live in :mod:`eawf.surfaces.cli.commands.lifecycle`, violating
the "CLI is dispatch; library implements" rule — atomic state writes, event
envelope construction, and the crash-safe commit sequence are domain logic,
not argument parsing. They now live here so the CLI layer composes them as a
thin dispatcher and the daemon-down fallback path is reusable from any caller
holding the sibling lock.

The functions split into three concerns:

- :func:`write_state_unlocked` — atomic ``state.json`` persist (no lock
  acquisition; the caller holds the transaction-level
  :func:`eawf.runtime.lock.portalock.acquire` lock for the whole read-modify-write).
- :func:`state_version` / :func:`build_event_envelope` / :func:`append_event`
  — the event-side primitives: a stable payload digest, the canonical
  ``EVENT``-kind envelope, and the routed append.
- :func:`commit_mutation` — the in-process WAL-backed transaction (the V1
  carve-out: CI / one-shot / recovery shell). The canonical writer is the
  daemon (rule 4); this path runs only when the daemon is unavailable and
  mirrors the daemon's outcome-WAL ordering so the *same*
  :func:`eawf.runtime.daemon.recovery.replay_wal` reconciles a crash.

Validation rejections raise :class:`StateValidationError` (a stdlib
:class:`ValueError` subclass) rather than a CLI-layer error type, keeping
this module free of the ``eawf.surfaces.cli`` import that would otherwise invert the
layering. The CLI maps it onto its canonical
:class:`eawf.surfaces.cli.errors.ValidationError` exit code at the boundary.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson

from eawf.kernel.state.enums import StoreKind

if TYPE_CHECKING:
    from pathlib import Path

    from eawf.kernel.state.models import State
    from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


class StateValidationError(ValueError):
    """Candidate ``state.json`` payload failed strict invariant validation.

    Raised by :func:`commit_mutation` when the post-apply payload does not
    pass :func:`eawf.kernel.validate.strict.validate_state`. Subclasses
    :class:`ValueError` so callers that have no CLI-error context can still
    catch it; the CLI boundary maps it onto
    :class:`eawf.surfaces.cli.errors.ValidationError` (exit code 2).
    """


def write_state_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically WITHOUT acquiring the sibling lock.

    The caller must already hold the lock via
    :func:`eawf.runtime.lock.portalock.acquire`. The locked variant lives in
    :mod:`eawf.kernel.state.writer`; this unlocked variant is needed because the
    transaction-level lock is held for the entire handler.

    Args:
        path: Destination ``state.json`` path (parent dirs are created).
        data: JSON-serialisable payload to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = path.with_name(f"{path.name}.tmp.{suffix}")
    payload = orjson.dumps(dict(data), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):  # parent-dir fsync is POSIX-only (no-op on Windows)
            parent_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        tmp.unlink(missing_ok=True)


def state_version(payload: dict[str, Any]) -> str:
    """Return a stable 16-hex-char digest of a state payload.

    Used as the ``before_state_version`` / ``after_state_version`` pointer on
    the event row. Mirrors
    :func:`eawf.runtime.daemon.methods.state._state_version` so the digests stay
    comparable across the in-process and daemon-proxy paths.

    Args:
        payload: JSON-mode state payload.

    Returns:
        The first 16 hex chars of the sorted-key sha256 digest.
    """
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def build_event_envelope(
    *,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    before_version: str,
    after_version: str,
    summary: str,
) -> Envelope:
    """Build (but do not append) the canonical ``EVENT``-kind envelope.

    Split out of :func:`append_event` so the WAL-backed commit path can
    capture the post-apply envelope in the ``.pending`` record before the
    state write, then append the *same* envelope after the state lands. The
    envelope shape mirrors
    :func:`eawf.runtime.daemon.methods.state._build_event_envelope` so the in-process
    fallback and the daemon-proxy path converge on identical on-disk rows.

    Args:
        command: Verb name recorded as ``event_type`` and ``command``.
        args: Verb args; hashed into ``args_hash``.
        scope_id: Scope the event is attributed to.
        before_version: State digest before the mutation.
        after_version: State digest after the mutation.
        summary: Human-readable one-line summary.

    Returns:
        An unsaved :class:`~eawf.kernel.store.envelope.Envelope`.
    """
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload

    args_blob = orjson.dumps(args, option=orjson.OPT_SORT_KEYS)
    args_hash = hashlib.sha256(args_blob).hexdigest()[:16]
    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=EventPayload(
            timestamp=now,
            event_type=command,
            actor="cli",
            command=command,
            args_hash=args_hash,
            before_state_version=before_version,
            after_state_version=after_version,
            status="ok",
            message=summary,
        ).model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )


def append_event(
    events_path: Path,
    *,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    before_version: str,
    after_version: str,
    summary: str,
) -> None:
    """Append one ``EVENT``-kind envelope to *events_path*.

    Builds the envelope via :func:`build_event_envelope` and routes the write
    through :func:`eawf.kernel.store.append.append_envelope`. The caller already
    holds the state-side sibling lock; the events store uses its own sibling
    lock so concurrent appends from unrelated callers stay safe.

    Args:
        events_path: Destination ``event.jsonl`` path.
        command: Verb name recorded on the envelope.
        args: Verb args; hashed into ``args_hash``.
        scope_id: Scope the event is attributed to.
        before_version: State digest before the mutation.
        after_version: State digest after the mutation.
        summary: Human-readable one-line summary.
    """
    from eawf.kernel.store.append import append_envelope

    envelope = build_event_envelope(
        command=command,
        args=args,
        scope_id=scope_id,
        before_version=before_version,
        after_version=after_version,
        summary=summary,
    )
    append_envelope(events_path, envelope)


def fallback_wal_dir(state_path: Path) -> Path:
    """Return the in-process fallback WAL directory for *state_path*.

    The fallback writer is the V1 carve-out (CI / one-shot / recovery shell)
    — it does not share the daemon's per-user ``<runtime>/wal``. Instead it
    keeps a repo-local WAL under ``.ea/locks/wal/`` so a crash mid-write is
    reconcilable on the next mutation against the same repo's ``state.json``
    + ``event.jsonl``. ``.ea/locks/`` is gitignored machine-local scratch,
    the right home for a transient recovery aid.

    Args:
        state_path: Path to the active ``state.json``.

    Returns:
        The repo-local ``locks/wal`` directory beside the state file.
    """
    return state_path.parent / "locks" / "wal"


def commit_mutation(
    state_path: Path,
    *,
    candidate: State,
    before_version: str,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    summary: str,
) -> dict[str, Any]:
    """Validate + WAL-pending + persist state + append event, crash-safely.

    This is the in-process fallback writer (the V1 carve-out: CI / one-shot /
    recovery shell). The canonical writer is the daemon (rule 4); this path
    runs only when the daemon is unavailable, and it mirrors the daemon's
    outcome-WAL ordering so the *same*
    :func:`eawf.runtime.daemon.recovery.replay_wal` reconciles a crash.

    Order (state-first, WAL-backed):

    1. ``replay_wal`` over the fallback WAL to finish/roll-forward any record
       a prior crashed fallback left behind (idempotent no-op on a clean
       WAL).
    2. Build the post-apply event envelope in memory.
    3. ``write_pending`` the envelope to the WAL — the durable capture that
       lets replay re-issue the event row verbatim.
    4. :func:`write_state_unlocked` persists ``state.json`` (fsynced) — the
       point of no return.
    5. ``mark_applied`` retires the WAL record to ``.applied`` **before** the
       event append.
    6. ``append_envelope`` lands the event row in ``event.jsonl``, then
       ``mark_fsynced`` completes the record.

    The invariant this buys: the event row is appended **only after**
    ``state.json`` is durably written, so a crash never leaves a phantom
    event (an event whose state change did not commit). Because
    ``mark_applied`` fires before the event append, a crash in the
    state-write→event-append window leaves an ``.applied`` record, and the
    next startup's :func:`eawf.runtime.daemon.recovery.replay_wal` re-issues the
    captured envelope (idempotent on envelope id) so state and the event log
    stay in sync. ``replay_wal`` only re-issues APPLIED records; a PENDING
    record (crash before the state write landed) is POISONED and its mutator
    is never re-run — which is why ``mark_applied`` must precede the append,
    not follow it.

    Args:
        state_path: Path to the active ``state.json``.
        candidate: The mutated, typed state to persist.
        before_version: State digest captured before the mutation.
        command: Verb name recorded on the event row.
        args: Verb args; hashed into the event ``args_hash``.
        scope_id: Scope the event is attributed to.
        summary: Human-readable one-line summary.

    Returns:
        The candidate payload (already JSON-mode-dumped) so the caller can
        compute its own envelope without a second ``model_dump``.

    Raises:
        StateValidationError: When the post-apply payload fails strict
            invariant validation.
    """
    from eawf.kernel.store.append import append_envelope
    from eawf.kernel.store.paths import store_path
    from eawf.runtime.daemon import wal
    from eawf.runtime.daemon.recovery import replay_wal
    from eawf.runtime.daemon.wal import WalRecord

    payload = candidate.model_dump(mode="json")
    # validate the payload that will actually go to disk
    _validate_or_raise(payload)
    after_version = state_version(payload)
    events_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = fallback_wal_dir(state_path)

    # Roll forward any record a prior crashed fallback left behind so the
    # log never carries an event whose state change is missing, and so a
    # half-applied prior write completes before this one starts.
    replay_wal(wal_dir, state_path=state_path, event_path=events_path)

    envelope = build_event_envelope(
        command=command,
        args=args,
        scope_id=scope_id,
        before_version=before_version,
        after_version=after_version,
        summary=summary,
    )
    record_id = uuid.uuid4().hex
    record = WalRecord(
        record_id=record_id,
        envelope=envelope,
        idempotency_key=None,
        written_at=datetime.now(UTC),
        before_state_version=before_version,
        after_state_version=after_version,
    )
    wal.write_pending(wal_dir, record)
    # ``write_state_unlocked`` fsyncs state.json — the point of no return.
    # Mark APPLIED before the event append so a crash in the
    # state-write→event-append window leaves an APPLIED record that replay
    # re-issues, not a PENDING one that replay poisons (which would silently
    # drop the event row). Mirrors the daemon mutator.
    write_state_unlocked(state_path, payload)
    wal.mark_applied(wal_dir, record_id)
    append_envelope(events_path, envelope)
    wal.mark_fsynced(wal_dir, record_id)
    logger.info(
        f"commit_mutation command={command!r} scope={scope_id!r} "
        f"before={before_version} after={after_version} record={record_id!r}"
    )
    return payload


def _validate_or_raise(payload: dict[str, Any]) -> State:
    """Validate the candidate payload; raise on schema / invariant failure.

    Args:
        payload: JSON-mode candidate state payload.

    Returns:
        The validated typed :class:`~eawf.kernel.state.models.State`.

    Raises:
        StateValidationError: When schema validation or any cross-entity
            invariant check fails. The message is the ``"; "``-joined list
            of schema errors followed by ``"<code>@<path>: <message>"`` for
            each invariant violation.
    """
    from eawf.kernel.validate.strict import validate_state

    report = validate_state(payload, strict_optional=False)
    if not report.ok:
        msgs = list(report.schema_errors)
        msgs.extend(f"{v.code}@{v.path}: {v.message}" for v in report.violations)
        raise StateValidationError("; ".join(msgs))
    assert report.state is not None  # ok==True guarantees this
    return report.state
