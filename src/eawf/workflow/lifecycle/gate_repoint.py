"""Bounded argv repoint for the recorded gates of a CLOSED wave.

A closed wave's gate rows are its verification record: they say what was
run to prove the wave's criteria held. When a later wave moves the test
tree beneath them, that record stops being *re-runnable* -- replaying the
recorded argv exits on a usage error because the named path no longer
exists -- even though the verification itself was sound. There is no
wave-level reopen, and ``spec sync`` refuses a non-PENDING wave, so today
the record simply stays wrong.

This module is the narrow repair path. It rewrites the argv of a closed
wave's gates and NOTHING else: the mutation is applied and then checked
against a fingerprint of the whole wave row with only the gate argv
elided (:func:`frozen_wave_fingerprint`). If anything else moved -- a
criterion, the outcome, ``closed_at``, the status, a gate's policy /
cadence / criterion binding, or the gate set itself -- the mutation is
rolled back and refused. The guard is the mechanism, not a comment: a
caller cannot smuggle a second edit through this verb even by handing it
a fully rewritten gate list.

The two functions split by responsibility: :func:`build_argv_repoint`
translates a narrow ``gate_id -> argv`` request into candidate gate rows
(re-running the L0 argv policy at the parse seam), and
:func:`repoint_closed_wave_gates` applies a candidate gate list under the
frozen-fingerprint guard.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.promotion import (
    ARGV_BEARING_GATE_KINDS,
    SpecPromoteValidationError,
    validate_argv_gates,
)
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)

#: Stand-in written over every gate's ``args['argv']`` when the frozen
#: fingerprint is computed. Any real argv would make the fingerprint
#: sensitive to the one field this verb is allowed to move.
ELIDED_ARGV: Final[str] = "<repointable-argv>"


class GateArgvRepoint(BaseModel):
    """One requested argv rewrite against a single recorded gate.

    Attributes:
        gate_id: Id of the gate row already recorded on the wave. The
            repoint never creates a gate, so an unknown id is refused.
        argv: Replacement argv vector, re-validated through the L0
            argv policy before it can reach state.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)


class GateArgvChange(BaseModel):
    """The applied before/after argv pair for one repointed gate."""

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    before_argv: list[str]
    after_argv: list[str]


class GateRepointReport(BaseModel):
    """What a repoint actually moved, for the operator and the audit event.

    Attributes:
        wave_id: The closed wave whose gates were repointed.
        changed: One row per gate whose argv actually differs.
        unchanged_gate_ids: Gate ids that survived the pass untouched,
            so a replayed repoint reads as a no-op rather than as a
            silent partial application.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    changed: list[GateArgvChange] = Field(default_factory=list)
    unchanged_gate_ids: list[str] = Field(default_factory=list)


def frozen_wave_fingerprint(wave: Wave) -> dict[str, Any]:
    """Return everything about *wave* that a repoint must leave equal.

    The fingerprint is the wave row's full JSON dump with every gate's
    ``args['argv']`` replaced by :data:`ELIDED_ARGV`. It therefore still
    covers the wave's success criteria, outcome, status, ``closed_at``,
    commit pin, sessions, and every non-argv gate field -- the argv is
    the single degree of freedom the repoint is allowed to use.

    Args:
        wave: The wave row to fingerprint.

    Returns:
        A JSON-safe dict comparable with ``==`` across a mutation.
    """
    payload: dict[str, Any] = wave.model_dump(mode="json")
    for gate in payload.get("gates", []):
        args = gate.get("args")
        if isinstance(args, dict) and "argv" in args:
            args["argv"] = ELIDED_ARGV
    return payload


def build_argv_repoint(wave: Wave, repoints: list[GateArgvRepoint]) -> list[GateSpec]:
    """Build the candidate gate list that applies *repoints* to *wave*.

    Every recorded gate is carried over; the named ones get their
    ``args['argv']`` replaced. The candidate rows are rebuilt through
    ``GateSpec.model_validate`` rather than ``model_copy`` so the L0
    argv-policy validator on :class:`~eawf.kernel.spec.common.GateSpec`
    fires at this parse seam -- ``model_copy`` skips validators, which
    would let a shell-metachar argv through.

    Args:
        wave: The wave whose recorded gates are the base of the rewrite.
        repoints: Requested per-gate argv rewrites.

    Returns:
        The candidate gate list, in the wave's recorded gate order.

    Raises:
        LifecycleError: When *repoints* is empty, names a gate id twice,
            names a gate the wave does not record, targets a gate whose
            kind carries no argv, or supplies an argv the L0 policy
            rejects.
    """
    if not repoints:
        raise LifecycleError("no gate repoints supplied")
    requested: dict[str, list[str]] = {}
    for repoint in repoints:
        if repoint.gate_id in requested:
            raise LifecycleError(f"duplicate gate repoint for gate {repoint.gate_id!r}")
        requested[repoint.gate_id] = list(repoint.argv)
    recorded = {gate.id for gate in wave.gates}
    unknown = sorted(gate_id for gate_id in requested if gate_id not in recorded)
    if unknown:
        raise LifecycleError(
            f"wave {wave.id!r} records no gate(s) {unknown}; a repoint never creates a gate"
        )

    candidates: list[GateSpec] = []
    for gate in wave.gates:
        argv = requested.get(gate.id)
        if argv is None:
            candidates.append(gate)
            continue
        if gate.kind not in ARGV_BEARING_GATE_KINDS:
            raise LifecycleError(
                f"gate {gate.id!r} kind={gate.kind!r} carries no argv; nothing to repoint"
            )
        payload = gate.model_dump(mode="json")
        payload["args"] = {**payload.get("args", {}), "argv": argv}
        try:
            candidates.append(GateSpec.model_validate(payload))
        except ValidationError as exc:
            raise LifecycleError(f"gate {gate.id!r} repoint argv rejected: {exc}") from exc
    return candidates


def repoint_closed_wave_gates(
    state: State,
    *,
    wave_id: str,
    gates: list[GateSpec],
) -> GateRepointReport:
    """Rewrite a CLOSED wave's recorded gate argv, and only that.

    Mutates *state* in place. The wave is never reopened and its status
    stays ``CLOSED``; the whole row is fingerprinted before and after
    (:func:`frozen_wave_fingerprint`) and the mutation is rolled back and
    refused when anything outside the gate argv moved. That covers the
    criteria, the recorded outcome, ``closed_at``, and every non-argv
    gate field, so this verb cannot be used to rewrite a verification
    verdict under cover of a path fix.

    Args:
        state: State to mutate in place.
        wave_id: Canonical id of the closed wave.
        gates: Full replacement gate list, normally produced by
            :func:`build_argv_repoint`.

    Returns:
        A :class:`GateRepointReport` naming the gates whose argv moved.

    Raises:
        LifecycleError: When *wave_id* is unknown, the wave is not
            CLOSED, the wave records no gates, a replacement argv fails
            the L0 policy, or the replacement changes anything other
            than gate argv.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.CLOSED:
        raise LifecycleError(
            f"wave {wave_id!r} is not closed (status={wave.status.value!r}); "
            "a plan-time gate edit goes through `eawf spec sync`"
        )
    if not wave.gates:
        raise LifecycleError(f"wave {wave_id!r} records no gates to repoint")

    try:
        validate_argv_gates(gates)
    except SpecPromoteValidationError as exc:
        raise LifecycleError(f"wave {wave_id!r} repoint argv rejected: {exc}") from exc

    before = frozen_wave_fingerprint(wave)
    recorded_argv = {gate.id: list(gate.args.get("argv", [])) for gate in wave.gates}
    original = list(wave.gates)
    wave.gates = list(gates)
    after = frozen_wave_fingerprint(wave)
    if after != before:
        wave.gates = original
        raise LifecycleError(
            f"wave {wave_id!r} repoint refused: it changes more than gate argv "
            f"({_describe_drift(before, after)})"
        )

    changed: list[GateArgvChange] = []
    unchanged: list[str] = []
    for gate in wave.gates:
        after_argv = list(gate.args.get("argv", []))
        before_argv = recorded_argv[gate.id]
        if after_argv == before_argv:
            unchanged.append(gate.id)
            continue
        changed.append(
            GateArgvChange(gate_id=gate.id, before_argv=before_argv, after_argv=after_argv)
        )
    logger.info(
        f"repoint_closed_wave_gates wave={wave_id} changed={len(changed)} "
        f"unchanged={len(unchanged)}"
    )
    return GateRepointReport(wave_id=wave_id, changed=changed, unchanged_gate_ids=unchanged)


def _describe_drift(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Name the fingerprint fields that moved, for the refusal message.

    Args:
        before: Frozen fingerprint captured before the assignment.
        after: Frozen fingerprint captured after it.

    Returns:
        A comma-joined list of the differing top-level wave fields, so
        triage reads the reason off the error without a re-run.
    """
    keys = set(before) | set(after)
    drifted = sorted(key for key in keys if before.get(key) != after.get(key))
    return "changed fields: " + ", ".join(drifted)


__all__ = [
    "ELIDED_ARGV",
    "GateArgvChange",
    "GateArgvRepoint",
    "GateRepointReport",
    "build_argv_repoint",
    "frozen_wave_fingerprint",
    "repoint_closed_wave_gates",
]
