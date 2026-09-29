"""``cost`` statusline module — the session cost the host reports.

Claude Code's statusline payload carries the session's running cost under
``cost.total_cost_usd``. The segment renders it as ``cost:$<usd>``. The host
prices usage at list rates on its side, so the figure is an estimate of the
bill rather than the bill, and the segment's truth field says so.

The module reads only the host payload; a cost the host did not report
renders ``cost:n/a(no-cost-reported)``, never ``$0.00``.
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
    StatuslineSegment,
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


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``cost:$<usd>`` segment, or the marker naming why not.

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
    return sourced_segment(_MODULE, _MODULE, f"${counters.cost_usd:.2f}", _SOURCE)


__all__ = ["build"]
