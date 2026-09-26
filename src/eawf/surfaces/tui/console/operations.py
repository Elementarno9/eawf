"""The console's writes: which verbs reach a daemon mutator, and the ledger of what was sent.

A console verb never changes what the console holds. It is sent to the daemon as one
operation named by an operation id, and the only thing that moves the frame afterwards is
the daemon's own answer and the patch its commit pushes. The id is what the daemon files
the write under, so sending the same operation twice is one write: that is what lets a
reconnect reconcile an operation whose answer was lost by simply asking again under the
same id, instead of guessing whether it landed.

Two daemon mutators are bound. An answer to a pending action goes to the approval seal,
which reports a later conflicting answer as superseded rather than refusing it; and a Run
control goes to the control-request verb, which records that a principal asked and moves
the Run not at all. Every other writing verb stays listed and refused with its reason,
because a verb that looked like it worked while the daemon never heard of it is the one
thing a console must not draw.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from eawf.kernel.runtime.provider import ControlKind
from eawf.workflow.delivery.acceptance_approval import ACCEPTANCE_OPTIONS

logger = logging.getLogger(__name__)

#: The daemon verb that seals an answer to a pending action. Spelled here rather than
#: imported, because importing the daemon's method module registers its handlers in the
#: console's process; a contract test pins the two spellings together.
SEAL_METHOD: Final = "runtime.delivery.seal_acceptance_approval"

#: The daemon verb that records a principal's request for a Run control.
CONTROL_METHOD: Final = "runtime.run.control.request"

#: The route whose verbs answer pending actions.
ATTENTION_ROUTE: Final = "attention"

#: The target kinds a Run control addresses: the Run's own route, and the pause card's.
RUN_KINDS: Final = frozenset({"run.detail", "run"})

#: The answer an attention verb gives, by verb name. The daemon seals only the
#: acceptance approval, so its option ids are the answers a console can give.
ANSWER_OPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"answer": "approve", "deny": "decline"}
)

#: The question overlay's numbered answers, in the order the approval offers them.
QUESTION_OPTIONS: Final[tuple[str, ...]] = tuple(o.option_id for o in ACCEPTANCE_OPTIONS)

#: The Run control each bound run verb requests, by verb name.
RUN_CONTROLS: Final[Mapping[str, ControlKind]] = MappingProxyType(
    {
        "interrupt": ControlKind.INTERRUPT,
        "cancel": ControlKind.CANCEL,
        "reconcile": ControlKind.RECONCILE,
    }
)

#: Why a writing verb with no daemon mutator is refused; the reason the menu shows.
UNBOUND_REASON: Final = "no daemon verb carries this yet"
_UNBOUND_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "snooze": "no daemon verb snoozes a pending action",
        "resolve": "no daemon verb resolves a notice",
    }
)

#: How many random bytes an operation id carries.
_ID_BYTES: Final = 8


def binding_refusal(kind: str, verb: str) -> str:
    """Return why ``verb`` on a ``kind`` target reaches no daemon mutator.

    Args:
        kind: The route or target kind the verb acts on (``attention``, ``run.detail``).
        verb: The verb's name, as the menu lists it.

    Returns:
        An empty string when a daemon mutator carries the verb; otherwise the reason the
        verb is refused.
    """
    if kind == ATTENTION_ROUTE and verb in ANSWER_OPTIONS:
        return ""
    if kind in RUN_KINDS and verb in RUN_CONTROLS:
        return ""
    return _UNBOUND_REASONS.get(verb, UNBOUND_REASON)


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerRequest:
    """An operator's answer to one pending action, before it is addressed.

    Attributes:
        target: The pending action's public key.
        option_id: The option the operator chose.
    """

    target: str
    option_id: str

    def __post_init__(self) -> None:
        """Refuse an option the approval does not offer.

        Raises:
            ValueError: ``option_id`` is not one of the approval's options.
        """
        if self.option_id not in QUESTION_OPTIONS:
            raise ValueError(
                f"option {self.option_id!r} is not one of {', '.join(QUESTION_OPTIONS)}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlRequest:
    """An operator's request for one Run control, before it is addressed.

    Attributes:
        target: The Run's public key.
        control: The control asked for.
    """

    target: str
    control: ControlKind


VerbRequest = AnswerRequest | ControlRequest


@dataclass(frozen=True, slots=True, kw_only=True)
class Operator:
    """Who the console acts as.

    Attributes:
        principal: The principal key every write is attributed to; an answer is sealed
            in this person's name, so it is typed human on the wire.
        receipt_ref: The evidence row an answer is recorded under. The daemon seals only
            an answer that cites a held evidence row, so a console with none refuses to
            answer rather than inventing one.
    """

    principal: str
    receipt_ref: str | None = None


class OperationStatus(StrEnum):
    """Where one operation stands, as far as the console knows."""

    OUTSTANDING = "outstanding"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsoleOperation:
    """One write, addressed and named, exactly as it is sent.

    Attributes:
        operation_id: The id the daemon files the write under; the same id sent again
            answers the first write rather than making a second.
        method: The daemon verb.
        params: The request parameters, the operation id among them.
        target: The public key of the record written.
    """

    operation_id: str
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    target: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationResult:
    """What became of one operation.

    Attributes:
        operation_id: The operation's id; ``None`` for a request refused before it was
            addressed, which was never sent.
        target: The public key of the record the request was about.
        status: Where the operation stands.
        detail: One sentence an operator reads.
    """

    operation_id: str | None
    target: str
    status: OperationStatus
    detail: str


def _minted(prefix: str) -> str:
    """Return a fresh operation id under ``prefix``."""
    return f"{prefix}-{secrets.token_hex(_ID_BYTES)}"


def address(
    request: VerbRequest, *, urn: str, revision: int, operator: Operator
) -> ConsoleOperation | OperationResult:
    """Return the operation ``request`` is sent as, or why it cannot be sent.

    Args:
        request: What the operator asked for.
        urn: The target record's canonical address, as the projection states it.
        revision: The target record's revision, as the projection states it.
        operator: Who the console acts as.

    Returns:
        The addressed operation under a freshly minted id; a refused result when the
        operator holds no receipt an answer could be recorded under.
    """
    if isinstance(request, ControlRequest):
        ref = _minted("CTL")
        return ConsoleOperation(
            operation_id=ref,
            method=CONTROL_METHOD,
            params=MappingProxyType(
                {
                    "urn": urn,
                    "control_request_ref": ref,
                    "control": request.control.value,
                    "actor": operator.principal,
                }
            ),
            target=request.target,
        )
    if operator.receipt_ref is None:
        return OperationResult(
            operation_id=None,
            target=request.target,
            status=OperationStatus.REFUSED,
            detail="no evidence receipt to record the answer under — nothing was sent",
        )
    key = _minted("console")
    return ConsoleOperation(
        operation_id=key,
        method=SEAL_METHOD,
        params=MappingProxyType(
            {
                "urn": urn,
                "expected_revision": revision,
                "idempotency_key": key,
                "actor": operator.principal,
                "resolver": {"principal_kind": "human", "principal_id": operator.principal},
                "option_id": request.option_id,
                "receipt_ref": operator.receipt_ref,
            }
        ),
        target=request.target,
    )


def settled(operation: ConsoleOperation, answer: Mapping[str, Any]) -> OperationResult:
    """Return the result the daemon's answer to ``operation`` states.

    An answer the seal reports as ``superseded`` lost to one already given: it is a
    result, not a refusal, and the console says so rather than claiming it applied.
    """
    if answer.get("outcome") == OperationStatus.SUPERSEDED.value:
        return OperationResult(
            operation_id=operation.operation_id,
            target=operation.target,
            status=OperationStatus.SUPERSEDED,
            detail=f"{operation.target} was already answered · this answer is superseded",
        )
    stated = answer.get("reason") or answer.get("disposition") or "recorded"
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.APPLIED,
        detail=f"{operation.target} · {stated}",
    )


def refused(operation: ConsoleOperation, message: str) -> OperationResult:
    """Return the result of a write the daemon answered with a refusal; nothing was written."""
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.REFUSED,
        detail=f"{operation.target} refused · {message}",
    )


def unanswered(operation: ConsoleOperation) -> OperationResult:
    """Return the result of a write whose answer never arrived; its outcome is unknown."""
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.OUTSTANDING,
        detail=f"{operation.target} · no answer yet · a reconnect reconciles it by its id",
    )


class OperationLedger:
    """The operations sent and not yet answered, in the order they were sent.

    The ledger is what the reconnect protocol reconciles: an operation stays in it from
    the moment it is sent until an answer for its id arrives, however many breaks in the
    link happen in between.
    """

    def __init__(self) -> None:
        self._outstanding: dict[str, ConsoleOperation] = {}

    def open(self, operation: ConsoleOperation) -> None:
        """Hold ``operation`` as sent and not yet answered.

        Raises:
            ValueError: An operation with the same id is already outstanding.
        """
        if operation.operation_id in self._outstanding:
            raise ValueError(f"operation {operation.operation_id} is already outstanding")
        self._outstanding[operation.operation_id] = operation

    def settle(self, result: OperationResult) -> None:
        """Close the operation ``result`` answers; an outstanding result keeps it open.

        Raises:
            KeyError: No outstanding operation carries the result's id.
        """
        if result.operation_id is None or result.operation_id not in self._outstanding:
            raise KeyError(f"no outstanding operation {result.operation_id}")
        if result.status is not OperationStatus.OUTSTANDING:
            del self._outstanding[result.operation_id]

    def outstanding(self) -> tuple[ConsoleOperation, ...]:
        """Return every operation still waiting for an answer, oldest first."""
        return tuple(self._outstanding.values())


__all__ = [
    "ANSWER_OPTIONS",
    "CONTROL_METHOD",
    "QUESTION_OPTIONS",
    "RUN_CONTROLS",
    "RUN_KINDS",
    "SEAL_METHOD",
    "UNBOUND_REASON",
    "AnswerRequest",
    "ConsoleOperation",
    "ControlRequest",
    "OperationLedger",
    "OperationResult",
    "OperationStatus",
    "Operator",
    "VerbRequest",
    "address",
    "binding_refusal",
    "refused",
    "settled",
    "unanswered",
]
