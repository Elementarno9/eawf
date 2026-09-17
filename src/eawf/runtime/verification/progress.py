"""Durable progress manifests for a running gate leg.

A gate command can run for up to an hour inside one blocking capture, so
without a side channel a long leg and a hung one look identical until the
budget expires, and a leg killed by its timeout leaves nothing to resume
from. This module gives a leg three pieces:

* :class:`ProgressChannel` is the command's end. The executed command
  reports what it collected and how each obligation ended; pytest does so
  when this module is loaded as a plugin (``-p
  eawf.runtime.verification.progress``). Progress comes from the command
  itself and is never inferred from its output, so a leg reports the same
  progress whichever process started it.
* :class:`ProgressPublisher` is the runner's end. It lives in the sandboxed
  gate child that holds the leg's durable claim, folds the channel into a
  :class:`ProgressManifest` at a bounded cadence, and replaces the manifest
  atomically, so a leg killed at any instant leaves a readable record of
  what had completed. A leg whose command never publishes still carries
  liveness: start time, budget and elapsed time.
* :func:`read_progress_manifest` and :func:`list_progress_manifests` are
  the readers for anyone who is not the runner. They take no lock, so a
  reader never slows or blocks the leg, and they refuse any manifest that
  no matching durable claim covers.

Manifests are daemon-local observations under ``.ea/local/gate-progress``,
not ledger state.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Self

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from eawf.kernel.delivery.receipts import FreshnessDigestStr, canonical_digest
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.writer import atomic_write_json

logger = logging.getLogger(__name__)

#: File the gate child places in the sandbox runtime directory to offer a
#: channel. The command finds it through ``EAWF_RUNTIME_DIR``, which the
#: gate env scrub carries through, so no new variable reaches the command.
PROGRESS_CHANNEL_FILENAME: Final = "progress-channel.json"

#: Append-only event log the command writes and the publisher folds.
PROGRESS_EVENTS_FILENAME: Final = "progress-events.jsonl"

#: Sandbox entries a nested sandbox must never inherit. A copied channel
#: would let a nested, unclaimed command publish into the outer leg.
PROGRESS_CHANNEL_ENTRIES: Final = frozenset({PROGRESS_CHANNEL_FILENAME, PROGRESS_EVENTS_FILENAME})

#: Seconds between durable manifest writes while a leg runs. Bounds both
#: the work lost to a kill and the write load a long leg generates.
PROGRESS_WRITE_INTERVAL_SECONDS: Final = 0.5

#: Ceiling on each output tail a manifest carries.
OUTPUT_TAIL_LIMIT: Final = 4_096

_RUNTIME_DIR_ENV: Final = "EAWF_RUNTIME_DIR"
_PYTEST_PLUGIN_NAME: Final = "eawf-progress"

#: One obligation's identity as the command names it (a pytest node id).
ObligationIdStr = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_096)]

_IdentityStr = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
_TailStr = Annotated[str, StringConstraints(max_length=OUTPUT_TAIL_LIMIT)]


class _Frozen(BaseModel):
    """Strict and immutable: a manifest edited in place is a forged one."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProgressMode(StrEnum):
    """How much a leg's command reports about its own progress."""

    NONE = "none"
    ENUMERATED = "enumerated"


class ObligationDisposition(StrEnum):
    """How one completed obligation ended."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class LegOutcome(StrEnum):
    """Where the leg a manifest describes stands."""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    ERRORED = "errored"


class UnclaimedProgressError(ValueError):
    """A manifest was offered for a leg that no matching durable claim covers."""


def progress_manifest_id(freshness_key: str) -> str:
    """Return the manifest id for the leg claimed under *freshness_key*."""
    return f"PM-{freshness_key[:32]}"


def _manifest_dir(state_path: Path) -> Path:
    """Return the daemon-local directory holding every leg's manifest."""
    return state_path.parent / "local" / "gate-progress"


def progress_manifest_path(state_path: Path, freshness_key: str) -> Path:
    """Return the daemon-local manifest path for one freshness key.

    Args:
        state_path: The live ``state.json`` whose ``.ea/local`` holds the
            manifest, beside the leg's claim.
        freshness_key: The leg's 64-hex freshness key.

    Returns:
        ``<state dir>/local/gate-progress/<freshness_key>.json``.
    """
    return _manifest_dir(state_path) / f"{freshness_key}.json"


def collection_digest(obligation_ids: Sequence[str]) -> str:
    """Return the ``sha256:`` digest of an ordered obligation collection.

    Order is part of the identity: the command's collection order is what a
    residue is replayed against.
    """
    return canonical_digest(list(obligation_ids))


class LegIdentity(_Frozen):
    """The durable claim a manifest is written under."""

    attempt_id: _IdentityStr
    criterion_id: _IdentityStr
    gate_id: _IdentityStr
    freshness_key: FreshnessDigestStr
    claimed_at: UtcDatetime


class ObligationRecord(_Frozen):
    """One completed obligation.

    ``carried`` marks a pass proved by the timed-out leg this one resumed
    rather than by this leg's own command.
    """

    obligation_id: ObligationIdStr
    disposition: ObligationDisposition
    completed_at: UtcDatetime
    carried: bool = False


class ResidueSeed(_Frozen):
    """What a resumed leg inherits from the timed-out leg it continues."""

    resumed_from: FreshnessDigestStr
    collection_digest: Sha256DigestStr
    residue: tuple[ObligationIdStr, ...] = Field(min_length=1)
    carried: tuple[ObligationRecord, ...] = ()


class LegLiveness(_Frozen):
    """Proof of life that every leg publishes, including an opaque one.

    The output tails are ``None`` while the leg runs: the runner captures
    command output in one blocking read, so a tail exists only at terminal.
    """

    started_at: UtcDatetime
    heartbeat_at: UtcDatetime
    elapsed_ms: int = Field(ge=0)
    resolved_timeout_seconds: int | None = Field(default=None, gt=0)
    stdout_tail: _TailStr | None = None
    stderr_tail: _TailStr | None = None


class ProgressManifest(_Frozen):
    """The durable, readable progress of one claimed gate leg.

    ``cursor`` counts the channel events folded so far and only grows.
    ``collected`` is the command's own enumeration; ``None`` means the
    command published none, so the leg is progress-opaque and no residue
    can be proved from it.
    """

    id: Annotated[str, Field(pattern=r"^PM-[0-9a-f]{32}$")]
    leg: LegIdentity
    producer_digest: Sha256DigestStr
    writer_pid: int = Field(gt=0)
    progress_mode: ProgressMode
    outcome: LegOutcome
    collected: tuple[ObligationIdStr, ...] | None = None
    collection_digest: Sha256DigestStr | None = None
    obligations: tuple[ObligationRecord, ...] = ()
    cursor: int = Field(ge=0)
    seed: ResidueSeed | None = None
    liveness: LegLiveness
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _identity_follows_key(self) -> Self:
        """Require the id to name the claimed freshness key.

        Raises:
            ValueError: ``id`` was derived from another key, or the record
                was updated before it was created.
        """
        if self.id != progress_manifest_id(self.leg.freshness_key):
            raise ValueError(f"manifest {self.id!r} does not name its freshness key")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @model_validator(mode="after")
    def _collection_is_consistent(self) -> Self:
        """Tie the mode, the collection, its digest and the records together.

        Raises:
            ValueError: The mode disagrees with whether a collection exists,
                the digest does not match the collection, an id repeats, or
                a record names an obligation outside the collection.
        """
        enumerated = self.collected is not None
        if enumerated != (self.progress_mode is ProgressMode.ENUMERATED):
            raise ValueError("progress_mode enumerated requires a collection, and only then")
        if self.collected is None:
            if self.collection_digest is not None or self.obligations:
                raise ValueError("an opaque leg carries no collection digest or obligations")
            return self
        if len(set(self.collected)) != len(self.collected):
            raise ValueError("collected repeats an obligation id")
        if self.collection_digest != collection_digest(self.collected):
            raise ValueError("collection_digest does not match collected")
        known = set(self.collected)
        recorded = [record.obligation_id for record in self.obligations]
        if len(set(recorded)) != len(recorded):
            raise ValueError("an obligation is recorded more than once")
        stray = sorted(set(recorded) - known)
        if stray:
            raise ValueError(f"obligations outside the collection: {stray}")
        return self

    def pending(self) -> tuple[str, ...]:
        """Return the collected obligations not proved passing, in collection order.

        Returns:
            Every obligation that is missing, failed or unknown; empty for
            an opaque leg, which has no collection to measure against.
        """
        if self.collected is None:
            return ()
        passed = {
            record.obligation_id
            for record in self.obligations
            if record.disposition is ObligationDisposition.PASS
        }
        return tuple(item for item in self.collected if item not in passed)


class ProgressEvent(_Frozen):
    """One line the command appends to its channel.

    A collect event carries ``collected``; an obligation event carries
    ``obligation_id`` and ``disposition``. The freshness key keys the event
    to one leg, so a stray writer cannot feed another leg's manifest.
    """

    freshness_key: FreshnessDigestStr
    recorded_at: UtcDatetime
    collected: tuple[ObligationIdStr, ...] | None = None
    obligation_id: ObligationIdStr | None = None
    disposition: ObligationDisposition | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        """Require exactly one event kind with all of its fields.

        Raises:
            ValueError: The event mixes kinds or leaves a field half set.
        """
        is_collect = self.collected is not None
        is_obligation = self.obligation_id is not None or self.disposition is not None
        if is_collect == is_obligation:
            raise ValueError("an event is either a collection or an obligation")
        if is_obligation and (self.obligation_id is None or self.disposition is None):
            raise ValueError("an obligation event needs both obligation_id and disposition")
        return self


class ProgressChannelDescriptor(_Frozen):
    """What the gate child hands the command: identity, log and residue.

    ``residue`` is set only for a resumed leg, together with the digest of
    the collection it was planned against.
    """

    leg: LegIdentity
    events_path: Path
    residue: tuple[ObligationIdStr, ...] | None = None
    collection_digest: Sha256DigestStr | None = None

    @model_validator(mode="after")
    def _residue_has_its_collection(self) -> Self:
        """Require a residue and its collection digest together.

        Raises:
            ValueError: Only one of the pair is set.
        """
        if (self.residue is None) != (self.collection_digest is None):
            raise ValueError("residue and collection_digest must be given together")
        return self


def _claim_mismatch(state_path: Path, leg: LegIdentity) -> str | None:
    """Return why the durable claim does not cover *leg*, or ``None`` when it does."""
    from eawf.runtime.daemon.gate_execution import load_gate_claim

    try:
        claim = load_gate_claim(state_path, leg.freshness_key)
    except ValueError as exc:
        return str(exc)
    if claim is None:
        return "no durable claim exists for the freshness key"
    differing = [
        name
        for name, recorded in (
            ("attempt_id", claim.attempt_id),
            ("criterion_id", claim.criterion_id),
            ("gate_id", claim.gate_id),
            ("freshness_key", claim.freshness_key),
            ("claimed_at", claim.claimed_at),
        )
        if getattr(leg, name) != recorded
    ]
    if differing:
        return f"the durable claim differs on {', '.join(differing)}"
    return None


def write_progress_manifest(state_path: Path, manifest: ProgressManifest) -> None:
    """Durably replace the manifest of a claimed leg.

    The write is a temp file plus an atomic rename, so a concurrent reader
    sees either the previous manifest or this one, never a partial file.

    Args:
        state_path: The live ``state.json`` the leg's claim lives beside.
        manifest: The manifest to publish.

    Raises:
        UnclaimedProgressError: No durable claim covers the manifest's leg;
            nothing is written.
    """
    mismatch = _claim_mismatch(state_path, manifest.leg)
    if mismatch is not None:
        raise UnclaimedProgressError(
            f"progress manifest {manifest.id!r} refused: {mismatch} "
            f"(gate={manifest.leg.gate_id!r} attempt={manifest.leg.attempt_id!r})"
        )
    atomic_write_json(
        progress_manifest_path(state_path, manifest.leg.freshness_key),
        manifest.model_dump(mode="json"),
    )


def read_progress_manifest(state_path: Path, freshness_key: str) -> ProgressManifest | None:
    """Return the claimed manifest for *freshness_key*, without locking.

    Args:
        state_path: The live ``state.json`` the leg's claim lives beside.
        freshness_key: The leg's freshness key.

    Returns:
        The manifest, or ``None`` when none exists, it is unreadable, it
        names another key, or no matching durable claim covers it.
    """
    path = progress_manifest_path(state_path, freshness_key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            f"read_progress_manifest status='unreadable' key={freshness_key!r} detail={exc!s}"
        )
        return None
    try:
        manifest = ProgressManifest.model_validate(orjson.loads(raw))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            f"read_progress_manifest status='invalid' key={freshness_key!r} detail={exc!s}"
        )
        return None
    if manifest.leg.freshness_key != freshness_key:
        logger.warning(
            f"read_progress_manifest status='refused' key={freshness_key!r} reason='key-mismatch'"
        )
        return None
    mismatch = _claim_mismatch(state_path, manifest.leg)
    if mismatch is not None:
        logger.warning(
            f"read_progress_manifest status='refused' key={freshness_key!r} reason={mismatch!r}"
        )
        return None
    return manifest


def list_progress_manifests(state_path: Path) -> list[ProgressManifest]:
    """Return every claimed manifest beside *state_path*, ordered by key.

    Args:
        state_path: The live ``state.json`` whose ``.ea/local`` is read.

    Returns:
        The manifests :func:`read_progress_manifest` accepts; refused and
        unreadable files are left out.
    """
    directory = _manifest_dir(state_path)
    if not directory.is_dir():
        return []
    manifests: list[ProgressManifest] = []
    for path in sorted(directory.glob("*.json")):
        manifest = read_progress_manifest(state_path, path.stem)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


class ProgressChannel:
    """The executed command's end of a leg's progress channel."""

    def __init__(self, descriptor: ProgressChannelDescriptor) -> None:
        """Bind the channel to the leg *descriptor* names.

        Args:
            descriptor: The channel the gate child offered.
        """
        self._descriptor = descriptor

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProgressChannel | None:
        """Return the channel the gate child offered this process, if any.

        Args:
            environ: Environment to resolve ``EAWF_RUNTIME_DIR`` from;
                defaults to :data:`os.environ`.

        Returns:
            The channel, or ``None`` when no channel was offered or its
            descriptor is unreadable. A command without a channel still
            runs; it is simply progress-opaque.
        """
        source = os.environ if environ is None else environ
        runtime = source.get(_RUNTIME_DIR_ENV)
        if not runtime:
            return None
        path = Path(runtime) / PROGRESS_CHANNEL_FILENAME
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning(f"from_env status='unreadable' detail={exc!s}")
            return None
        try:
            descriptor = ProgressChannelDescriptor.model_validate(orjson.loads(raw))
        except (orjson.JSONDecodeError, ValidationError) as exc:
            logger.warning(f"from_env status='invalid' detail={exc!s}")
            return None
        return cls(descriptor)

    @property
    def descriptor(self) -> ProgressChannelDescriptor:
        """Return the descriptor this channel is bound to."""
        return self._descriptor

    def collect(self, obligation_ids: Sequence[str]) -> tuple[str, ...]:
        """Publish the command's collection and return what it should run.

        Args:
            obligation_ids: Every obligation the command enumerated, in its
                own order.

        Returns:
            The residue, in collection order, when this leg resumes one and
            the collection is exactly the one the residue was planned
            against; otherwise every collected obligation. Running more
            than the residue is always safe, so a changed collection falls
            back to a full run.

        Raises:
            ValueError: An obligation id repeats or is not a valid id.
        """
        collected = tuple(obligation_ids)
        if len(set(collected)) != len(collected):
            raise ValueError("a progress collection cannot repeat an obligation id")
        self._append(
            ProgressEvent(
                freshness_key=self._descriptor.leg.freshness_key,
                recorded_at=datetime.now(UTC),
                collected=collected,
            )
        )
        residue = self._descriptor.residue
        if residue is None or collection_digest(collected) != self._descriptor.collection_digest:
            return collected
        wanted = set(residue)
        return tuple(item for item in collected if item in wanted)

    def record(self, obligation_id: str, disposition: ObligationDisposition) -> None:
        """Publish how one obligation ended.

        Args:
            obligation_id: An id from the published collection.
            disposition: Its outcome.

        Raises:
            pydantic.ValidationError: The id is empty or over-long.
        """
        self._append(
            ProgressEvent(
                freshness_key=self._descriptor.leg.freshness_key,
                recorded_at=datetime.now(UTC),
                obligation_id=obligation_id,
                disposition=disposition,
            )
        )

    def _append(self, event: ProgressEvent) -> None:
        """Append one event as a single ``O_APPEND`` write, so lines never interleave."""
        line = orjson.dumps(event.model_dump(mode="json")) + b"\n"
        fd = os.open(self._descriptor.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


class ProgressPublisher:
    """The gate child's writer of one claimed leg's durable manifest.

    :meth:`open` publishes the first manifest before any channel is offered,
    so a publisher whose leg is unclaimed is refused before the command can
    report anything. A background thread then republishes every
    :data:`PROGRESS_WRITE_INTERVAL_SECONDS` until :meth:`close`.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        leg: LegIdentity,
        producer_digest: str,
        resolved_timeout_seconds: int | None,
        channel_dir: Path | None,
        seed: ResidueSeed | None = None,
        interval_seconds: float = PROGRESS_WRITE_INTERVAL_SECONDS,
    ) -> None:
        """Prepare a publisher; nothing is written until :meth:`open`.

        Args:
            state_path: The live ``state.json`` the claim lives beside.
            leg: The claim the manifest is written under.
            producer_digest: ``sha256:`` digest of the gate contract that
                produces the leg.
            resolved_timeout_seconds: The leg's budget, when it has one.
            channel_dir: The sandbox runtime directory to offer the channel
                in; ``None`` offers none, leaving the leg progress-opaque.
            seed: The residue and carried passes of a resumed leg.
            interval_seconds: Seconds between durable writes.

        Raises:
            ValueError: *interval_seconds* is not positive.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._state_path = state_path
        self._leg = leg
        self._producer_digest = producer_digest
        self._timeout = resolved_timeout_seconds
        self._channel_dir = channel_dir
        self._seed = seed
        self._interval = interval_seconds
        self._events_path = None if channel_dir is None else channel_dir / PROGRESS_EVENTS_FILENAME
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._created_at = datetime.now(UTC)
        self._started_ns = time.monotonic_ns()
        self._offset = 0
        self._cursor = 0
        self._collected: tuple[str, ...] | None = None
        self._records: dict[str, ObligationRecord] = {}

    def open(self) -> ProgressManifest:
        """Publish the first manifest, offer the channel and start the heartbeat.

        Returns:
            The first published manifest.

        Raises:
            UnclaimedProgressError: No durable claim covers the leg; no
                channel is offered.
            OSError: The manifest or the channel descriptor cannot be
                written.
        """
        with self._lock:
            manifest = self._publish_locked(outcome=LegOutcome.RUNNING)
        if self._channel_dir is not None and self._events_path is not None:
            descriptor = ProgressChannelDescriptor(
                leg=self._leg,
                events_path=self._events_path,
                residue=None if self._seed is None else self._seed.residue,
                collection_digest=None if self._seed is None else self._seed.collection_digest,
            )
            self._events_path.touch()
            atomic_write_json(
                self._channel_dir / PROGRESS_CHANNEL_FILENAME,
                descriptor.model_dump(mode="json"),
            )
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"eawf-progress-{self._leg.gate_id}",
            daemon=True,
        )
        self._thread.start()
        return manifest

    def pump(self) -> ProgressManifest:
        """Fold new channel events and republish the running manifest.

        Returns:
            The published manifest.

        Raises:
            UnclaimedProgressError: The claim no longer covers the leg.
        """
        with self._lock:
            return self._publish_locked(outcome=LegOutcome.RUNNING)

    def close(
        self,
        *,
        outcome: LegOutcome,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
    ) -> ProgressManifest:
        """Stop the heartbeat, withdraw the channel and publish the terminal manifest.

        Args:
            outcome: How the leg ended; never :attr:`LegOutcome.RUNNING`.
            stdout_tail: The command's captured stdout; only its tail is kept.
            stderr_tail: The command's captured stderr; only its tail is kept.

        Returns:
            The terminal manifest.

        Raises:
            ValueError: *outcome* is ``running``.
            UnclaimedProgressError: The claim no longer covers the leg.
        """
        if outcome is LegOutcome.RUNNING:
            raise ValueError("a closing leg cannot still be running")
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._channel_dir is not None:
            (self._channel_dir / PROGRESS_CHANNEL_FILENAME).unlink(missing_ok=True)
        with self._lock:
            return self._publish_locked(
                outcome=outcome,
                stdout_tail=None if stdout_tail is None else stdout_tail[-OUTPUT_TAIL_LIMIT:],
                stderr_tail=None if stderr_tail is None else stderr_tail[-OUTPUT_TAIL_LIMIT:],
            )

    def _heartbeat(self) -> None:
        """Republish on the write interval until closed; a failed write is only logged."""
        while not self._stop.wait(self._interval):
            try:
                self.pump()
            except (OSError, ValueError) as exc:
                logger.warning(
                    f"_heartbeat status='skipped' gate={self._leg.gate_id!r} detail={exc!s}"
                )

    def _publish_locked(
        self,
        *,
        outcome: LegOutcome,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
    ) -> ProgressManifest:
        """Fold pending events, then build and durably write the manifest."""
        for event in self._drain_locked():
            self._fold_locked(event)
        now = datetime.now(UTC)
        collected = self._collected
        manifest = ProgressManifest(
            id=progress_manifest_id(self._leg.freshness_key),
            leg=self._leg,
            producer_digest=self._producer_digest,
            writer_pid=os.getpid(),
            progress_mode=ProgressMode.NONE if collected is None else ProgressMode.ENUMERATED,
            outcome=outcome,
            collected=collected,
            collection_digest=None if collected is None else collection_digest(collected),
            obligations=(
                ()
                if collected is None
                else tuple(self._records[item] for item in collected if item in self._records)
            ),
            cursor=self._cursor,
            seed=self._seed,
            liveness=LegLiveness(
                started_at=self._created_at,
                heartbeat_at=now,
                elapsed_ms=(time.monotonic_ns() - self._started_ns) // 1_000_000,
                resolved_timeout_seconds=self._timeout,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
            ),
            created_at=self._created_at,
            updated_at=now,
        )
        write_progress_manifest(self._state_path, manifest)
        return manifest

    def _drain_locked(self) -> list[ProgressEvent]:
        """Return the complete, well-formed events appended since the last drain.

        A trailing partial line is left for the next drain, so an event the
        command is still writing is never half-read.
        """
        if self._events_path is None:
            return []
        try:
            with self._events_path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        complete = chunk[: end + 1]
        self._offset += len(complete)
        events: list[ProgressEvent] = []
        for line in complete.splitlines():
            if not line.strip():
                continue
            self._cursor += 1
            try:
                event = ProgressEvent.model_validate(orjson.loads(line))
            except (orjson.JSONDecodeError, ValidationError) as exc:
                logger.warning(f"_drain_locked status='skipped' reason='invalid' detail={exc!s}")
                continue
            if event.freshness_key != self._leg.freshness_key:
                logger.warning("_drain_locked status='skipped' reason='foreign-key'")
                continue
            events.append(event)
        return events

    def _fold_locked(self, event: ProgressEvent) -> None:
        """Apply one event to the leg's collection and records."""
        if event.collected is not None:
            if self._collected is not None:
                if event.collected != self._collected:
                    logger.warning("_fold_locked status='skipped' reason='second-collection'")
                return
            self._collected = event.collected
            seed = self._seed
            if seed is not None and collection_digest(event.collected) == seed.collection_digest:
                self._records = {record.obligation_id: record for record in seed.carried}
            return
        if self._collected is None or event.obligation_id not in self._collected:
            logger.warning("_fold_locked status='skipped' reason='uncollected-obligation'")
            return
        assert event.disposition is not None
        self._records[event.obligation_id] = ObligationRecord(
            obligation_id=event.obligation_id,
            disposition=event.disposition,
            completed_at=event.recorded_at,
        )


class _PytestProgressPlugin:
    """Publishes one leg's pytest items as obligations through its channel."""

    def __init__(self, channel: ProgressChannel) -> None:
        self._channel = channel
        self._failed: set[str] = set()

    def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
        """Publish the collection and deselect everything outside the residue."""
        selected = set(self._channel.collect([item.nodeid for item in items]))
        deselected = [item for item in items if item.nodeid not in selected]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item.nodeid in selected]

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Record each test once its teardown ends, failed if any phase failed."""
        if report.failed:
            self._failed.add(report.nodeid)
        if report.when != "teardown":
            return
        disposition = (
            ObligationDisposition.FAIL
            if report.nodeid in self._failed
            else ObligationDisposition.PASS
        )
        self._channel.record(report.nodeid, disposition)


def pytest_configure(config: Any) -> None:
    """Register the obligation reporter when a gate child offered a channel.

    Loaded with ``pytest -p eawf.runtime.verification.progress``. Without an
    offered channel the plugin does nothing, so the same argv runs unchanged
    outside a gate.
    """
    channel = ProgressChannel.from_env()
    if channel is not None:
        config.pluginmanager.register(_PytestProgressPlugin(channel), _PYTEST_PLUGIN_NAME)


__all__ = [
    "OUTPUT_TAIL_LIMIT",
    "PROGRESS_CHANNEL_ENTRIES",
    "PROGRESS_CHANNEL_FILENAME",
    "PROGRESS_EVENTS_FILENAME",
    "PROGRESS_WRITE_INTERVAL_SECONDS",
    "LegIdentity",
    "LegLiveness",
    "LegOutcome",
    "ObligationDisposition",
    "ObligationRecord",
    "ProgressChannel",
    "ProgressChannelDescriptor",
    "ProgressEvent",
    "ProgressManifest",
    "ProgressMode",
    "ProgressPublisher",
    "ResidueSeed",
    "UnclaimedProgressError",
    "collection_digest",
    "list_progress_manifests",
    "progress_manifest_id",
    "progress_manifest_path",
    "read_progress_manifest",
    "write_progress_manifest",
]
