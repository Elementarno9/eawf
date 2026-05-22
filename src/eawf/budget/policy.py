"""Pure-function token-budget thresholds + enforcement classification.

Two layers live here, both I/O-free and state-free:

* **Advisory thresholds** (pre-existing): warn at 75 %, block at 100 %.
  :func:`classify` returns ``None`` below the warn band, :data:`WARN_TAG`
  in the warn band, and :data:`BLOCK_TAG` at or above the cap. The
  classifier short-circuits to ``None`` when no budget is configured so
  callers treat a missing budget as a no-op.

* **Enforcement decision** (this wave): given a ``(consumed, base
  budget)`` pair, the configured enforce mode, and the cap multiplier,
  :func:`classify_enforcement` returns a typed :class:`BudgetDecision`.
  ``soft`` (the default) always continues — it only flips its action to
  :data:`BudgetAction.WARN` once consumption crosses the multiplier-scaled
  cap. ``hard`` is opt-in: it returns :data:`BudgetAction.HALT` at the
  same cap so the dispatcher can run the SIGTERM -> SIGKILL ladder
  (:func:`eawf.budget.service.terminate_with_grace`).

No I/O. No state mutation. The CLI / daemon layer owns persistence and
the actual process signalling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

WARN_FRACTION: float = 0.75
BLOCK_FRACTION: float = 1.0

WARN_TAG: str = "warn:75-percent"
BLOCK_TAG: str = "block:over-budget"

#: Enforcement modes for ``flow.budget.enforce``. ``soft`` (default) warns
#: and lets the wave continue past its cap; ``hard`` is opt-in and halts
#: the wave at the cap.
EnforceMode = Literal["soft", "hard"]

#: Default for ``flow.budget.enforce``. Soft = advisory-only so the wave
#: never dies on a budget overshoot unless the operator opts in.
DEFAULT_ENFORCE: EnforceMode = "soft"

#: Default for ``flow.budget.multiplier``. The base budget is scaled by
#: this factor to derive the enforced cap (1.5 == 50 % safety margin).
DEFAULT_MULTIPLIER: float = 1.5


class BudgetAction(StrEnum):
    """The action a caller should take for a classified budget state.

    ``CONTINUE`` — under the cap; proceed.
    ``WARN`` — at or over the cap under ``soft`` enforce; emit the
    overshoot warning but let the wave run.
    ``HALT`` — at or over the cap under ``hard`` enforce; stop the wave
    (the dispatcher runs the SIGTERM -> SIGKILL ladder).
    """

    CONTINUE = "continue"
    WARN = "warn"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Typed enforcement verdict for a ``(consumed, cap)`` pair.

    Attributes:
        action: What the caller should do — see :class:`BudgetAction`.
        enforce: The enforce mode the decision was computed under.
        cap: The effective (multiplier-scaled) cap consumption was tested
            against, or ``None`` when no base budget was configured.
        consumed: The consumption the decision was computed for.
        over_cap: Whether ``consumed`` met or exceeded ``cap``. Always
            ``False`` when ``cap`` is ``None``.
    """

    action: BudgetAction
    enforce: EnforceMode
    cap: int | None
    consumed: int
    over_cap: bool


def classify(consumed: int, budget: int | None) -> str | None:
    """Classify a (consumed, budget) pair against the advisory thresholds.

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


def effective_cap(base_budget: int | None, multiplier: float) -> int | None:
    """Scale *base_budget* by *multiplier* to derive the enforced cap.

    The scaled value is floored to an integer token count (a fractional
    token is not spendable). A ``None`` base budget passes through as
    ``None`` so the absence of a budget stays a no-op for the caller.

    Args:
        base_budget: The wave's configured budget, or ``None``.
        multiplier: The ``flow.budget.multiplier`` factor (must be > 0).

    Returns:
        The floored ``base_budget * multiplier``, or ``None``.

    Raises:
        ValueError: ``multiplier`` is not strictly positive.
    """
    if multiplier <= 0:
        raise ValueError(f"multiplier must be positive; got {multiplier!r}")
    if base_budget is None:
        return None
    return int(base_budget * multiplier)


def classify_enforcement(
    consumed: int,
    base_budget: int | None,
    *,
    enforce: EnforceMode = DEFAULT_ENFORCE,
    multiplier: float = DEFAULT_MULTIPLIER,
) -> BudgetDecision:
    """Classify a consumption against the multiplier-scaled enforced cap.

    The base budget is first scaled by *multiplier* (:func:`effective_cap`).
    Consumption strictly below the scaled cap is :data:`BudgetAction.CONTINUE`
    under either mode. At or above the cap the verdict diverges by mode:
    ``soft`` returns :data:`BudgetAction.WARN` (advise, keep running);
    ``hard`` returns :data:`BudgetAction.HALT` (stop the wave). When no
    base budget is configured the verdict is always
    :data:`BudgetAction.CONTINUE` with a ``None`` cap.

    Args:
        consumed: Cumulative tokens spent so far (>= 0).
        base_budget: The wave's configured budget, or ``None`` for no cap.
        enforce: Enforce mode — ``soft`` (default) or ``hard``.
        multiplier: Cap multiplier (default :data:`DEFAULT_MULTIPLIER`).

    Returns:
        A :class:`BudgetDecision` describing the action, the cap tested
        against, and whether the cap was exceeded.

    Raises:
        ValueError: ``consumed`` is negative, or ``multiplier`` is not
            strictly positive.
    """
    if consumed < 0:
        raise ValueError(f"consumed must be non-negative; got {consumed}")
    cap = effective_cap(base_budget, multiplier)
    if cap is None:
        return BudgetDecision(
            action=BudgetAction.CONTINUE,
            enforce=enforce,
            cap=None,
            consumed=consumed,
            over_cap=False,
        )
    over_cap = consumed >= cap
    if not over_cap:
        action = BudgetAction.CONTINUE
    elif enforce == "hard":
        action = BudgetAction.HALT
    else:
        action = BudgetAction.WARN
    return BudgetDecision(
        action=action,
        enforce=enforce,
        cap=cap,
        consumed=consumed,
        over_cap=over_cap,
    )
