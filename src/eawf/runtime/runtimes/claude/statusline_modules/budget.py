"""``budget`` statusline module — the active scope's spend against its one ceiling.

On an epoch-1 tree the scope is the active wave: its budget and spend are
document fields, and the ceiling is the one the daemon meters, notices and
enforces against, :meth:`~eawf.runtime.budget.policy.BudgetConfig.ceiling`
over the repo's validated ``flow.budget`` table. An open limit-reached
notice for the same wave, read from the notice ledger beside ``state.json``,
marks the segment.

On an epoch-2 tree the scope is the Run bound to the host's session, and its
reading is the budget notice the daemon's in-flight meter filed for it: the
consumption it observed and the cap it tested against. The meter holds a
reading below the cap in memory only, so a Run that never reached its cap
has no stored reading and the segment says so rather than guessing one.

Every failure degrades to an explicit ``budget:n/a(<reason>)`` marker rather
than a guessed number.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.projection.truth import Precision, TruthKind
from eawf.runtime.budget.notices import (
    BudgetThresholdNotice,
    load_notice_ledger,
    notices_path,
)
from eawf.runtime.budget.policy import DuplicateCeilingError
from eawf.runtime.budget.service import load_budget_config
from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    SPINE_PRODUCER,
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

#: The producer an epoch-2 reading names: the notice ledger the in-flight meter files into.
NOTICE_PRODUCER: Final = "eawf.budget-notice-ledger"

# The stored counts are exact; the rendered figures are rounded to a tenth of a thousand.
_SOURCE = replace(document_source("waves.token_budget"), precision=Precision.APPROXIMATE)
_RUN_SOURCE = SegmentSource(
    producer=SPINE_PRODUCER, provenance="generation#run", truth_kind=TruthKind.STORED
)


def _active_wave(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return the first active wave id and its row, or ``None`` when there is none."""
    current = payload.get("current")
    waves = payload.get("waves")
    if not isinstance(current, dict) or not isinstance(waves, dict):
        return None
    active = current.get("active_wave_ids")
    if not isinstance(active, list) or not active:
        return None
    wave_id = active[0]
    row = waves.get(wave_id) if isinstance(wave_id, str) else None
    if not isinstance(row, dict):
        return None
    return wave_id, row


def _notice_open(state_path: Path, wave_id: str) -> bool:
    """Return whether *wave_id* has an open token-budget notice on file."""
    ledger = load_notice_ledger(notices_path(state_path))
    return any(
        notice.scope_id == wave_id and notice.axis == "tokens" and notice.status == "OPEN"
        for notice in ledger.notices.values()
    )


def _run_reading(
    notices: list[BudgetThresholdNotice], run_key: str
) -> BudgetThresholdNotice | None:
    """Return the latest token reading filed for *run_key*, or ``None``."""
    readings = [
        notice
        for notice in notices
        if notice.scope_id == run_key
        and notice.axis == "tokens"
        and notice.budget_value is not None
    ]
    return max(readings, key=lambda notice: notice.last_observed_at, default=None)


def _run_budget(claude_payload: dict[str, Any], state_path: Path) -> StatuslineSegment:
    """Return the session Run's last metered reading against its cap."""
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return budget_unavailable_segment(document_path, _RUN_SOURCE)
    run = session_run(claude_payload, document_path)
    if isinstance(run, str):
        return budget_unavailable_segment(run, _RUN_SOURCE)
    path = notices_path(state_path)
    try:
        ledger = load_notice_ledger(path)
    except (OSError, ValidationError) as exc:
        logger.debug(f"_run_budget notice-ledger-unreadable error={exc}")
        return budget_unavailable_segment("notices-unreadable", _RUN_SOURCE)
    notice = _run_reading(list(ledger.notices.values()), run.key)
    if notice is None or notice.budget_value is None:
        return budget_unavailable_segment("no-run-budget-reading", _RUN_SOURCE)
    source = SegmentSource(
        producer=NOTICE_PRODUCER,
        provenance=f"{path.relative_to(state_path.parent.parent).as_posix()}#{notice.notice_key}",
        revision=notice.revision,
        truth_kind=TruthKind.STORED,
        precision=Precision.APPROXIMATE,
    )
    return budget_segment(
        spent=notice.observed_value,
        limit=notice.budget_value,
        notice_open=notice.status == "OPEN",
        source=source,
    )


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``budget:<spent>/<limit>`` segment for the active scope.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for ``session_id`` on
            an epoch-2 tree.
        state_path: Resolved ``.ea/state.json`` path, or ``None``.

    Returns:
        The spend-against-ceiling segment, or a ``budget:n/a(<reason>)``
        marker naming why it cannot be drawn.
    """
    if state_path is None:
        return budget_unavailable_segment(DocumentGap.NO_STATE.value, _SOURCE)
    payload = read_legacy_document(state_path)
    if payload is DocumentGap.NO_EPOCH2_SOURCE:
        return _run_budget(claude_payload, state_path)
    if isinstance(payload, DocumentGap):
        return budget_unavailable_segment(payload.value, _SOURCE)
    active = _active_wave(payload)
    if active is None:
        return budget_unavailable_segment("no-active-wave", _SOURCE)
    wave_id, row = active
    budget = row.get("token_budget")
    spent = row.get("tokens_consumed")
    if not isinstance(budget, int) or not isinstance(spent, int) or budget < 0 or spent < 0:
        return budget_unavailable_segment("no-budget", _SOURCE)
    try:
        ceiling = load_budget_config(state_path.parent.parent).ceiling(budget)
    except DuplicateCeilingError:
        return budget_unavailable_segment("duplicate-ceiling", _SOURCE)
    except (OSError, ValueError) as exc:
        logger.debug(f"build budget-config-invalid error={exc}")
        return budget_unavailable_segment("config-invalid", _SOURCE)
    if ceiling is None:
        return budget_unavailable_segment("no-budget", _SOURCE)
    try:
        notice_open = _notice_open(state_path, wave_id)
    except (OSError, ValidationError) as exc:
        logger.debug(f"build notice-ledger-unreadable error={exc}")
        return budget_unavailable_segment("notices-unreadable", _SOURCE)
    return budget_segment(
        spent=spent, limit=ceiling.tokens, notice_open=notice_open, source=_SOURCE
    )


__all__ = ["NOTICE_PRODUCER", "build"]
