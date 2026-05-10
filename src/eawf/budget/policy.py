"""Pure-function token-budget thresholds.

The policy is intentionally tiny: warn at 75 %, block at 100 %. Anything
above is still a block — there is no "over-block" tier in v0.1. The
classifier returns ``None`` when no budget is configured so callers can
treat the absence of a budget as a no-op.

No I/O. No state mutation. The CLI layer is responsible for persistence
and for surfacing the returned classification string to the operator.
"""

from __future__ import annotations

WARN_FRACTION: float = 0.75
BLOCK_FRACTION: float = 1.0

WARN_TAG: str = "warn:75-percent"
BLOCK_TAG: str = "block:over-budget"


def classify(consumed: int, budget: int | None) -> str | None:
    """Classify a (consumed, budget) pair against the v0.1 thresholds.

    Args:
        consumed: Cumulative tokens spent so far on the wave (>= 0).
        budget: The wave's allotted budget, or ``None`` when no budget
            has been configured.

    Returns:
        ``None`` when no budget is set or consumption is below the warn
        threshold (75 %). :data:`WARN_TAG` between warn and block. :data:`BLOCK_TAG`
        at or above 100 %.
    """
    if budget is None:
        return None
    if budget <= 0:
        # A zero or negative budget cannot be under-spent. Any positive
        # consumption is over-budget; zero consumption is technically at
        # 100 % and we report block.
        return BLOCK_TAG
    fraction = consumed / budget
    if fraction >= BLOCK_FRACTION:
        return BLOCK_TAG
    if fraction >= WARN_FRACTION:
        return WARN_TAG
    return None
