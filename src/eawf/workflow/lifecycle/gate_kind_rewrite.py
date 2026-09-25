"""Bounded gate-kind rewrite for the recorded gates of a CLOSED wave.

Some waves closed with a verification record that cannot prove anything:
their criteria were converted into single-token ``criterion_in_diff``
greps, or they were authored at phase close with attested criteria and no
gate at all. Re-running such a record produces a receipt, but not proof --
a grep for one word passes whether or not the behaviour exists.

The argv repoint (:mod:`eawf.workflow.lifecycle.gate_repoint`) cannot help:
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
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    response_from_gate,
    validate_criterion_gate_refs,
)
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)

#: The only kind a rewrite may produce. A command gate runs the behaviour
#: the criterion describes, which is what every grep kind only guesses at.
COMMAND_GATE_KIND: Final[str] = "command_exit_zero"

#: Gate kinds that read a file for a token or a path and so can pass
#: without the behaviour existing. Only these may be rewritten.
GREP_GATE_KINDS: Final[frozenset[str]] = frozenset(
    {"criterion_in_diff", "regex_in_file", "path_glob_nonempty", "file_exists"}
)

#: Evidence kinds a criterion may be promoted from. A jury or human
#: criterion asserts a judgement a command cannot replace.
_PROMOTABLE_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset({"deterministic", "attested"})

#: Criterion fields the rewrite owns on the criterion of a touched gate.
_PROOF_FIELDS: Final[tuple[str, ...]] = ("gate_ids", "evidence_kind", "response", "oracle_tier")

_ELIDED: Final[str] = "<rewritable>"


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


class GateKindRewriteReport(BaseModel):
    """What a rewrite moved, for the operator and the audit event.

    Attributes:
        wave_id: The closed wave whose gates were rewritten.
        changed: One row per gate rewritten or added; ``before_kind`` is
            ``None`` for an added gate.
        unchanged_gate_ids: Requested gates already carrying the same
            command, so a replayed rewrite reads as a no-op.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    changed: list[GateKindChange] = Field(default_factory=list)
    unchanged_gate_ids: list[str] = Field(default_factory=list)


def frozen_kind_rewrite_fingerprint(
    wave: Wave,
    *,
    gate_ids: frozenset[str],
    criterion_ids: frozenset[str],
) -> dict[str, Any]:
    """Return everything about *wave* a gate-kind rewrite must leave equal.

    Args:
        wave: The wave row to fingerprint.
        gate_ids: Gates the rewrite may replace or add; they are dropped.
        criterion_ids: Criteria owning those gates; their proof fields
            are elided and every other field stays in the fingerprint.

    Returns:
        A JSON-safe dict comparable with ``==`` across a mutation.
    """
    payload: dict[str, Any] = wave.model_dump(mode="json")
    payload["gates"] = [gate for gate in payload.get("gates", []) if gate["id"] not in gate_ids]
    for criterion in payload.get("success_criteria", []):
        if criterion["id"] in criterion_ids:
            for field in _PROOF_FIELDS:
                criterion[field] = _ELIDED
    return payload


def _require_closed_wave(state: State, wave_id: str) -> Wave:
    """Return the CLOSED wave a rewrite may touch, or refuse."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.CLOSED:
        raise LifecycleError(
            f"wave {wave_id!r} is not closed (status={wave.status.value!r}); "
            "a plan-time gate edit goes through `eawf spec sync`"
        )
    return wave


def _command_gate_for(
    rewrite: GateKindRewrite, *, criterion_id: str, base: GateSpec | None
) -> GateSpec:
    """Build the command gate for *rewrite* through a validated parse.

    ``model_validate`` rather than ``model_copy`` so the L0 argv policy on
    :class:`GateSpec` runs on the new argv.
    """
    payload: dict[str, Any] = {
        "id": rewrite.gate_id,
        "criterion_id": criterion_id,
        "kind": COMMAND_GATE_KIND,
        "args": {"argv": list(rewrite.argv)},
        "policy": base.policy if base is not None else "block",
        "cadence": base.cadence if base is not None else "every-wave",
        "required": base.required if base is not None else True,
        "timeout_s": base.timeout_s if base is not None else None,
    }
    try:
        return GateSpec.model_validate(payload)
    except ValidationError as exc:
        raise LifecycleError(f"gate {rewrite.gate_id!r} rewrite argv rejected: {exc}") from exc


def _require_unique(rewrites: list[GateKindRewrite]) -> None:
    """Refuse an empty request or one that names a gate twice."""
    if not rewrites:
        raise LifecycleError("no gate-kind rewrites supplied")
    seen: set[str] = set()
    for rewrite in rewrites:
        if rewrite.gate_id in seen:
            raise LifecycleError(f"duplicate gate-kind rewrite for gate {rewrite.gate_id!r}")
        seen.add(rewrite.gate_id)


def _plan_addition(wave: Wave, rewrite: GateKindRewrite) -> GateSpec:
    """Build a new command gate for a criterion the wave records."""
    if rewrite.criterion_id is None:
        raise LifecycleError(
            f"gate {rewrite.gate_id!r} is not recorded on wave {wave.id!r}; "
            "adding a gate needs the criterion it proves"
        )
    if rewrite.criterion_id not in {criterion.id for criterion in wave.success_criteria}:
        raise LifecycleError(f"wave {wave.id!r} records no criterion {rewrite.criterion_id!r}")
    return _command_gate_for(rewrite, criterion_id=rewrite.criterion_id, base=None)


def _plan_rewrite(base: GateSpec, rewrite: GateKindRewrite) -> GateSpec | None:
    """Build the command gate replacing *base*, or ``None`` when it already is one.

    Raises:
        LifecycleError: The request rebinds the gate, moves a command
            gate's argv, or names a gate that is not grep-style.
    """
    if rewrite.criterion_id is not None and rewrite.criterion_id != base.criterion_id:
        raise LifecycleError(
            f"gate {base.id!r} proves criterion {base.criterion_id!r}, "
            f"not {rewrite.criterion_id!r}; a rewrite never rebinds a gate"
        )
    if base.kind == COMMAND_GATE_KIND:
        if list(base.args.get("argv", [])) == list(rewrite.argv):
            return None
        raise LifecycleError(
            f"gate {base.id!r} is already a {COMMAND_GATE_KIND!r} gate; "
            "move its argv with `eawf spec repoint-gates`"
        )
    if base.kind not in GREP_GATE_KINDS:
        raise LifecycleError(
            f"gate {base.id!r} kind={base.kind!r} is not a grep-style gate; only "
            f"{sorted(GREP_GATE_KINDS)} may be rewritten"
        )
    return _command_gate_for(rewrite, criterion_id=base.criterion_id, base=base)


def _plan_gates(
    wave: Wave, rewrites: list[GateKindRewrite]
) -> tuple[list[GateSpec], list[GateKindChange], list[str]]:
    """Return the candidate gate list, the changes and the no-op gate ids.

    Raises:
        LifecycleError: On a duplicate or empty request, a weakening
            target kind, or any refusal from the per-gate planners.
    """
    _require_unique(rewrites)
    recorded = {gate.id: gate for gate in wave.gates}
    replaced: dict[str, GateSpec] = {}
    added: list[GateSpec] = []
    changes: list[GateKindChange] = []
    unchanged: list[str] = []
    for rewrite in rewrites:
        if rewrite.kind != COMMAND_GATE_KIND:
            raise LifecycleError(
                f"gate {rewrite.gate_id!r} rewrite to kind={rewrite.kind!r} refused: a "
                f"rewrite only strengthens a gate to {COMMAND_GATE_KIND!r} and never weakens one"
            )
        base = recorded.get(rewrite.gate_id)
        if base is None:
            gate = _plan_addition(wave, rewrite)
            added.append(gate)
            changes.append(_change(gate, before_kind=None))
            continue
        rewritten = _plan_rewrite(base, rewrite)
        if rewritten is None:
            unchanged.append(base.id)
            continue
        replaced[base.id] = rewritten
        changes.append(_change(rewritten, before_kind=base.kind))

    candidates = [replaced.get(gate.id, gate) for gate in wave.gates] + added
    return candidates, changes, unchanged


def _change(gate: GateSpec, *, before_kind: str | None) -> GateKindChange:
    """Describe one applied rewrite for the report."""
    return GateKindChange(
        gate_id=gate.id,
        criterion_id=gate.criterion_id,
        before_kind=before_kind,
        after_kind=gate.kind,
        after_argv=list(gate.args["argv"]),
    )


def _promote_criteria(
    wave: Wave, *, gates: list[GateSpec], owner_ids: frozenset[str]
) -> list[CriterionSpec]:
    """Return the criteria with each touched owner promoted to its command gate.

    An owner gains the new gate ids, becomes ``deterministic`` so the gate
    actually runs, and has a response clause rederived from its cheapest
    bound gate when the old clause named a grep kind or was absent. An
    authored clause naming a command kind is kept.

    Raises:
        LifecycleError: An owner is a jury or human criterion, or the
            promoted set fails the cross-reference and tier check.
    """
    bound: dict[str, list[str]] = {}
    for gate in gates:
        bound.setdefault(gate.criterion_id, []).append(gate.id)
    promoted: list[CriterionSpec] = []
    for criterion in wave.success_criteria:
        if criterion.id not in owner_ids:
            promoted.append(criterion.model_copy(deep=True))
            continue
        if criterion.evidence_kind not in _PROMOTABLE_EVIDENCE_KINDS:
            raise LifecycleError(
                f"criterion {criterion.id!r} evidence_kind={criterion.evidence_kind!r} "
                "is a judgement a command gate cannot replace"
            )
        gate_ids = list(criterion.gate_ids)
        gate_ids += [gate_id for gate_id in bound.get(criterion.id, []) if gate_id not in gate_ids]
        response = criterion.response
        if response is not None and response.gate_ref in GREP_GATE_KINDS:
            response = None
        payload = criterion.model_dump(mode="json")
        payload.update(
            gate_ids=gate_ids,
            evidence_kind="deterministic",
            response=response.model_dump(mode="json") if response is not None else None,
            oracle_tier=None,
        )
        candidate = CriterionSpec.model_validate(payload)
        derived = response_from_gate(candidate, gates)
        if derived is not None:
            candidate = candidate.model_copy(update={"response": derived})
        promoted.append(candidate)
    # The cross-check fills in every criterion's computed tier as a side
    # effect; it runs on copies so only the owners take the new tier and
    # an untouched row stays byte-equal for the fingerprint guard.
    checked = [criterion.model_copy(deep=True) for criterion in promoted]
    try:
        validate_criterion_gate_refs(checked, gates, allow_computed_tier=True)
    except ValueError as exc:
        raise LifecycleError(f"wave {wave.id!r} rewrite fails the gate cross-check: {exc}") from exc
    return [
        verified if verified.id in owner_ids else original
        for verified, original in zip(checked, promoted, strict=True)
    ]


def rewrite_closed_wave_gate_kinds(
    state: State,
    *,
    wave_id: str,
    rewrites: list[GateKindRewrite],
    reason: str | None,
) -> GateKindRewriteReport:
    """Strengthen a CLOSED wave's grep gates to command gates, and only that.

    Mutates *state* in place. The wave stays CLOSED and its criteria text,
    outcome, ``closed_at``, commit pin and untouched gates are held by
    :func:`frozen_kind_rewrite_fingerprint`; any other movement rolls the
    mutation back.

    Args:
        state: State to mutate in place.
        wave_id: Canonical id of the closed wave.
        rewrites: The requested rewrites and additions.
        reason: Why the record is being strengthened; carried onto the
            audit event by the caller.

    Returns:
        A :class:`GateKindRewriteReport` naming what moved.

    Raises:
        LifecycleError: The wave is unknown or not CLOSED, the reason is
            blank, a rewrite is refused by :func:`_plan_gates` or
            :func:`_promote_criteria`, or the result moves anything
            outside the touched gates and their criteria's proof fields.
    """
    wave = _require_closed_wave(state, wave_id)
    if reason is None or not reason.strip():
        raise LifecycleError("a gate-kind rewrite needs a non-empty reason for the audit record")

    gates, changes, unchanged = _plan_gates(wave, rewrites)
    if not changes:
        return GateKindRewriteReport(wave_id=wave_id, unchanged_gate_ids=unchanged)

    touched_gates = frozenset(change.gate_id for change in changes)
    owners = frozenset(change.criterion_id for change in changes)
    criteria = _promote_criteria(wave, gates=gates, owner_ids=owners)

    before = frozen_kind_rewrite_fingerprint(wave, gate_ids=touched_gates, criterion_ids=owners)
    original_gates, original_criteria = list(wave.gates), list(wave.success_criteria)
    wave.gates = gates
    wave.success_criteria = criteria
    after = frozen_kind_rewrite_fingerprint(wave, gate_ids=touched_gates, criterion_ids=owners)
    if after != before:
        wave.gates, wave.success_criteria = original_gates, original_criteria
        drifted = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
        raise LifecycleError(
            f"wave {wave_id!r} gate-kind rewrite refused: it changes more than the "
            f"rewritten gates (changed fields: {', '.join(drifted)})"
        )
    logger.info(
        f"rewrite_closed_wave_gate_kinds wave={wave_id} changed={len(changes)} "
        f"unchanged={len(unchanged)}"
    )
    return GateKindRewriteReport(wave_id=wave_id, changed=changes, unchanged_gate_ids=unchanged)


__all__ = [
    "COMMAND_GATE_KIND",
    "GREP_GATE_KINDS",
    "GateKindChange",
    "GateKindRewrite",
    "GateKindRewriteReport",
    "frozen_kind_rewrite_fingerprint",
    "rewrite_closed_wave_gate_kinds",
]
