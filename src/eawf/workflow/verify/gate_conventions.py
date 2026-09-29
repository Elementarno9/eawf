"""The conventions a minted argv gate declares instead of inheriting defaults.

A ``command_exit_zero`` gate that omits its ``scope`` or ``timeout_class``
runs under the runner's defaults, and both defaults are wrong for a wave
gate. A scope that derives its diff base from a wave revision measures a
pending wave against something other than what it measures at close, so a
gate uses ``scope: all`` until the wave has a commit to derive a base
from. And the lean-verification rule sets the ceiling of a targeted run,
which is the ``quick`` class; a wave whose targeted run genuinely exceeds
it says so on the wave, once, rather than every gate drifting past it.

The other two conventions are about how many gates a criterion gets. A
criterion that asserts a repetition count is one test that loops
internally, and a half a single argv cannot observe is an assertion inside
that same test, so a criterion carries at most one wave-close argv gate. A
half whose truth is a continuous-integration result is a second gate at
``cadence: ship``, which must not block a wave that never runs the
pipeline, so it is authored non-required.

These are authoring rules, checked where a spec is promoted to ``READY``;
the close runner reads the gates they produce.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from typing import Final

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.promotion import ARGV_BEARING_GATE_KINDS

logger = logging.getLogger(__name__)

#: The scope every gate uses until the wave has a commit to diff against.
PENDING_WAVE_SCOPE: Final = "all"

#: The timeout class a targeted wave run fits inside.
TARGETED_TIMEOUT_CLASS: Final = "quick"

#: The cadence a gate observing a continuous-integration result carries.
CI_CADENCE: Final = "ship"

#: The cadence the wave close runs.
WAVE_CLOSE_CADENCE: Final = "every-wave"


class GateConventionError(ValueError):
    """A minted gate contradicts one of the declared gate conventions."""


def validate_gate_conventions(
    gates: Sequence[GateSpec],
    *,
    wave_has_commit: bool,
    timeout_exception: str | None,
) -> None:
    """Refuse the first argv gate whose scope, timeout class or cadence is off-convention.

    Args:
        gates: The gates the spec mints. Gates of a kind that carries no
            argv are not subject to these conventions and are skipped.
        wave_has_commit: Whether the wave already has a commit a
            revision-derived scope could take its diff base from.
        timeout_exception: The wave's stated reason its targeted run
            exceeds the ``quick`` ceiling, or ``None`` when it states none.

    Raises:
        GateConventionError: A gate leaves its scope or timeout class to the
            runner's default, narrows its scope before the wave has a
            commit, exceeds ``quick`` without a stated exception, is a
            second wave-close argv gate on one criterion, or blocks the
            wave close from ``cadence: ship``. The message names the gate
            and the convention it breaks.
    """
    argv_gates = [gate for gate in gates if gate.kind in ARGV_BEARING_GATE_KINDS]
    for gate in argv_gates:
        _check_scope(gate, wave_has_commit=wave_has_commit)
        _check_timeout_class(gate, timeout_exception=timeout_exception)
        if gate.cadence == CI_CADENCE and gate.required:
            _refuse(
                gate,
                f"cadence {CI_CADENCE!r} observes a continuous-integration result a wave "
                f"close never runs, so it must be required: false",
            )
    close_gates = Counter(
        gate.criterion_id for gate in argv_gates if gate.cadence == WAVE_CLOSE_CADENCE
    )
    for gate in argv_gates:
        if gate.cadence == WAVE_CLOSE_CADENCE and close_gates[gate.criterion_id] > 1:
            _refuse(
                gate,
                f"criterion {gate.criterion_id!r} carries {close_gates[gate.criterion_id]} "
                f"wave-close argv gates; a repetition loops inside one test and a half one "
                f"argv cannot observe is an assertion in that test, while a CI result is a "
                f"second gate at cadence {CI_CADENCE!r}",
            )


def _check_scope(gate: GateSpec, *, wave_has_commit: bool) -> None:
    """Refuse a defaulted scope, or a narrowed one on a wave with no commit."""
    scope = gate.args.get("scope")
    if scope is None:
        _refuse(gate, f"declares no scope; write scope: {PENDING_WAVE_SCOPE!r}")
    if scope != PENDING_WAVE_SCOPE and not wave_has_commit:
        _refuse(
            gate,
            f"scope {scope!r} derives its diff base from a wave commit the wave does not "
            f"have yet; write scope: {PENDING_WAVE_SCOPE!r}",
        )


def _check_timeout_class(gate: GateSpec, *, timeout_exception: str | None) -> None:
    """Refuse a defaulted timeout class, or one past ``quick`` with no exception."""
    timeout_class = gate.args.get("timeout_class")
    if timeout_class is None:
        _refuse(gate, f"declares no timeout_class; write timeout_class: {TARGETED_TIMEOUT_CLASS!r}")
    if timeout_class != TARGETED_TIMEOUT_CLASS and timeout_exception is None:
        _refuse(
            gate,
            f"timeout_class {timeout_class!r} exceeds the targeted-run ceiling "
            f"{TARGETED_TIMEOUT_CLASS!r}; state timeout_exception on the wave if its "
            f"targeted run genuinely needs more",
        )


def _refuse(gate: GateSpec, reason: str) -> None:
    """Log and raise the convention a gate breaks."""
    logger.warning(f"validate_gate_conventions reject gate={gate.id!r} reason={reason!r}")
    raise GateConventionError(f"gate {gate.id!r} {reason}")


__all__ = [
    "CI_CADENCE",
    "PENDING_WAVE_SCOPE",
    "TARGETED_TIMEOUT_CLASS",
    "WAVE_CLOSE_CADENCE",
    "GateConventionError",
    "validate_gate_conventions",
]
