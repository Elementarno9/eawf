"""Refuse any lifecycle record built from a budget signal.

A budget crossing is an informational notice: it blocks nothing and asks
the operator nothing. Modelling one as an open question, a pending action
or a hold would turn an arithmetic fact -- consumption passed a fraction
of an estimate -- into something that can gate work, which is exactly
the coupling the notice record exists to prevent. Each of those records
therefore runs :func:`refuse_budget_signal` before its own validation, so
feeding it a budget event fails with a message naming the mistake rather
than a generic unknown-field refusal a later edit could quietly admit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel

#: Field names only a budget signal carries. Any input spelling one of them
#: is a budget event or notice, whatever record the caller meant to build.
BUDGET_SIGNAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "notice_key",
        "band",
        "highest_band",
        "basis",
        "axis",
        "observed_value",
        "budget_value",
        "cap_tokens",
        "observed_tokens",
    }
)


def refuse_budget_signal(data: object, *, target: str) -> None:
    """Raise when *data* is shaped like a budget signal.

    Args:
        data: The raw input a record's ``mode="before"`` validator received:
            a mapping of field values or another model instance.
        target: The record being built, named in the refusal.

    Raises:
        ValueError: *data* carries a budget-signal field. Raised inside a
            Pydantic validator, it surfaces as a ``ValidationError``.
    """
    if isinstance(data, BaseModel):
        names: set[str] = set(type(data).model_fields)
    elif isinstance(data, Mapping):
        names = {key for key in data if isinstance(key, str)}
    else:
        return
    found = sorted(names & BUDGET_SIGNAL_FIELDS)
    if found:
        raise ValueError(
            f"a budget signal is never {target}; it is a non-blocking notice "
            f"(budget fields: {', '.join(found)})"
        )


__all__ = ["BUDGET_SIGNAL_FIELDS", "refuse_budget_signal"]
