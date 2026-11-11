"""``cost`` statusline module — the session cost the host reports.

Claude Code's statusline payload carries the session's running cost under
``cost.total_cost_usd``. The segment renders it as ``cost:$<usd> API-equivalent``.
The host prices usage at list rates on its side, so the figure is what the API
would charge, not the bill, and the label and the segment's truth field say so.

The module reads only the host payload; a cost the host did not report
renders ``cost:n/a(no-cost-reported)``, never ``$0.00``.

:func:`build_active` reads the same ``cost`` block for time: the session's
wall-clock duration and the share of it spent waiting on the API, rendered
``active:<wall> · <p>% active``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.kernel.projection.truth import Precision, TruthKind
from eawf.kernel.state.enums import MeasurementQuality
from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    COST_LABEL,
    StatuslineSegment,
    round_half_up,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "cost"
_SOURCE = host_source(
    "cost.total_cost_usd",
    truth_kind=TruthKind.DERIVED,
    precision=Precision.APPROXIMATE,
    quality=MeasurementQuality.ESTIMATED,
)
_ACTIVE_SOURCE = host_source(
    "cost.total_api_duration_ms,cost.total_duration_ms",
    truth_kind=TruthKind.DERIVED,
    precision=Precision.APPROXIMATE,
)
_MS_PER_MINUTE = 60_000


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``cost:$<usd> API-equivalent`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — the module reads only from the host payload.

    Returns:
        The cost segment, rounded to the cent.
    """
    del state_path  # accepted for uniform signature
    counters = parse_runtime_counters(claude_payload)
    if counters is None or counters.cost_usd is None:
        return unavailable_segment(_MODULE, _MODULE, "no-cost-reported", _SOURCE)
    return sourced_segment(_MODULE, _MODULE, f"${counters.cost_usd:.2f} {COST_LABEL}", _SOURCE)


def _wall(total_ms: int) -> str:
    """Return a duration as ``<m>m`` under an hour and ``<h>h<mm>m`` from one."""
    hours, minutes = divmod(total_ms // _MS_PER_MINUTE, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def build_active(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``active:<wall> · <p>% active`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused; the module reads only from the host payload.

    Returns:
        The session's wall-clock time and the share of it spent on the API,
        rounded half up and capped at 100. The share needs the API time and a
        session of at least a minute, since a few seconds give a share that
        swings on every call; without it the wall time stands alone.
        ``active:n/a(no-duration)`` when the session's duration is missing
        or zero.
    """
    del state_path  # accepted for uniform signature
    counters = parse_runtime_counters(claude_payload)
    api_ms = counters.api_duration_ms if counters is not None else None
    total_ms = counters.total_duration_ms if counters is not None else None
    if not total_ms:
        return unavailable_segment("active_time", "active", "no-duration", _ACTIVE_SOURCE)
    value = _wall(total_ms)
    if api_ms is not None and total_ms >= _MS_PER_MINUTE:
        value = f"{value} · {min(100, round_half_up(100 * api_ms / total_ms))}% active"
    return sourced_segment("active_time", "active", value, _ACTIVE_SOURCE)


__all__ = ["build", "build_active"]
