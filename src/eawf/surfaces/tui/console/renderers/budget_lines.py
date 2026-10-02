"""How a budget reads: spent, the limit, and an estimated remainder -- never a countdown.

A token, cost or rate-limit budget reads as what was spent, the limit it is spent
against, and what is left carrying ``≈``, because the remainder is a projection and not
a promise. A time budget reads as the elapsed time, the limit and the typical duration
of the same kind of Run, carrying ``~``; it never reads as a remaining time, which is a
countdown an operator would watch. No budget line carries a threshold word, a bar or a
warning colour: it is a fact in the pane, and a spend no reading priced reads
``∅ unmetered``, never zero.
"""

from __future__ import annotations

from typing import Final

from eawf.kernel.state.epoch2.campaign import BudgetAxis
from eawf.surfaces.tui.console.format import group, span

#: The marker a projected remainder carries.
ESTIMATED: Final = "≈"

#: The marker a derived or estimated figure carries.
APPROXIMATE: Final = "~"

#: What a spend no reading priced reads as.
UNMETERED: Final = "∅ unmetered"

_MICROUSD_PER_USD: Final = 1_000_000


def money(microusd: int) -> str:
    """Return a cost in dollars to the cent, as in ``4.62``."""
    return f"{microusd / _MICROUSD_PER_USD:,.2f}"


def spent_of(spent: str, limit: str | None, left: str | None) -> str:
    """Return ``spent of limit · ≈left left``, or ``spent · no limit is set`` without one.

    Args:
        spent: The spend, already formatted with its unit and any marker.
        limit: The limit, formatted, or ``None`` when none binds this basis.
        left: The remainder, formatted, or ``None`` when it cannot be projected.
    """
    if limit is None:
        return f"{spent} · no limit is set"
    tail = f" · {ESTIMATED}{left} left" if left is not None else ""
    return f"{spent} of {limit}{tail}"


def tokens_line(
    spent: int | None, limit: int | None, *, held: int | None = None, estimated: bool = False
) -> str:
    """Return a token budget: spent, the limit and the estimated remainder.

    Args:
        spent: Tokens spent, or ``None`` when no reading counted any.
        limit: The token limit, or ``None`` when none is set.
        held: What stands against the limit when it is more than the spend -- the
            reservations live Runs hold -- or ``None`` when the spend is all that does.
        estimated: Whether the spend itself is derived rather than measured.
    """
    if spent is None:
        shown = "tokens ∅ no reading yet"
    else:
        shown = f"tokens {APPROXIMATE if estimated else ''}{group(spent)}"
    against = held if held is not None else spent
    left = None if limit is None or against is None else group(max(0, limit - against))
    return spent_of(shown, None if limit is None else group(limit), left)


def cost_line(spent: int | None, limit: int | None, *, partial: bool = False) -> str:
    """Return a cost budget, or the unmetered mark when no reading priced the spend.

    Args:
        spent: Cost spent in micro-dollars, or ``None`` when unpriced.
        limit: The cost limit in micro-dollars, or ``None`` when none is set.
        partial: Whether some of the spend went unpriced, so *spent* is a floor
            and no remainder can be projected from it.
    """
    cap = f" of {money(limit)}" if limit is not None else " · no limit is set"
    if spent is None:
        return f"cost {UNMETERED}{cap}"
    if partial:
        return f"cost ≥{money(spent)}{cap} · partly unmetered"
    left = None if limit is None else money(max(0, limit - spent))
    return spent_of(f"cost {money(spent)}", None if limit is None else money(limit), left)


def axis_line(axis: BudgetAxis) -> str:
    """Return one Campaign budget axis: spent of its limit, and an estimated remainder.

    The axis kind is named only when its unit does not already say it, so a rounds axis
    reads ``3 of 7 rounds`` and a wall-time axis ``wall time 1 of 6 h``.

    Args:
        axis: The axis pair as the Campaign record holds it.

    Returns:
        The line; an axis no reading observed reads ``∅ unmetered``, and one some
        reading missed reads as a floor with no remainder.
    """
    kind = axis.axis_kind.replace("_", " ")
    head = "" if kind == axis.unit else f"{kind} "
    limit = f"{group(axis.limit)} {axis.unit}"
    soft = "" if axis.hard else " · soft"
    if axis.spent_quality == "unavailable" and axis.spent == 0:
        return f"{head}{UNMETERED} of {limit}{soft}"
    if axis.spent_quality == "unavailable":
        return f"{head}≥{group(axis.spent)} of {limit} · partly unmetered{soft}"
    mark = "" if axis.spent_quality == "measured" else APPROXIMATE
    left = group(max(0, axis.limit - axis.spent))
    return spent_of(f"{head}{mark}{group(axis.spent)}", limit, left) + soft


def time_line(elapsed: str, limit: int | None, typical: int | None) -> str:
    """Return a time budget: elapsed, the limit and the typical duration, never a remainder.

    Args:
        elapsed: The elapsed words the Run frame already states, as in ``elapsed 4m``.
        limit: The wall-clock limit in seconds, or ``None`` when none is sealed.
        typical: The median duration of the same kind of Run, or ``None`` with none.
    """
    text = elapsed if limit is None else f"{elapsed} of {span(limit)}"
    if typical is None:
        return f"{text} · typical ∅ no Run of this kind has completed"
    return f"{text} · typical {APPROXIMATE}{span(typical)}"


__all__ = [
    "APPROXIMATE",
    "ESTIMATED",
    "UNMETERED",
    "axis_line",
    "cost_line",
    "money",
    "spent_of",
    "time_line",
    "tokens_line",
]
