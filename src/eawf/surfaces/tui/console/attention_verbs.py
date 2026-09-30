"""What an attention verb sends for a held pending action, or why it is refused.

An answer or a denial seals the action for every principal. A snooze hides it from the
operator alone for a while and answers nothing. An assignment addresses it to the one
other principal the register names, and is refused while there is nobody else. Resolve is
refused on a pending action: it closes only by its answer, because an action resolved
with no option chosen would be the answer nobody gave.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.state.epoch2.consequence import Refusal
from eawf.surfaces.tui.console.operations import (
    ANSWER_OPTIONS,
    ATTENTION_ROUTE,
    SNOOZE_FOR,
    ActionDisposition,
    AnswerRequest,
    VerbRequest,
    binding_refusal,
)

#: Why ``assign`` is refused while the register names no principal but this one.
ONLY_PRINCIPAL: Final = "you are the only principal"

#: The fixed consequence of each attention verb that does not answer, by verb name.
_ACTION_EFFECTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "snooze": "hidden for you only — other principals still see it",
        "assign": "addressed to {assignee} — anyone eligible may still answer",
    }
)


def action_request(
    row: ProjectionRow, name: str, *, principal: str, known: frozenset[str], wall: datetime
) -> tuple[VerbRequest | None, Refusal | None, tuple[str, ...]]:
    """Return what one attention verb sends for ``row``, or why it is refused, and its effects.

    Args:
        row: The pending action as the Attention register holds it.
        name: The attention verb's name.
        principal: Who the console acts as.
        known: Every principal the held register names.
        wall: The wall-clock instant a snooze is measured from.

    Returns:
        The request to send, or ``None``; the refusal, or ``None``; and the effects.
    """
    option = ANSWER_OPTIONS.get(name)
    effects: tuple[str, ...]
    if option is not None:
        effects = (
            f"{row.key} is sealed {option!r} in your name, under your evidence receipt",
            "the question closes for every principal; a later answer is superseded",
        )
        return AnswerRequest(target=row.key, option_id=option), None, effects
    if name == "snooze":
        until = wall + SNOOZE_FOR
        effects = (_ACTION_EFFECTS[name], f"{row.key} returns to your count at {until:%H:%M} UTC")
        return ActionDisposition(target=row.key, verb="snooze", snooze_until=until), None, effects
    if name == "assign":
        others = sorted(known - {row.assignee_ref or principal, principal})
        if not others:
            refusal = Refusal(
                code="no_other_principal",
                reason=ONLY_PRINCIPAL,
                remediation="Answer it yourself, or wait until another principal is named.",
            )
            return None, refusal, ()
        effects = (_ACTION_EFFECTS[name].format(assignee=others[0]),)
        return ActionDisposition(target=row.key, verb="assign", assignee=others[0]), None, effects
    why = binding_refusal(ATTENTION_ROUTE, name)
    return None, Refusal(code="unbound_verb", reason=why, remediation="Answer or deny instead."), ()


__all__ = ["ONLY_PRINCIPAL", "action_request"]
