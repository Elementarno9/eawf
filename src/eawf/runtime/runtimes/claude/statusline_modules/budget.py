"""``budget`` statusline module — this session's Run spend against its sealed cap.

The scope is the Run bound to the host's session in the tree's selected
generation. Its cap is the token ceiling its dispatch binding sealed into the
authority capsule, and its spend is what its usage readings on the run ledger
add up to, folded so a running total is never counted twice. The segment
renders ``budget:<spent>/<cap>`` and marks ``!limit`` once the spend reaches the
cap, which is where the in-flight meter opens its limit-reached notice.

Every failure degrades to an explicit ``budget:n/a(<reason>)`` marker rather
than a guessed number: no workspace, a tree still in epoch 1, no host session,
no Run bound to it, an unreadable run ledger, a Run sealed without a token cap,
or a Run with no usage reading yet.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from eawf.kernel.projection.truth import Precision, TruthKind
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.usage import UsagePayload, UsageQuality, aggregate_usage
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.ledger import LedgerError, LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    NO_STATE,
    selected_generation,
    session_run,
)
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    budget_segment,
    budget_unavailable_segment,
)

logger = logging.getLogger(__name__)

#: The producer a reading names: the run ledger holding the binding and the readings.
RUN_LEDGER_PRODUCER: Final = "eawf.epoch2-run-ledger"

_RUN_SOURCE: Final = SegmentSource(
    producer=RUN_LEDGER_PRODUCER,
    provenance=f"generation#ledger/{Epoch2Collection.RUN.value}.jsonl",
    truth_kind=TruthKind.DERIVED,
)

# Ordered from the most to the least trusted: a total is as good as its worst
# reading, and an "unavailable" one beside a counted one leaves an estimate.
_QUALITY: Final[Mapping[UsageQuality, MeasurementQuality]] = {
    "measured": MeasurementQuality.EXACT,
    "derived": MeasurementQuality.RECONSTRUCTED,
    "estimated": MeasurementQuality.ESTIMATED,
    "unavailable": MeasurementQuality.ESTIMATED,
}
_RANK: Final = tuple(_QUALITY)


def _token_cap(records: tuple[LedgerRecord, ...], run: Run) -> int | None:
    """Return the token cap the Run's binding sealed, or ``None`` when unbound or uncapped."""
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.run_ref == run.urn:
            return binding.capsule.budget.tokens if binding.capsule is not None else None
    return None


def _readings(records: tuple[LedgerRecord, ...], run: Run) -> list[UsagePayload]:
    """Return the Run's usage readings that are part of its event stream.

    Only the usage payload is validated rather than the whole event line:
    the full event model costs the statusline's cold start more than its
    whole budget, and the line itself was validated as a ledger record.
    """
    run_ref = str(run.urn)
    return [
        UsagePayload.model_validate(line["payload"])
        for line in (item.payload for item in records)
        if line.get("payload_kind") == "run_event"
        and line.get("run_ref") == run_ref
        and line.get("quarantine") is None
        and isinstance(line.get("payload"), dict)
        and line["payload"].get("payload_kind") == "usage"
    ]


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``budget:<spent>/<cap>`` segment for this session's Run.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for ``session_id``.
        state_path: Resolved ``.ea/state.json`` path, or ``None``.

    Returns:
        The spend-against-cap segment, or a ``budget:n/a(<reason>)`` marker
        naming why it cannot be drawn.
    """
    if state_path is None:
        return budget_unavailable_segment(NO_STATE, _RUN_SOURCE)
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return budget_unavailable_segment(document_path, _RUN_SOURCE)
    run = session_run(claude_payload, document_path)
    if isinstance(run, str):
        return budget_unavailable_segment(run, _RUN_SOURCE)
    try:
        records = read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN))
        cap = _token_cap(records, run)
        readings = _readings(records, run)
    except (OSError, ValueError, LedgerError) as exc:
        # pydantic's ValidationError is a ValueError.
        logger.debug(f"build run-ledger-unreadable error={exc}")
        return budget_unavailable_segment("run-ledger-unreadable", _RUN_SOURCE)
    if cap is None:
        return budget_unavailable_segment("no-token-cap", _RUN_SOURCE)
    spent = aggregate_usage(readings).tokens
    if spent is None:
        return budget_unavailable_segment("no-usage-reading", _RUN_SOURCE)
    worst = max((reading.measurement_quality for reading in readings), key=_RANK.index)
    source = SegmentSource(
        producer=RUN_LEDGER_PRODUCER,
        provenance=str(run.urn),
        revision=run.revision,
        truth_kind=TruthKind.DERIVED,
        precision=Precision.APPROXIMATE,
        quality=_QUALITY[worst],
    )
    return budget_segment(spent=spent, limit=cap, notice_open=spent >= cap, source=source)


__all__ = ["RUN_LEDGER_PRODUCER", "build"]
