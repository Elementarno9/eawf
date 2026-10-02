"""The spend views: a governor ceiling beside what is spent against it, and one Run's.

These are what the spend reads answer with and what the console renders; the folds
that build them over the run ledger live with the daemon. A figure no reading reported
is ``None``, never zero, so a renderer can say it was not read.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.runtime.usage import UsageQuality
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.types import UtcDatetime

#: Where the ceiling is owned: the governor field of the ``economics`` table.
CEILING_OWNER: Final = "economics.governor"

Pricing = Literal["priced", "partial", "unmetered"]


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSpend(_View):
    """What the Runs of one provider have spent.

    Attributes:
        provider: The provider the Runs were routed to.
        runs: How many Runs reported usage.
        tokens: Their tokens, or ``None`` when no reading counted any.
        cost_microusd: Their cost, or ``None`` when no reading priced any.
        pricing: ``priced`` when every Run's cost was read, ``unmetered`` when none
            was, ``partial`` between: a partial sum is a floor, not the spend.
    """

    provider: str
    runs: StrictPositiveInt
    tokens: StrictNonNegativeInt | None
    cost_microusd: StrictNonNegativeInt | None
    pricing: Pricing


class StoppedRun(_View):
    """A Run a hard limit stopped, or a crossing whose stop is not yet confirmed.

    Attributes:
        run_key: The Run.
        noticed_at: When the crossing was read.
        basis: What the ceiling was set on; a stop only happens at a hard limit.
        confirmed: Whether a confirmed control effect answers the crossing.
        observed_tokens: The reading that crossed.
        cap_tokens: The ceiling it crossed.
    """

    run_key: str
    noticed_at: UtcDatetime
    basis: Literal["hard_limit"] = "hard_limit"
    confirmed: bool
    observed_tokens: StrictNonNegativeInt
    cap_tokens: StrictNonNegativeInt


class CostCeilingView(_View):
    """The governor ceiling beside what the live Runs spend and hold against it.

    Attributes:
        owner: The configuration field that binds the ceiling.
        ceiling_tokens: The in-flight token ceiling, or ``None`` when unbound.
        ceiling_cost_microusd: The in-flight cost ceiling, or ``None`` when unbound.
        live_runs: How many admitted Runs are still live.
        spent_tokens: What the live Runs' readings say they spent: zero with none
            live, ``None`` when live Runs reported no tokens.
        held_tokens: What they hold against the ceiling -- each the larger of its
            reservation and its spend -- or ``None`` when one holds no cap.
        spent_cost_microusd: Their priced spend: zero with none live, ``None`` when
            none was priced.
        spent_cost_pricing: ``priced`` when every live Run's cost was read,
            ``unmetered`` when none was, ``partial`` between: a partial sum leaves
            the unpriced Runs out, so it is a floor under the spend.
        held_cost_microusd: What they hold in cost, or ``None`` when one is uncapped.
        stopped: The crossings a hard limit answered, newest first.
        providers: Spend by provider over every Run on the ledger.
    """

    owner: str = CEILING_OWNER
    ceiling_tokens: StrictNonNegativeInt | None
    ceiling_cost_microusd: StrictNonNegativeInt | None
    live_runs: StrictNonNegativeInt
    spent_tokens: StrictNonNegativeInt | None
    held_tokens: StrictNonNegativeInt | None
    spent_cost_microusd: StrictNonNegativeInt | None
    spent_cost_pricing: Pricing
    held_cost_microusd: StrictNonNegativeInt | None
    stopped: tuple[StoppedRun, ...] = ()
    providers: tuple[ProviderSpend, ...] = ()


class RunUsageView(_View):
    """What one Run has spent against the caps its binding sealed.

    Attributes:
        run_key: The Run.
        tokens: Its tokens, or ``None`` when no reading counted any.
        cost_microusd: Its cost, or ``None`` when no reading priced any.
        quality: The least trusted quality among its readings, or ``None`` with none.
        cap_tokens: The sealed token cap, or ``None`` when unbound or unsealed.
        cap_cost_microusd: The sealed cost cap, or ``None``.
        wall_seconds: The sealed wall-clock limit, or ``None`` when unsealed.
        typical_seconds: The median measured duration of completed Runs of the same
            purpose, or ``None`` when none completed with both stamps.
        typical_runs: How many durations the median was taken over.
    """

    run_key: str
    tokens: StrictNonNegativeInt | None
    cost_microusd: StrictNonNegativeInt | None
    quality: UsageQuality | None
    cap_tokens: StrictNonNegativeInt | None
    cap_cost_microusd: StrictNonNegativeInt | None
    wall_seconds: StrictNonNegativeInt | None
    typical_seconds: StrictNonNegativeInt | None
    typical_runs: StrictNonNegativeInt


__all__ = [
    "CEILING_OWNER",
    "CostCeilingView",
    "Pricing",
    "ProviderSpend",
    "RunUsageView",
    "StoppedRun",
]
