"""Fold a Run's control facts into Run truth, a cursor, and a lease.

Three questions are answered here and nowhere else.

*What is the Run?* :func:`reduce_run_control` walks the ledger in
sequence order and moves the status only where a confirmed effect of a
terminalizing control says so. A request moves nothing, an accepted
acknowledgement moves nothing, and an undetermined effect moves nothing:
"we asked and never found out" is a state the vocabulary can say, so
there is no reason to round it to a terminal one. Once the Run is
terminal a later fact cannot move it again, which is what stops a late
provider event from reopening a finished episode.

*What was asked?* :func:`project_disposition` reduces one request's facts
to its single rendered outcome, and answers ``idle`` for a request with
no facts at all -- the projection over an absent request, which is the
only place ``idle`` is ever produced.

*Who may act?* :func:`decide_control_lease` grants the Run's one control
lease to the first request that was accepted and holds it until that
request is resolved. A second principal asking while the lease is held is
``superseded``: neither applied nor refused, and carrying no receipt,
because the holder's receipt is the only record of the resolution.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from eawf.kernel.runtime.control import (
    LEASE_HOLDING_DISPOSITIONS,
    TERMINAL_EFFECT_STATUS,
    TERMINAL_RUN_STATUSES,
    ControlDisposition,
    ControlFact,
    ControlPhase,
    ControlRequestId,
    RunBinding,
)
from eawf.kernel.runtime.provider import ControlKind, Digest, RuntimeRecord
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn

logger = logging.getLogger(__name__)


class ControlDispositionRow(RuntimeRecord):
    """One request's rendered outcome, as a projection reads it.

    Attributes:
        control_request_ref: The request the row is about.
        control: Which control it asked for.
        disposition: The single outcome the request's facts reduce to.
    """

    control_request_ref: ControlRequestId
    control: ControlKind
    disposition: ControlDisposition


@dataclass(frozen=True, slots=True)
class RunControlState:
    """What a Run's control ledger reduces to.

    Attributes:
        status: The Run status the confirmed effects support. It is the
            stored status until a confirmed terminalizing effect moves it.
        control_cursor: The highest applied control-ledger sequence, or
            zero when the Run has no control facts.
        rows: One row per request, in first-request order.
    """

    status: RunStatus
    control_cursor: int
    rows: tuple[ControlDispositionRow, ...]


@dataclass(frozen=True, slots=True)
class ControlLeaseDecision:
    """Whether a request may take the Run's one control lease.

    Attributes:
        disposition: ``accepted`` when the request takes the lease,
            ``superseded`` when another request already holds it.
        holder: The request holding the lease, which equals the asking
            request when it was just granted.
        reason: One sentence an operator reads, naming the holder.
    """

    disposition: ControlDisposition
    holder: ControlRequestId
    reason: str


class RunContract(RuntimeRecord):
    """The whole Run contract, rebuilt from durable records alone.

    Attributes:
        run_ref: The Run the contract is of.
        status: The status its confirmed effects support.
        revision: The compare-and-swap token of the stored record.
        compiled_spec_digest: The exact compiled provider and policy input.
        authority_capsule_digest: The exact redacted authority capsule.
        route_policy_revision: The routing policy the compiler resolved.
        control_cursor: The last applied control-ledger sequence.
        rows: One rendered outcome per control request.
    """

    run_ref: RunUrn
    status: RunStatus
    revision: StrictPositiveInt
    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    route_policy_revision: StrictPositiveInt
    control_cursor: StrictNonNegativeInt
    rows: tuple[ControlDispositionRow, ...]


def project_disposition(facts: Sequence[ControlFact]) -> ControlDisposition:
    """Return the one rendered outcome *facts* reduce to.

    Args:
        facts: Every fact of one control request, in ledger order. An
            empty sequence is a request that was never issued.

    Returns:
        The disposition of the latest phase the request reached, or
        :attr:`~eawf.kernel.runtime.control.ControlDisposition.IDLE` when
        it produced no fact at all. ``idle`` is produced here and stored
        nowhere.

    Raises:
        ValueError: The facts belong to more than one request, so no
            single outcome is theirs to report.
    """
    if not facts:
        return ControlDisposition.IDLE
    refs = {fact.control_request_ref for fact in facts}
    if len(refs) != 1:
        raise ValueError(f"facts name {len(refs)} requests; a disposition projects over one")
    latest = max(facts, key=lambda fact: fact.sequence)
    return latest.disposition


def reduce_run_control(*, status: RunStatus, facts: Sequence[ControlFact]) -> RunControlState:
    """Fold a Run's control facts into its status, cursor and outcomes.

    Args:
        status: The status the stored Run record carries. The fold starts
            from it, so a record that already moved is never walked back.
        facts: Every control fact of the Run, in ledger order.

    Returns:
        The reduced state.

    Raises:
        ValueError: The sequences are not the contiguous run ``1..n``. A
            gap means a ledger line was lost, and reducing over the
            survivors would report a cursor that skips the missing fact.
    """
    _require_contiguous(facts)
    reduced = status
    grouped: dict[ControlRequestId, list[ControlFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.control_request_ref, []).append(fact)
        reduced = _advance(reduced, fact)
    rows = tuple(
        ControlDispositionRow(
            control_request_ref=ref,
            control=members[0].control,
            disposition=project_disposition(members),
        )
        for ref, members in grouped.items()
    )
    cursor = facts[-1].sequence if facts else 0
    return RunControlState(status=reduced, control_cursor=cursor, rows=rows)


def decide_control_lease(
    *, control_request_ref: ControlRequestId, facts: Sequence[ControlFact]
) -> ControlLeaseDecision:
    """Decide whether *control_request_ref* takes the Run's control lease.

    A Run has one control lease rather than one per control kind: two
    principals cancelling and interrupting the same Run at once are two
    answers to the same question, and granting both would apply one and
    silently drop the other.

    Args:
        control_request_ref: The request asking to be acknowledged.
        facts: Every control fact of the Run, in ledger order.

    Returns:
        The decision, naming whichever request holds the lease.

    Raises:
        ValueError: The request has no requested fact, so there is
            nothing to acknowledge, or it has already been acknowledged,
            which would make a second decision overwrite the first.
    """
    grouped: dict[ControlRequestId, list[ControlFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.control_request_ref, []).append(fact)
    own = grouped.get(control_request_ref)
    if own is None:
        raise ValueError(f"request {control_request_ref!r} has no requested fact to acknowledge")
    if project_disposition(own) is not ControlDisposition.REQUESTING:
        raise ValueError(f"request {control_request_ref!r} has already been acknowledged")
    for ref, members in grouped.items():
        if ref == control_request_ref:
            continue
        if project_disposition(members) in LEASE_HOLDING_DISPOSITIONS:
            return ControlLeaseDecision(
                disposition=ControlDisposition.SUPERSEDED,
                holder=ref,
                reason=f"request {ref} already holds this Run's control lease",
            )
    return ControlLeaseDecision(
        disposition=ControlDisposition.ACCEPTED,
        holder=control_request_ref,
        reason=f"request {control_request_ref} takes this Run's control lease",
    )


def reconstruct_run_contract(
    *, run: Run, binding: RunBinding, facts: Sequence[ControlFact]
) -> RunContract:
    """Rebuild one Run's whole contract from durable records alone.

    Args:
        run: The stored Run record, from the document or its ledger.
        binding: The Run's durable contract binding.
        facts: Every control fact of the Run, in ledger order.

    Returns:
        The contract, carrying the two digests the binding records and
        the control cursor the ledger reaches.

    Raises:
        ValueError: The binding belongs to another Run, which would
            report one Run's authority under another's identity, or the
            control facts are not contiguous.
    """
    if binding.run_ref != run.urn:
        raise ValueError(f"binding names {binding.run_ref} but the record is {run.urn}")
    state = reduce_run_control(status=run.status, facts=facts)
    logger.info(
        f"reconstruct_run_contract run={run.key!r} status={state.status.value} "
        f"control_cursor={state.control_cursor}"
    )
    return RunContract(
        run_ref=run.urn,
        status=state.status,
        revision=run.revision,
        compiled_spec_digest=binding.compiled_spec_digest,
        authority_capsule_digest=binding.authority_capsule_digest,
        route_policy_revision=binding.route_policy_revision,
        control_cursor=state.control_cursor,
        rows=state.rows,
    )


def _require_contiguous(facts: Sequence[ControlFact]) -> None:
    """Refuse a fact sequence with a gap or a repeat.

    Raises:
        ValueError: The sequences are not exactly ``1..len(facts)`` in
            order.
    """
    for position, fact in enumerate(facts, start=1):
        if fact.sequence != position:
            raise ValueError(
                f"control ledger is not contiguous: expected sequence {position}, "
                f"found {fact.sequence}"
            )


def _advance(status: RunStatus, fact: ControlFact) -> RunStatus:
    """Return the status *fact* supports, given the status so far.

    A terminal Run stays terminal: a late confirmed effect arriving after
    the episode ended describes something that can no longer change.
    """
    if status in TERMINAL_RUN_STATUSES:
        return status
    if fact.phase is not ControlPhase.EFFECTED:
        return status
    if fact.disposition is not ControlDisposition.CONFIRMED:
        return status
    terminal = TERMINAL_EFFECT_STATUS[fact.control]
    return status if terminal is None else terminal


__all__ = [
    "ControlDispositionRow",
    "ControlLeaseDecision",
    "RunContract",
    "RunControlState",
    "decide_control_lease",
    "project_disposition",
    "reconstruct_run_contract",
    "reduce_run_control",
]
