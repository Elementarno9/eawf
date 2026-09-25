"""Assembling a Batch's integrate request from the records its plan names.

``runtime.delivery.assemble``: the request ``/integrate apply`` sends.

A delivery request is part durable fact and part reference. The skill
that drives integration reaches the tree only over the daemon's verbs, so
it cannot read the Batch, its Tasks and their sealed candidates itself;
before this verb it stopped naming the fields it would have had to
invent. The verb resolves the plan against the tree and answers the
request with every field derived from records -- the branch, one commit
subject per sealed candidate, the affected criteria -- and the typed
references the caller presented checked rather than trusted: each exit
resolves to a record of the kind it lands on, the diagnostic evidence is
held, and the base binds this Batch and is the commit every candidate
started from.

The verb reads and writes nothing. The request it answers is the one the
integrate verb is then asked, and assembling the same plan twice names
the same idempotency key, so a retried skill replays rather than
integrating twice.

It sits behind the native fence because the records it reads exist only
in an epoch-2 tree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.delivery.integration import ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, BatchUrn, EvidenceUrn
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.workflow.delivery.request_assembly import (
    AssemblyRefusedError,
    DeliveryReferences,
    assemble_integrate_request,
    resolve_batch_plan,
)

logger = logging.getLogger(__name__)


#: The verb ``/integrate apply`` asks for the request it then sends.
DELIVERY_ASSEMBLE_METHOD: Final = "runtime.delivery.assemble"


class DeliveryAssembleParams(BaseModel):
    """Params of :data:`DELIVERY_ASSEMBLE_METHOD`.

    Attributes:
        urn: The Batch whose plan is assembled.
        actor: The principal the assembled request is attributed to.
        base: The exact revision the Batch starts from.
        exit_refs: Where each conflict exit lands.
        diagnostic_ref: The evidence a conflict diagnostic is filed against.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    base: RevisionBinding
    exit_refs: dict[ConflictExitKind, AnyEntityUrn] = Field(min_length=1)
    diagnostic_ref: EvidenceUrn


def _assemble_params(params: dict[str, Any]) -> DeliveryAssembleParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The detail is
            reduced to field paths so a submitted value never reaches a log.
    """
    try:
        return DeliveryAssembleParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def assemble_delivery(context: Epoch2RootContext, args: DeliveryAssembleParams) -> dict[str, Any]:
    """Return the integrate request of the Batch *args* names.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.

    Returns:
        The integrate request, in its wire form.

    Raises:
        DaemonValidationError: A record the plan names does not resolve,
            a reference disagrees with the tree, the Batch has no target
            branch, or no planned Task has sealed a candidate. The
            assembly code leads the message so a skill can route on it.
    """
    references = DeliveryReferences(
        batch_ref=args.urn,
        base=args.base,
        exit_refs=args.exit_refs,
        diagnostic_ref=args.diagnostic_ref,
    )
    try:
        request = assemble_integrate_request(
            resolve_batch_plan(context, references), actor=args.actor
        )
    except AssemblyRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    logger.info(f"assemble_delivery batch={args.urn.entity_key} candidates={len(request.subjects)}")
    return request.model_dump(mode="json")


@native_mutator(DELIVERY_ASSEMBLE_METHOD)
async def _assemble_delivery(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Answer the integrate request one Batch's plan assembles to."""
    args = _assemble_params(params)
    context = ctx.native_root_context(authority.root)
    return await asyncio.to_thread(assemble_delivery, context, args)


__all__ = [
    "DELIVERY_ASSEMBLE_METHOD",
    "DeliveryAssembleParams",
    "assemble_delivery",
]
