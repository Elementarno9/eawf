"""Bounded gate-kind rewrite for the recorded gates of a CLOSED wave.

Some waves closed with a verification record that cannot prove anything:
their criteria were converted into single-token ``criterion_in_diff``
greps, or they were authored at phase close with attested criteria and no
gate at all. Re-running such a record produces a receipt, but not proof --
a grep for one word passes whether or not the behaviour exists.

An argv repoint cannot help:
it moves argv and nothing else, by design. This module is the one other
degree of freedom, and it only points one way. A grep-style gate may
become a ``command_exit_zero`` gate, and a criterion with no gate may gain
one. Nothing may move the other way: a command gate is never re-kinded, a
target kind other than ``command_exit_zero`` is refused, and the criterion
that owns a rewritten gate is only ever promoted to ``deterministic``.

The guard is a fingerprint, as in the argv repoint. The whole wave row is
dumped with only the touched gates and the four proof fields of their
owning criteria (``gate_ids``, ``evidence_kind``, ``response``,
``oracle_tier``) elided; if anything else differs after the rewrite, the
mutation is rolled back and refused. The criterion text, the outcome,
``closed_at``, the commit pin and every untouched gate stay byte-equal.
"""

from __future__ import annotations

import logging
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: The only kind a rewrite may produce. A command gate runs the behaviour
#: the criterion describes, which is what every grep kind only guesses at.
COMMAND_GATE_KIND: Final[str] = "command_exit_zero"

#: Gate kinds that read a file for a token or a path and so can pass
#: without the behaviour existing. Only these may be rewritten.
GREP_GATE_KINDS: Final[frozenset[str]] = frozenset(
    {"criterion_in_diff", "regex_in_file", "path_glob_nonempty", "file_exists"}
)


class GateKindRewrite(BaseModel):
    """One requested strengthening of a single gate.

    Attributes:
        gate_id: A recorded grep-style gate to rewrite, or a new gate id
            to add to a criterion that has no command gate.
        criterion_id: The owning criterion. Required for a new gate; for
            a recorded gate it must match the recorded binding when given.
        kind: The target kind. Only ``command_exit_zero`` is accepted; the
            field exists so a weakening request is refused by name rather
            than being unexpressible.
        argv: The command the gate runs, checked by the L0 argv policy.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str = Field(min_length=1)
    criterion_id: str | None = None
    kind: str = COMMAND_GATE_KIND
    argv: list[str] = Field(min_length=1)


class GateKindChange(BaseModel):
    """The applied before/after pair for one rewritten or added gate."""

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    criterion_id: str
    before_kind: str | None
    after_kind: str
    after_argv: list[str]


__all__ = [
    "COMMAND_GATE_KIND",
    "GREP_GATE_KINDS",
    "GateKindChange",
    "GateKindRewrite",
]
