"""The fields a Run's start and stop edges record about its counters.

The daemon is the one caller of the capture producer. When a Run starts it
counts the Runs already active on the same vendor session -- that count is
the divisor the session is shared by, and it has to be taken now, because
recovering it later from another snapshot yields a different number -- and
records the baseline. When a Run stops it records what the stop reading
makes of that baseline. Both are returned as transition updates, so the
readings land in the same commit as the edge they describe.

The reads happen before the transaction opens, never under its locks: a
transcript can be large, and the transaction re-validates the record the
updates land on anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.measurement.capture import capture_run_start, capture_run_terminal
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext

logger = logging.getLogger(__name__)

_INSTANT: TypeAdapter[datetime] = TypeAdapter(UtcDatetime)


def _sharing_runs(document: dict[str, Any], *, run: Run, ref: VendorSessionRef) -> int:
    """Return how many Runs share *ref* once *run* starts, *run* included."""
    others = 0
    for key, row in document_rows(document, Epoch2Collection.RUN).items():
        if key == run.key or row.get("status") != RunStatus.RUNNING.value:
            continue
        session = row.get("vendor_session")
        if isinstance(session, Mapping) and session.get("session_digest") == ref.session_digest:
            others += 1
    return others + 1


def start_capture_updates(
    document: dict[str, Any], *, run: Run, updates: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the vendor session and baseline a Run's start records.

    Args:
        document: The root document the concurrent Runs are counted in.
        run: The QUEUED Run being started.
        updates: The start edge's caller-supplied updates; ``started_at``
            is required by the edge, and ``vendor_session`` names the
            session when the Run does not already carry one.

    Returns:
        The ``vendor_session`` and ``counter_baseline`` updates.

    Raises:
        ValidationError: ``started_at`` or ``vendor_session`` is malformed.
    """
    presented = updates.get("vendor_session")
    ref = run.vendor_session if presented is None else VendorSessionRef.model_validate(presented)
    started_at = _INSTANT.validate_python(updates["started_at"])
    count = 1 if ref is None else _sharing_runs(document, run=run, ref=ref)
    baseline = capture_run_start(ref, at=started_at, concurrent_run_count=count)
    return {
        "vendor_session": None if ref is None else ref.model_dump(mode="json"),
        "counter_baseline": baseline.model_dump(mode="json"),
    }


def terminal_capture_updates(run: Run, *, ended_at: object) -> dict[str, Any]:
    """Return the captured runtime a Run's stop records.

    Args:
        run: The Run being stopped.
        ended_at: The stop edge's ``ended_at`` update.

    Returns:
        The ``captured_runtime`` update.

    Raises:
        ValidationError: ``ended_at`` is malformed.
    """
    at = _INSTANT.validate_python(ended_at)
    captured = capture_run_terminal(
        run.vendor_session,
        baseline=run.counter_baseline,
        started_at=run.started_at or at,
        at=at,
    )
    return {"captured_runtime": captured.model_dump(mode="json")}


def bind_run_capture(
    context: Epoch2RootContext,
    *,
    urn: QualifiedUrn,
    to_status: RunStatus,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Return *updates* carrying the counter readings a Run edge records.

    Only a start out of the queue and a stop from a started Run take a
    reading. A Run the document does not hold, or an edge missing the
    stamp it requires, is left to the transaction to refuse.

    Args:
        context: The native context of the Run's root.
        urn: The Run being moved.
        to_status: Where the edge takes it.
        updates: The edge's caller-supplied updates.

    Returns:
        The updates, joined by the readings the edge records.

    Raises:
        ValidationError: A presented ``vendor_session`` or a stamp is
            malformed.
    """
    normalized = dict(updates)
    # A presented session is stored hashed whatever the Run's status, so a
    # retry that finds the Run already moved presents the same request.
    if normalized.get("vendor_session") is not None:
        ref = VendorSessionRef.model_validate(normalized["vendor_session"])
        normalized["vendor_session"] = ref.model_dump(mode="json")
    with context.session([urn]) as session:
        document = session.read_document()
    row = document_rows(document, Epoch2Collection.RUN).get(urn.entity_key)
    if row is None:
        return normalized
    try:
        run = Run.model_validate(row)
    except ValidationError:
        return normalized
    if to_status is RunStatus.RUNNING and run.status is RunStatus.QUEUED:
        if "started_at" not in normalized:
            return normalized
        return {**normalized, **start_capture_updates(document, run=run, updates=normalized)}
    if to_status in TERMINAL_RUN_STATUSES and "ended_at" in normalized:
        return {**normalized, **terminal_capture_updates(run, ended_at=normalized["ended_at"])}
    return normalized


__all__ = ["bind_run_capture", "start_capture_updates", "terminal_capture_updates"]
