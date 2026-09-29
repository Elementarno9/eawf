"""``token_saving`` statusline module — prompt-cache hit ratio.

Computes ``cache_read / (cache_read + cache_creation + input)`` over the last
call's usage the host reports under ``context_window.current_usage`` and
renders ``save:<pct>%``, the share of the context not re-billed at the full
input rate. A payload without that usage renders ``save:n/a(<reason>)``.

The module reads only the host payload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.kernel.projection.truth import Precision, TruthKind
from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "token_saving"
_LABEL = "save"
_SOURCE = host_source(
    "context_window.current_usage",
    truth_kind=TruthKind.DERIVED,
    precision=Precision.APPROXIMATE,
)


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``save:<pct>%`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — kept for the uniform module signature.

    Returns:
        The ratio segment; ``save:n/a(no-current-usage)`` when the host reports
        no usage, ``save:n/a(partial-usage)`` when it omits one of the three
        input classes (a missing class is not a zero), and
        ``save:n/a(no-input-tokens)`` when every class is zero, so there is no
        ratio to take.
    """
    del state_path  # accepted for uniform signature
    counters = parse_runtime_counters(claude_payload)
    cache_read, cache_creation, plain = (
        (
            counters.cache_read_input_tokens,
            counters.cache_creation_input_tokens,
            counters.input_tokens,
        )
        if counters is not None
        else (None, None, None)
    )
    if cache_read is None and cache_creation is None and plain is None:
        return unavailable_segment(_MODULE, _LABEL, "no-current-usage", _SOURCE)
    if cache_read is None or cache_creation is None or plain is None:
        return unavailable_segment(_MODULE, _LABEL, "partial-usage", _SOURCE)
    total = cache_read + cache_creation + plain
    if total == 0:
        return unavailable_segment(_MODULE, _LABEL, "no-input-tokens", _SOURCE)
    return sourced_segment(_MODULE, _LABEL, f"{round(cache_read / total * 100)}%", _SOURCE)


__all__ = ["build"]
