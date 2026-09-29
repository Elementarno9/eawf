"""``budget`` statusline module — the active wave's spend against its one ceiling.

The ceiling is the one the daemon meters, notices and enforces against:
:meth:`~eawf.runtime.budget.policy.BudgetConfig.ceiling` over the repo's
validated ``flow.budget`` table. An open limit-reached notice for the same
wave, read from the notice ledger beside ``state.json``, marks the segment.

The wave's budget and spend are epoch-1 document fields, read through
:func:`~._document.read_legacy_document`, so on an epoch-2 tree the segment
names ``no-epoch2-source``. Every failure degrades to an explicit
``budget:n/a(<reason>)`` marker rather than a guessed number.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.projection.truth import Precision
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.policy import DuplicateCeilingError
from eawf.runtime.budget.service import load_budget_config
from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    budget_segment,
    budget_unavailable_segment,
)

logger = logging.getLogger(__name__)

# The stored counts are exact; the rendered figures are rounded to a tenth of a thousand.
_SOURCE = replace(document_source("waves.token_budget"), precision=Precision.APPROXIMATE)


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


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``budget:<spent>/<limit>`` segment for the active wave.

    Args:
        claude_payload: Decoded Claude stdin JSON; unused, kept for the
            uniform module signature.
        state_path: Resolved ``.ea/state.json`` path, or ``None``.

    Returns:
        The spend-against-ceiling segment, or a ``budget:n/a(<reason>)``
        marker naming why it cannot be drawn.
    """
    del claude_payload
    if state_path is None:
        return budget_unavailable_segment(DocumentGap.NO_STATE.value, _SOURCE)
    payload = read_legacy_document(state_path)
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


__all__ = ["build"]
