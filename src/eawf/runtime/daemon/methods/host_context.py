"""``runtime.host.context.boundary``: a host compaction, on the stream of the Run it compacts.

A host harness compacts a long session by summarising it, and the summary is all the
model keeps. What the Run is bound to -- its authority capsule, its compiled spec, its
criteria, its scope and exact base, and the decision receipts it has earned -- must come
through that unchanged, and it can, because none of it lives in the context: the store
holds it. The host's pre-compaction hook calls this verb with ``compacting``, which
snapshots those anchors as the store holds them onto the Run's stream. Its post-
compaction session start calls it with ``resumed``, which reads them again, names every
anchor that no longer matches the snapshot, and answers with a restatement the hook
hands the model in place of whatever the summary kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.events import (
    ContextBoundaryPayload,
    ContractAnchorName,
    ContractAnchors,
    RunEventKind,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.store.ledger import read_ledger_records
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.host_calls import host_receipts
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.methods.run import append_run_event_in_session
from eawf.runtime.daemon.native_dispatch import run_binding_of, run_ledger
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.run_events import RunEventAppend, run_events_of
from eawf.runtime.daemon.semantic_gateway import run_receipts
from eawf.runtime.runtimes.host_transcript import HostHarness

logger = logging.getLogger(__name__)

#: State a host compaction on the Run its session runs as.
HOST_CONTEXT_BOUNDARY_METHOD: Final = "runtime.host.context.boundary"

#: The anchors compared across a compaction, in the order a mismatch names them.
_COMPARED: Final[tuple[ContractAnchorName, ...]] = (
    "authority_capsule_digest",
    "compiled_spec_digest",
    "criteria_digest",
    "scope_digest",
    "receipts_digest",
)

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class HostContextBoundary(BaseModel):
    """Params of :data:`HOST_CONTEXT_BOUNDARY_METHOD`, as the host's hook reports them.

    Attributes:
        harness: The host harness compacting.
        host_session_id: The session being compacted.
        boundary: ``compacting`` before the host summarises, ``resumed`` after.
        trigger: Whether the operator or the host's threshold asked for it.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    boundary: Literal["compacting", "resumed"]
    trigger: Literal["manual", "auto"] | None = None


class HostContextAnswer(BaseModel):
    """What the verb answers with.

    Attributes:
        run_ref: The Run the boundary was stated on.
        event_ref: The stream line it was stated as.
        anchors: The anchors as the store holds them.
        drift: The anchors that moved since the last snapshot.
        restatement: The anchors in words, for the host to hand its model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    event_ref: str
    anchors: ContractAnchors
    drift: tuple[ContractAnchorName, ...]
    restatement: str


def contract_anchors(session: RootSession, run: QualifiedUrn) -> ContractAnchors:
    """Read what *run* is bound to from the store.

    Args:
        session: An open session whose locks cover *run*.
        run: The Run.

    Returns:
        The anchors; a Run dispatched without a capsule has none of the
        capsule's digests, and every Run has its receipts.
    """
    binding = run_binding_of(read_ledger_records(run_ledger(session)), run)
    capsule = binding.capsule if binding is not None else None
    receipts = [item.receipt_id for item in run_receipts(session, run)]
    receipts.extend(item.receipt_id for item in host_receipts(session, run))
    return ContractAnchors(
        authority_capsule_digest=binding.authority_capsule_digest if binding else None,
        compiled_spec_digest=binding.compiled_spec_digest if binding else None,
        criteria_digest=capsule.criteria_digest if capsule else None,
        scope_digest=capsule.scope_digest if capsule else None,
        receipts_digest=canonical_digest({"receipts": receipts}),
        receipt_count=len(receipts),
    )


def anchor_drift(before: ContractAnchors, after: ContractAnchors) -> tuple[ContractAnchorName, ...]:
    """Return every anchor *after* no longer holds as *before* recorded it."""
    return tuple(name for name in _COMPARED if getattr(before, name) != getattr(after, name))


def _last_snapshot(session: RootSession, run: QualifiedUrn) -> ContractAnchors | None:
    """Return the anchors of the Run's latest ``compacting`` boundary, if it has one."""
    for line in reversed(run_events_of(read_ledger_records(run_ledger(session)), run)):
        payload = line.payload
        if isinstance(payload, ContextBoundaryPayload) and payload.boundary == "compacting":
            return payload.anchors
    return None


def restate(run: QualifiedUrn, anchors: ContractAnchors, drift: tuple[str, ...]) -> str:
    """Return the anchors as the words a compacted model is handed.

    Args:
        run: The Run.
        anchors: Its anchors as the store holds them.
        drift: The anchors that moved across the compaction.
    """
    held = (
        f"Run {run.entity_key} is bound to authority capsule "
        f"{anchors.authority_capsule_digest or 'none'}, compiled spec "
        f"{anchors.compiled_spec_digest or 'none'}, criteria {anchors.criteria_digest or 'none'} "
        f"and scope {anchors.scope_digest or 'none'}; {anchors.receipt_count} decision "
        f"receipts stand ({anchors.receipts_digest})."
    )
    if drift:
        return (
            f"{held} contract_mismatch: {', '.join(drift)} changed across the compaction; "
            "stop and hand back rather than act on the summary."
        )
    return f"{held} The compaction changed none of them; the store, not the summary, holds them."


def state_context_boundary(
    context: Epoch2RootContext,
    authority: RootAuthority,
    args: HostContextBoundary,
    *,
    now: datetime,
) -> HostContextAnswer:
    """Snapshot or re-verify the anchors of the Run on the host's session.

    Raises:
        DaemonValidationError: The host session is on no one live Run.
    """
    run = host_run(authority, args.host_session_id)
    with context.session([run]) as session:
        anchors = contract_anchors(session, run)
        drift: tuple[ContractAnchorName, ...] = ()
        if args.boundary == "resumed":
            snapshot = _last_snapshot(session, run)
            drift = anchor_drift(snapshot, anchors) if snapshot is not None else ()
        body = f"{args.host_session_id}:{args.boundary}:{now.isoformat()}"
        event_ref = f"EVT-{hashlib.sha256(body.encode()).hexdigest()[:32]}"
        answer = append_run_event_in_session(
            session,
            RunEventAppend(
                urn=run,
                event_ref=event_ref,
                run_sequence=1,
                event_kind=RunEventKind.CONTEXT_BOUNDARY,
                provenance="provider_native",
                payload=ContextBoundaryPayload(
                    boundary=args.boundary, trigger=args.trigger, anchors=anchors, drift=drift
                ),
                actor=HARNESS_ACTORS[args.harness],
            ),
            now=now,
            at_tail=True,
        )
    logger.info(
        f"state_context_boundary run={run.entity_key} boundary={args.boundary} "
        f"drift={len(drift)} disposition={answer.disposition}"
    )
    return HostContextAnswer(
        run_ref=str(run),
        event_ref=event_ref,
        anchors=anchors,
        drift=drift,
        restatement=restate(run, anchors, drift),
    )


@native_mutator(HOST_CONTEXT_BOUNDARY_METHOD)
async def _state_host_context_boundary(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """State a host compaction on the Run its session runs as."""
    args = native_params(HostContextBoundary, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(
        state_context_boundary, context, authority, args, now=datetime.now(UTC)
    )
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_CONTEXT_BOUNDARY_METHOD",
    "HostContextAnswer",
    "HostContextBoundary",
    "anchor_drift",
    "contract_anchors",
    "restate",
    "state_context_boundary",
]
