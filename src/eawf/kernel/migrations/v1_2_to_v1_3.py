"""Concrete ``1.2`` -> ``1.3`` migration step.

The v1.3 schema delta adds :attr:`~eawf.kernel.state.models.Wave.claimed_at`, an
optional work-start timestamp stamped on the first claim. Before v1.3 the
elapsed-clock consumers (the TUI roadmap time-burn bar and the daemon
wave-elapsed publisher) anchored on ``opened_at`` -- but ``opened_at`` is
plan/creation time, stamped when the wave row is inserted, not when work
starts. Under plan-all-then-execute (a phase whose waves are all planned
up front, then claimed hours later) that inflated the elapsed clocks
roughly sixty-fold. v1.3 splits the two facts: ``opened_at`` stays
plan-time and ``claimed_at`` carries work-start, so the consumers anchor
on ``claimed_at`` and render no clock at all while it is unset.

Backfill source -- the wave_claimed event anchors
--------------------------------------------------
A wave's true work-start fact for the historical corpus is not in the
state shape (``opened_at`` is plan-time; the per-attempt ``started_at`` is
dispatch-time and absent for a claimed-but-undispatched wave). The
canonical record is the ``wave_claimed`` event the daemon appends on the
claim mutation: its timestamp IS the claim instant. This step therefore
reads the sibling JSONL event store, takes the latest ``wave_claimed``
timestamp per wave id (last write wins, matching the daemon stale-wave
detector's anchor semantics), and backfills it onto the matching wave row.

The transform operates on the **raw** state dict and only ever *adds* the
``claimed_at`` key to a wave row that lacks it (idempotent ``setdefault``),
so a re-run -- or a wave a later lifecycle writer already stamped -- is
left untouched. A wave with no ``wave_claimed`` event (it was never
claimed, or its events were pruned) keeps ``claimed_at`` unset, which the
v1.3 model loads as ``None``: the optional field's default. The event
store path is supplied by :func:`eawf.kernel.migrations._base.run_chain`
through :meth:`bind_events_path` (this step opts in to the
:class:`~eawf.kernel.migrations._base.EventAnchoredMigration` protocol);
when no path is bound, or the store is absent, the backfill no-ops and
every wave stays unset.

The pre/post invariants Pydantic-load against lean fixture models that
read only the ``schema_version`` marker, matching the ``v1_1_to_v1_2``
step.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)

#: Event-kind discriminator whose timestamp anchors a wave's work-start.
_CLAIM_EVENT_KIND = "wave_claimed"

#: Extras key the daemon may carry the wave id under when the envelope's
#: ``scope_id`` is not the wave id itself (mirrors the stale-wave reader).
_WAVE_ID_EXTRA_KEY = "wave_id"


class StateV12(BaseModel):
    """Lean from-version invariant model -- the v1.2 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.2"]


class StateV13(BaseModel):
    """Lean to-version invariant model -- the v1.3 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.3"]


def read_claim_anchors(events_path: Path) -> dict[str, datetime]:
    """Return the latest ``wave_claimed`` timestamp per wave id.

    Reads the JSONL event store at *events_path*, skipping malformed rows
    and non-event envelopes. The latest timestamp per wave wins (last
    write), matching the daemon stale-wave detector's anchor semantics.
    The wave id is the envelope ``scope_id`` when present, falling back to
    the ``wave_id`` extras key.

    Args:
        events_path: Absolute path to the JSONL event store.

    Returns:
        ``wave_id -> claim-event timestamp`` map. Empty when *events_path*
        is absent or carries no ``wave_claimed`` row.
    """
    # Imported in-body (not module-level) so importing the migration chain --
    # which the `eawf migrate` CLI command pulls -- does not load the heavy
    # store.kinds graph into the CLI tree-build path (import-budget gate).
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload

    anchors: dict[str, datetime] = {}
    if not events_path.is_file():
        return anchors
    with events_path.open("rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = Envelope.model_validate(orjson.loads(line))
            except (orjson.JSONDecodeError, ValueError) as exc:
                logger.debug(f"read_claim_anchors skip envelope cause={exc!r}")
                continue
            if envelope.kind is not StoreKind.EVENT:
                continue
            try:
                payload = EventPayload.model_validate(envelope.payload)
            except ValueError as exc:
                logger.debug(f"read_claim_anchors skip payload cause={exc!r}")
                continue
            if payload.event_kind != _CLAIM_EVENT_KIND:
                continue
            wave_id = envelope.scope_id if isinstance(envelope.scope_id, str) else None
            if not wave_id:
                extra_wave_id = payload.extras.get(_WAVE_ID_EXTRA_KEY)
                wave_id = extra_wave_id if isinstance(extra_wave_id, str) else None
            if wave_id is None:
                continue
            anchors[wave_id] = payload.timestamp
    return anchors


class MigrationV12ToV13:
    """Migrate a ``state.json`` dict from schema ``1.2`` to ``1.3``.

    Opts in to the
    :class:`~eawf.kernel.migrations._base.EventAnchoredMigration` protocol:
    :func:`~eawf.kernel.migrations._base.run_chain` binds the sibling event
    store path via :meth:`bind_events_path` before :meth:`apply`, so the
    transform can backfill each wave's ``claimed_at`` from its
    ``wave_claimed`` event timestamp. ``run_chain`` re-binds on every run,
    so the registered singleton never carries a stale path.
    """

    from_version = "1.2"
    to_version = "1.3"

    def __init__(self) -> None:
        self._events_path: Path | None = None

    def bind_events_path(self, events_path: Path | None) -> None:
        """Supply the sibling event-store path for the next :meth:`apply`.

        Args:
            events_path: Absolute path to the JSONL event store, or
                ``None`` to leave every wave's ``claimed_at`` unset.
        """
        self._events_path = events_path

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Backfill ``Wave.claimed_at`` then bump ``schema_version``.

        Reads the bound event store for ``wave_claimed`` anchors (no-op
        when no path is bound or the store is absent), adds ``claimed_at``
        to every wave row that lacks it and has a claim anchor
        (idempotent ``setdefault``), and rewrites ``schema_version``
        ``1.2`` -> ``1.3``. A wave with no claim anchor keeps
        ``claimed_at`` unset; the v1.3 model loads it as ``None``.

        Args:
            state_dict: Raw v1.2 state dict.

        Returns:
            A deep copy at schema ``1.3`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)

        anchors = self._claim_anchors()
        if anchors:
            section = migrated.get("waves")
            if isinstance(section, dict):
                for wave_id, row in section.items():
                    if not isinstance(row, dict):
                        continue
                    anchor = anchors.get(wave_id)
                    if anchor is None:
                        continue
                    row.setdefault("claimed_at", _isoformat(anchor))

        migrated["schema_version"] = "1.3"
        logger.info(
            f"apply from={self.from_version} to={self.to_version} backfilled={len(anchors)}"
        )
        return migrated

    def _claim_anchors(self) -> dict[str, datetime]:
        """Return the per-wave claim anchors from the bound event store.

        Empty when no event store path is bound -- the backfill then
        no-ops and every wave's ``claimed_at`` stays unset.
        """
        if self._events_path is None:
            return {}
        return read_claim_anchors(self._events_path)

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.2 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.2
                payload.
        """
        StateV12.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.3 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.3
                payload.
        """
        StateV13.model_validate(state_dict)


def _isoformat(value: datetime) -> str:
    """Return the ISO-8601 string the v1.3 ``UtcDatetime`` field round-trips.

    The raw state dict stores datetimes as ISO strings (Pydantic decodes
    them on load), so the backfilled ``claimed_at`` matches the on-disk
    shape of the sibling ``opened_at`` / ``closed_at`` fields.
    """
    return value.isoformat()


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV12ToV13())


__all__ = ["STEP", "MigrationV12ToV13", "read_claim_anchors"]
