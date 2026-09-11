"""The floor that refuses a close which cannot prove its gates ran.

A wave's required blocking gates are the only thing standing between a
claimed outcome and a closed wave. Every branch that reaches the close
scorer is supposed to run them, but the scorer has several silent
``continue`` arms -- a gate whose compile yields nothing, a criterion a
tier filter skips, a risk band that short-circuits the whole scoring
pass -- and each of them returns the same empty evidence list a genuinely
gateless wave returns. The close then completes with ``status=closed``
against a non-empty ``required_gate_ids`` and zero receipts, which reads
on every surface as a verified close while proving nothing.

This module is the terminal check that closes that class of hole once
instead of per arm. It asks one question of the persisted record -- did
every gate the wave was obliged to run leave a receipt this close can
point at -- and refuses when the answer is no. It deliberately does NOT
score, run, or interpret a gate: a gate that ran and failed is the
scorer's business, while a gate nobody can prove ran is this floor's.

The refusal is a :class:`~eawf.workflow.lifecycle._errors.LifecycleError`
so the daemon close worker routes it onto a BLOCKED attempt rather than a
silent completion, and it names the receiptless gates so the operator
repairs the right rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import orjson

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.paths import store_path
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)


class GateReceiptFloorError(LifecycleError):
    """A close whose obliged gates left no receipt to point at.

    Attributes:
        scope_id: The wave whose close was refused.
        missing_gate_ids: The obliged gates with no receipt, sorted. Never
            empty -- the error is only raised when at least one gate is
            unaccounted for.
        obliged_gate_ids: Every gate the close was obliged to run, sorted.
    """

    def __init__(
        self,
        *,
        scope_id: str,
        missing_gate_ids: Sequence[str],
        obliged_gate_ids: Sequence[str],
    ) -> None:
        """Build the refusal naming the receiptless gates.

        Args:
            scope_id: Wave whose close is refused.
            missing_gate_ids: Obliged gates with no receipt.
            obliged_gate_ids: The full obliged set, for context.
        """
        self.scope_id = scope_id
        self.missing_gate_ids = sorted(missing_gate_ids)
        self.obliged_gate_ids = sorted(obliged_gate_ids)
        receipted = sorted(set(self.obliged_gate_ids) - set(self.missing_gate_ids))
        super().__init__(
            f"wave {scope_id!r} cannot close: required blocking gates "
            f"{self.missing_gate_ids} left no execution receipt, so the close "
            "cannot prove they ran "
            f"(obliged={self.obliged_gate_ids}, receipted={receipted})"
        )


def obliged_gate_ids(
    *,
    criteria: Iterable[CriterionSpec],
    gates: Iterable[GateSpec],
) -> frozenset[str]:
    """Return the gates a close MUST execute before it may complete.

    A gate is obliged when all three hold: it is ``required``, its policy
    is ``block``, and it hangs off a required criterion whose
    ``evidence_kind`` is ``deterministic``. Those are the same conditions
    that make the scorer treat a gate verdict as decisive, so the obliged
    set is exactly the set whose absence the close would otherwise have no
    way to notice.

    A non-blocking or non-required gate is excluded on purpose: the scorer
    is allowed to drop one (an argv that cannot red is dropped rather than
    scored), and a verdict it may ignore is not proof it must collect. A
    jury or attested criterion is excluded because no deterministic gate
    compiles for it at all.

    Args:
        criteria: The wave's success criteria.
        gates: The wave's typed gate rows.

    Returns:
        The obliged gate ids; empty when the wave owes no deterministic
        proof.
    """
    deterministic_required = {
        criterion.id
        for criterion in criteria
        if criterion.required and criterion.evidence_kind == "deterministic"
    }
    return frozenset(
        gate.id
        for gate in gates
        if gate.required and gate.policy == "block" and gate.criterion_id in deterministic_required
    )


def receipted_gate_ids(state_path: Path, *, receipt_ids: Iterable[str]) -> frozenset[str]:
    """Return the gate ids covered by the named durable receipts.

    Reads ``gate_receipt.jsonl`` and resolves each wanted receipt id to the
    gate it records. A row that is absent, unparseable, or of the wrong
    kind contributes nothing: an unreadable receipt is not proof, so the
    floor treats it exactly as a missing one.

    Args:
        state_path: Path to ``state.json``; anchors the sibling store dir.
        receipt_ids: Receipt ids bound to the close attempt.

    Returns:
        The gate ids those receipts record.
    """
    wanted = {str(receipt_id) for receipt_id in receipt_ids}
    if not wanted:
        return frozenset()
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return frozenset()
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        logger.warning(f"receipted_gate_ids status=unreadable detail={exc!s}")
        return frozenset()
    covered: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
            if envelope.kind is not StoreKind.GATE_RECEIPT or envelope.id not in wanted:
                continue
            covered.add(GateReceipt.model_validate(envelope.payload).gate_id)
        except orjson.JSONDecodeError, ValueError:
            continue
    return frozenset(covered)


def enforce_gate_receipt_floor(
    *,
    scope_id: str,
    criteria: Iterable[CriterionSpec],
    gates: Iterable[GateSpec],
    receipted: Iterable[str],
) -> frozenset[str]:
    """Refuse a close whose obliged gates are not all receipted.

    Args:
        scope_id: The closing wave's id, used in the refusal.
        criteria: The wave's success criteria.
        gates: The wave's typed gate rows.
        receipted: Gate ids this close can point at a receipt for.

    Returns:
        The obliged gate ids, so a caller can log the ratio it enforced.

    Raises:
        GateReceiptFloorError: When at least one obliged gate has no
            receipt.
    """
    obliged = obliged_gate_ids(criteria=criteria, gates=gates)
    missing = obliged - frozenset(receipted)
    if missing:
        raise GateReceiptFloorError(
            scope_id=scope_id,
            missing_gate_ids=sorted(missing),
            obliged_gate_ids=sorted(obliged),
        )
    logger.info(
        f"enforce_gate_receipt_floor scope_id={scope_id!r} obliged={len(obliged)} status=pass"
    )
    return obliged


__all__ = [
    "GateReceiptFloorError",
    "enforce_gate_receipt_floor",
    "obliged_gate_ids",
    "receipted_gate_ids",
]
