"""``budget`` statusline module — the active wave's spend against its one ceiling.

The ceiling is the one the daemon meters, notices and enforces against:
:meth:`~eawf.runtime.budget.policy.BudgetConfig.ceiling` over the repo's
validated ``flow.budget`` table. An open limit-reached notice for the same
wave, read from the notice ledger beside ``state.json``, marks the segment.

The state file is read with :mod:`orjson` rather than validated, for the
same cold-path reason the ``state`` module gives. Every failure degrades to
an explicit ``budget:n/a(<reason>)`` marker rather than a guessed number.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.policy import DuplicateCeilingError
from eawf.runtime.budget.service import load_budget_config
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    budget_segment,
    budget_unavailable_segment,
)

logger = logging.getLogger(__name__)


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
    """Return whether *wave_id* has an open budget notice on file."""
    ledger = load_notice_ledger(notices_path(state_path))
    return any(
        notice.scope_id == wave_id and notice.status == "OPEN" for notice in ledger.notices.values()
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
    if state_path is None or not state_path.exists():
        return budget_unavailable_segment("no-state")
    try:
        payload = orjson.loads(state_path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"build state-read-decode-failed error={exc}")
        return budget_unavailable_segment("state-unreadable")
    active = _active_wave(payload) if isinstance(payload, dict) else None
    if active is None:
        return budget_unavailable_segment("no-active-wave")
    wave_id, row = active
    budget = row.get("token_budget")
    spent = row.get("tokens_consumed")
    if not isinstance(budget, int) or not isinstance(spent, int) or budget < 0 or spent < 0:
        return budget_unavailable_segment("no-budget")
    try:
        ceiling = load_budget_config(state_path.parent.parent).ceiling(budget)
    except DuplicateCeilingError:
        return budget_unavailable_segment("duplicate-ceiling")
    except (OSError, ValueError) as exc:
        logger.debug(f"build budget-config-invalid error={exc}")
        return budget_unavailable_segment("config-invalid")
    if ceiling is None:
        return budget_unavailable_segment("no-budget")
    try:
        notice_open = _notice_open(state_path, wave_id)
    except (OSError, ValidationError) as exc:
        logger.debug(f"build notice-ledger-unreadable error={exc}")
        return budget_unavailable_segment("notices-unreadable")
    return budget_segment(spent=spent, limit=ceiling.tokens, notice_open=notice_open)


__all__ = ["build"]
