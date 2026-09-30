"""Who may answer a pending action, and what each of them has done with it so far.

An action more than one principal may answer is drawn with an eligible pane on its
consequence card before anybody acts: one row per principal, their authority class, when
they last acted and their own disposition, and the rule that the first answer wins. The
pane is the only place the multiple-eligible fact lives; no attention count moves for it,
because eligibility grants no ownership and no queue position.

Every cell is one principal's own. A principal who has never acted on the action has an
empty cell, never the unavailable token: a disposition nobody exercised is not a value a
producer failed to supply.

The principals are the ones the held register names -- the operator the console acts as,
every principal an item is addressed to, and every principal an action records a
disposition for. A principal the tree has never mentioned cannot be listed, because no
roster of principals exists to read them from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Final

from eawf.kernel.projection.attention import CONSOLE_PRINCIPAL_CLASS
from eawf.kernel.projection.compute import ACTED_FACT, ANSWERED_FACT, SNOOZED_FACT, ProjectionRow
from eawf.surfaces.tui.console.tokens import truth_cell
from eawf.surfaces.tui.console.width import pad

#: The pane's closing rule: whoever answers first seals it for everybody.
FIRST_ANSWER_WINS: Final = "first answer wins · one answer seals it for every principal"

#: The legend under the matrix, so an empty cell is never read as a missing value.
LEGEND: Final = "every cell is per principal · an empty cell means never acted"

_PREFIXES: Final = (ANSWERED_FACT, SNOOZED_FACT, ACTED_FACT)
_WHO_W: Final = 14
_CLASS_W: Final = 10
_WHEN_W: Final = 12


def principals_of(rows: Iterable[ProjectionRow], principal: str | None) -> frozenset[str]:
    """Return every principal the held register names, the console's own among them.

    Args:
        rows: The held Attention register's rows.
        principal: Who the console acts as, or ``None``.
    """
    named = {principal} if principal else set()
    for row in rows:
        if row.assignee_ref:
            named.add(row.assignee_ref)
        named.update(
            name.removeprefix(prefix)
            for name in row.facts
            for prefix in _PREFIXES
            if name.startswith(prefix)
        )
    return frozenset(named)


def _state(facts: Mapping[str, str], who: str) -> str | None:
    """Return ``who``'s own disposition of the action, or ``None`` when they never acted."""
    answered = facts.get(f"{ANSWERED_FACT}{who}")
    if answered:
        return answered.replace(" ", " · ", 1)
    snoozed = facts.get(f"{SNOOZED_FACT}{who}")
    return f"snoozed to {_clock(snoozed)}" if snoozed else None


def _clock(stated: str | None) -> str | None:
    """Return a stated instant as UTC hours and minutes, or ``None`` when none parses."""
    if not stated:
        return None
    try:
        return f"{datetime.fromisoformat(stated):%H:%M} UTC"
    except ValueError:
        return None


def eligible_pane(row: ProjectionRow, principals: frozenset[str], you: str) -> tuple[str, ...]:
    """Return the eligible pane of ``row``, or nothing when one principal alone may answer.

    Args:
        row: The pending action as the register holds it.
        principals: Every principal the held register names.
        you: The principal the console acts as; their row is marked.

    Returns:
        The column heads, one row per principal in key order, the first-answer rule and
        the legend; empty when fewer than two principals are named.
    """
    if len(principals) < 2:
        return ()
    heads = pad("PRINCIPAL", _WHO_W) + pad("CLASS", _CLASS_W) + pad("LAST ACTED", _WHEN_W)
    lines = [f"{heads}STATE"]
    for who in sorted(principals):
        name = f"{who} (you)" if who == you else who
        when = _clock(row.facts.get(f"{ACTED_FACT}{who}"))
        lines.append(
            pad(name, _WHO_W)
            + pad(CONSOLE_PRINCIPAL_CLASS, _CLASS_W)
            + pad(when or truth_cell(None), _WHEN_W)
            + (_state(row.facts, who) or truth_cell(None))
        )
    return (*lines, FIRST_ANSWER_WINS, LEGEND)


__all__ = ["FIRST_ANSWER_WINS", "LEGEND", "eligible_pane", "principals_of"]
