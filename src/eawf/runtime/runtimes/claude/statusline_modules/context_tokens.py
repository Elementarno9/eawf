"""``context_tokens`` statusline module — context-window occupancy.

Claude Code's statusline payload carries the last call's usage under
``context_window.current_usage`` and the window size under
``context_window.context_window_size``. The segment renders the tokens the
context holds (input plus both cache classes) against that size as
``ctx:<used>/<size>``, or ``ctx:<used>`` when the host names no size.

The module reads only the host payload — no transcript, no on-disk lookup —
so the segment is cheap and never disagrees with what the host shows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.kernel.projection.truth import Precision
from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)
from eawf.surfaces.render.units import format_tokens

logger = logging.getLogger(__name__)

_MODULE = "context_tokens"
_LABEL = "ctx"
# The counts are exact; the rendered figure is rounded to a tenth of a thousand.
_SOURCE = host_source("context_window.current_usage", precision=Precision.APPROXIMATE)


def _window_size(claude_payload: dict[str, Any]) -> int | None:
    """Return the host's context-window size, or ``None`` when it names none."""
    window = claude_payload.get("context_window")
    size = window.get("context_window_size") if isinstance(window, dict) else None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``ctx:<used>/<size>`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — the module reads only from the host payload.

    Returns:
        The occupancy segment; ``ctx:n/a(no-current-usage)`` when the host has
        not reported a call yet, ``ctx:n/a(partial-usage)`` when it reports only
        some of the three input classes, since a missing class is not a zero.
    """
    del state_path  # accepted for uniform signature
    counters = parse_runtime_counters(claude_payload)
    classes = (
        (
            counters.input_tokens,
            counters.cache_creation_input_tokens,
            counters.cache_read_input_tokens,
        )
        if counters is not None
        else (None, None, None)
    )
    present = [count for count in classes if count is not None]
    if not present:
        return unavailable_segment(_MODULE, _LABEL, "no-current-usage", _SOURCE)
    if len(present) != len(classes):
        return unavailable_segment(_MODULE, _LABEL, "partial-usage", _SOURCE)
    used = format_tokens(sum(present))
    size = _window_size(claude_payload)
    value = used if size is None else f"{used}/{format_tokens(size)}"
    return sourced_segment(_MODULE, _LABEL, value, _SOURCE)


__all__ = ["build"]
