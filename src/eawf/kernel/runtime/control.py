"""Control facts: what a request, an acknowledgement and an effect say.

A control is three facts, not one. The request records that a principal
asked; the acknowledgement records what the daemon decided about the
asking; the effect records what was observed to happen. Collapsing them
into one field is the defect this module exists to prevent, because the
collapsed form has no way to say "we asked and never found out", and a
surface with no way to say that says "cancelled" instead.

The outcome axis is :class:`ControlDisposition`, nine closed values. Each
one is reachable from exactly one phase, and
:data:`PHASE_DISPOSITIONS` is the total map that says which -- checked at
import, so a value nobody assigned a phase, or one assigned two, is a
startup failure rather than a row that renders plausibly and lies.

``idle`` is the exception that proves the rule: it is the projection over
an *absent* request and appears on no fact at all. A verb that was never
issued still has a target, an authority and a refusal reason, so the
renderer binds to ``idle`` rather than to nothing; persisting it would
turn "never asked" into an event that says something happened.

Run truth follows from confirmed effects alone.
:data:`TERMINAL_EFFECT_STATUS` names, for each control, the Run status a
confirmed effect of it terminalizes to, and ``None`` for the controls
that end no Run. An acknowledgement -- even an accepted one -- moves
nothing: a lost acknowledgement is a Run whose fate is unknown, and
unknown is not cancelled.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import StringConstraints, model_validator

from eawf.kernel.runtime.provider import ControlKind, Digest, RuntimeRecord
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime


def _identifier(prefix: str) -> StringConstraints:
    """Return the grammar of a prefixed, hex-bodied control identifier."""
    return StringConstraints(strict=True, pattern=rf"^{prefix}-[0-9a-f]{{8,32}}$")


#: One control request, named by the principal that opened it. The hex
#: body carries no meaning: a request id is compared, never parsed.
ControlRequestId = Annotated[str, _identifier("CTL")]

#: One observed effect of a control. Present only on a confirmed effect,
#: because an effect id names a fact somebody saw.
ControlEffectId = Annotated[str, _identifier("EFF")]

#: One reconciliation receipt. It stands where an effect id cannot: the
#: transport failed and what happened is genuinely undetermined.
ReconciliationReceiptId = Annotated[str, _identifier("REC")]


class ControlPhase(StrEnum):
    """The three facts one control produces, in the order they exist.

    The phases are separate rows rather than a field that advances,
    because each is a different principal's observation and losing the
    third must not rewrite the first two.
    """

    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    EFFECTED = "effected"


class ControlDisposition(StrEnum):
    """The one rendered outcome axis of every mutating control.

    ``UNKNOWN`` and ``RECOVERY`` say the same thing about the world --
    the effect is undetermined -- and differ only in whether the
    automatic reconciliation is still running. Neither is success and
    neither is failure, and a surface that renders either as one of those
    is the dishonesty the nine-value vocabulary replaced.
    """

    IDLE = "idle"
    REQUESTING = "requesting"
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"
    UNKNOWN = "unknown"
    RECOVERY = "recovery"
    SUPERSEDED = "superseded"


#: The dispositions no event ever carries. ``idle`` is what the daemon
#: reports for a control nobody issued, so it is produced by a projection
#: over an absent request and is never appended to the control ledger.
PROJECTED_DISPOSITIONS: Final[frozenset[ControlDisposition]] = frozenset({ControlDisposition.IDLE})

#: Which phase admits which dispositions, as declared. Compiled through
#: :func:`compile_phase_dispositions` into :data:`PHASE_DISPOSITIONS`.
_DECLARED_PHASE_DISPOSITIONS: Final[Mapping[ControlPhase, tuple[ControlDisposition, ...]]] = {
    ControlPhase.REQUESTED: (ControlDisposition.REQUESTING,),
    ControlPhase.ACKNOWLEDGED: (
        ControlDisposition.ACCEPTED,
        ControlDisposition.REJECTED,
        ControlDisposition.INVALIDATED,
        ControlDisposition.SUPERSEDED,
    ),
    ControlPhase.EFFECTED: (
        ControlDisposition.CONFIRMED,
        ControlDisposition.UNKNOWN,
        ControlDisposition.RECOVERY,
    ),
}


def compile_phase_dispositions(
    declared: Mapping[ControlPhase, Iterable[ControlDisposition]],
) -> Mapping[ControlPhase, frozenset[ControlDisposition]]:
    """Compile *declared* into the total phase-to-disposition map.

    Args:
        declared: The admitted dispositions of each phase, in table order.

    Returns:
        A read-only mapping from every phase to the frozen set of
        dispositions it admits.

    Raises:
        ValueError: A phase has no row, so a fact of it could carry
            anything; a projected disposition is listed on a phase, which
            would let a never-issued control be persisted as an event; a
            persisted disposition is listed on no phase or on two, which
            would leave the fact it is reachable from ambiguous.
    """
    table: dict[ControlPhase, frozenset[ControlDisposition]] = {}
    owner: dict[ControlDisposition, ControlPhase] = {}
    for phase, dispositions in declared.items():
        admitted = frozenset(dispositions)
        projected = sorted(value.value for value in admitted & PROJECTED_DISPOSITIONS)
        if projected:
            raise ValueError(
                f"phase {phase.value!r} admits projected disposition {', '.join(projected)}, "
                "which no event carries"
            )
        for disposition in admitted:
            claimed = owner.get(disposition)
            if claimed is not None:
                raise ValueError(
                    f"disposition {disposition.value!r} is reachable from both "
                    f"{claimed.value!r} and {phase.value!r}"
                )
            owner[disposition] = phase
        table[phase] = admitted
    missing_phases = sorted(phase.value for phase in ControlPhase if phase not in table)
    if missing_phases:
        raise ValueError(f"no dispositions declared for phase {', '.join(missing_phases)}")
    unreachable = sorted(
        value.value
        for value in ControlDisposition
        if value not in owner and value not in PROJECTED_DISPOSITIONS
    )
    if unreachable:
        raise ValueError(f"no phase reaches disposition {', '.join(unreachable)}")
    return MappingProxyType(table)


#: The total, single-valued phase compatibility map every control fact is
#: validated against.
PHASE_DISPOSITIONS: Final[Mapping[ControlPhase, frozenset[ControlDisposition]]] = (
    compile_phase_dispositions(_DECLARED_PHASE_DISPOSITIONS)
)


def compile_terminal_effects(
    declared: Mapping[ControlKind, RunStatus | None],
) -> Mapping[ControlKind, RunStatus | None]:
    """Compile *declared* into the total control-to-terminal-status map.

    Args:
        declared: The Run status a confirmed effect of each control
            terminalizes to, or ``None`` for a control that ends no Run.

    Returns:
        A read-only mapping covering every control kind.

    Raises:
        ValueError: A control kind has no row, so whether a confirmed
            effect of it ends the Run would be decided by whichever
            reducer read it, or a row names a status that is not terminal.
    """
    missing = sorted(kind.value for kind in ControlKind if kind not in declared)
    if missing:
        raise ValueError(f"no terminal effect declared for control {', '.join(missing)}")
    for kind, status in declared.items():
        if status is not None and status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"control {kind.value!r} terminalizes to {status.value}, which is not terminal"
            )
    return MappingProxyType(dict(declared))


#: The Run statuses a Run has stopped in.
TERMINAL_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)

#: Which control, once its effect is confirmed, ends the Run and in what
#: status. Cancel and interrupt both stop the episode, and a stopped
#: episode that produced no report is cancelled rather than completed.
TERMINAL_EFFECT_STATUS: Final[Mapping[ControlKind, RunStatus | None]] = compile_terminal_effects(
    {
        ControlKind.CANCEL: RunStatus.CANCELLED,
        ControlKind.INTERRUPT: RunStatus.CANCELLED,
        ControlKind.STEER: None,
        ControlKind.ANSWER: None,
        ControlKind.RESUME: None,
        ControlKind.RETRY: None,
        ControlKind.FORK: None,
        ControlKind.RECONCILE: None,
    }
)

#: The dispositions under which a request still holds the Run's control
#: lease. A rejected, invalidated or superseded request holds nothing,
#: and a merely requesting one has not been granted anything yet.
LEASE_HOLDING_DISPOSITIONS: Final[frozenset[ControlDisposition]] = frozenset(
    {
        ControlDisposition.ACCEPTED,
        ControlDisposition.CONFIRMED,
        ControlDisposition.UNKNOWN,
        ControlDisposition.RECOVERY,
    }
)

#: The effected dispositions that carry a reconciliation receipt in place
#: of an effect reference, because no effect was observed.
UNRESOLVED_DISPOSITIONS: Final[frozenset[ControlDisposition]] = frozenset(
    {ControlDisposition.UNKNOWN, ControlDisposition.RECOVERY}
)


class ControlFact(RuntimeRecord):
    """One append-only line of a Run's control ledger.

    Attributes:
        payload_kind: The discriminator that separates a control fact
            from any other line the run collection holds.
        control_request_ref: The request this fact belongs to. All three
            phases of one control share it.
        run_ref: The Run the control addresses.
        control: Which control was asked for.
        phase: Which of the three facts this line is.
        disposition: The outcome this phase reports.
        effect_ref: The observed effect, present only on a confirmed one.
        receipt_ref: The reconciliation receipt, present only where the
            effect is undetermined.
        actor: The principal the fact is attributable to.
        recorded_at: When the daemon appended the line.
        sequence: The line's position in this Run's control ledger,
            counting from one. The control cursor is the highest of them.
    """

    payload_kind: Literal["control"] = "control"
    control_request_ref: ControlRequestId
    run_ref: RunUrn
    control: ControlKind
    phase: ControlPhase
    disposition: ControlDisposition
    effect_ref: ControlEffectId | None = None
    receipt_ref: ReconciliationReceiptId | None = None
    actor: PrincipalKey
    recorded_at: UtcDatetime
    sequence: StrictPositiveInt

    @model_validator(mode="after")
    def _disposition_belongs_to_the_phase(self) -> Self:
        """Refuse a pairing the total phase map does not admit.

        Raises:
            ValueError: The phase does not admit the disposition. The
                projected ``idle`` is refused here too, because no phase
                admits it.
        """
        if self.disposition not in PHASE_DISPOSITIONS[self.phase]:
            admitted = ", ".join(sorted(value.value for value in PHASE_DISPOSITIONS[self.phase]))
            raise ValueError(
                f"phase {self.phase.value!r} does not admit disposition "
                f"{self.disposition.value!r}; admitted: {admitted}"
            )
        return self

    @model_validator(mode="after")
    def _proof_matches_the_disposition(self) -> Self:
        """Require exactly the proof each outcome is evidenced by.

        Raises:
            ValueError: A confirmed effect names no effect, an
                undetermined one names no reconciliation receipt, a
                pre-effect phase names an effect, or a superseded request
                carries a receipt. The last is the rule that keeps a
                losing request from looking like a resolution: the
                winner's receipt is the only record of it.
        """
        if self.disposition is ControlDisposition.CONFIRMED:
            if self.effect_ref is None:
                raise ValueError("a confirmed effect names the effect that was observed")
            if self.receipt_ref is not None:
                raise ValueError("a confirmed effect carries no reconciliation receipt")
            return self
        if self.disposition in UNRESOLVED_DISPOSITIONS:
            if self.receipt_ref is None:
                raise ValueError(
                    f"disposition {self.disposition.value!r} names the reconciliation receipt "
                    "that stands in for the effect nobody observed"
                )
            if self.effect_ref is not None:
                raise ValueError(
                    f"disposition {self.disposition.value!r} means no effect was observed, "
                    "so it carries no effect reference"
                )
            return self
        if self.effect_ref is not None:
            raise ValueError(f"phase {self.phase.value!r} records no effect reference")
        if self.receipt_ref is not None:
            raise ValueError(
                f"disposition {self.disposition.value!r} was neither applied nor refused, "
                "so no receipt is written for it"
            )
        return self


class RunBinding(RuntimeRecord):
    """The durable contract binding of one Run, appended once at dispatch.

    The three digests a Run must be reconstructible against are recorded
    here rather than derived from a live process, which is the whole
    point: a contract that needs the dispatching session to still exist
    is not a contract that survives the session.

    Attributes:
        payload_kind: The discriminator separating a binding line from a
            control fact and from a compacted Run record.
        run_ref: The Run the binding belongs to.
        compiled_spec_digest: The exact compiled provider and policy input.
        authority_capsule_digest: The exact redacted authority capsule.
        route_policy_revision: The routing policy the compiler resolved.
        bound_at: When the binding was recorded.
    """

    payload_kind: Literal["run_binding"] = "run_binding"
    run_ref: RunUrn
    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    route_policy_revision: StrictPositiveInt
    bound_at: UtcDatetime


__all__ = [
    "LEASE_HOLDING_DISPOSITIONS",
    "PHASE_DISPOSITIONS",
    "PROJECTED_DISPOSITIONS",
    "TERMINAL_EFFECT_STATUS",
    "TERMINAL_RUN_STATUSES",
    "UNRESOLVED_DISPOSITIONS",
    "ControlDisposition",
    "ControlEffectId",
    "ControlFact",
    "ControlPhase",
    "ControlRequestId",
    "ReconciliationReceiptId",
    "RunBinding",
    "compile_phase_dispositions",
    "compile_terminal_effects",
]
