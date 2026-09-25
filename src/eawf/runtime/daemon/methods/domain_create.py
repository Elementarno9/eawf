"""The per-entity create verbs of a Track, a Milestone, a Batch, a Task and a Run.

Each verb is ``domain.<entity>.create`` and admits one record of its own
kind through :func:`~eawf.runtime.daemon.epoch2_create.run_create`. The
verb is fixed per kind for the same reason the lifecycle verbs are: the
transaction derives its machine from the URN, so a verb handed a URN of
another kind would admit that kind's record under this verb's name. That
mismatch is refused here, before the transaction opens a session.

The handler is dispatch and rendering. Every predicate about whether the
record may exist -- the create document, the cursor, the free key, the
live parent -- is decided inside the transaction, under the root's locks,
and a refusal comes back as the same machine envelope a lifecycle verb
answers with.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_transaction import (
    LIFECYCLE_ENTITIES,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    accepted_envelope,
    refused_envelope,
    schema_refusal,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator

logger = logging.getLogger(__name__)


#: The dotted JSON-RPC name of each kind's create verb, in the order the
#: kinds nest: a Track holds Milestones, a Milestone holds Batches, a Batch
#: holds Tasks and a Task is executed by Runs.
DOMAIN_CREATE_METHODS: Final[dict[EntityKind, str]] = {
    kind: f"domain.{kind.value}.create" for kind in LIFECYCLE_ENTITIES
}


def _kind_refusal(method: str, *, kind: EntityKind, request: CreateRequest) -> dict[str, Any]:
    """Return the refusal of a create verb pointed at another kind's URN.

    Args:
        method: The verb the client asked for.
        kind: The kind the verb admits.
        request: The already-validated request parameters.

    Returns:
        An ``error`` envelope carrying ``identity_kind_mismatch``, as a
        JSON-mode mapping. Neither revision is filled, because nothing
        was read.
    """
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
        detail=f"{method} admits a {kind.value} but the request names a {request.urn.kind.value}",
        entity_ref=str(request.urn),
        remediation=f"Name a {kind.value} URN, or call that entity's own create verb.",
    )
    return refused_envelope(refusal, operation=method).model_dump(mode="json")


async def _create(
    ctx: MethodContext,
    params: dict[str, Any],
    authority: RootAuthority,
    *,
    kind: EntityKind,
) -> dict[str, Any]:
    """Admit one record of *kind* into the addressed epoch-2 tree.

    Args:
        ctx: Server context, which owns the per-root native contexts and
            the subscription bus.
        params: The request parameters, less the routing key the fence
            already consumed.
        authority: The epoch-2 answer the fence resolved for the tree.
        kind: The kind this handler was registered for.

    Returns:
        The machine envelope, as a JSON-mode mapping. A commit whose
        post-commit publish did not reach the projection still answers
        ``ok`` and carries the ``projection_degraded`` warning. A retry the
        transaction answered from its receipt store publishes nothing.
    """
    method = DOMAIN_CREATE_METHODS[kind]
    try:
        request = CreateRequest.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=method).model_dump(mode="json")
    if request.urn.kind is not kind:
        return _kind_refusal(method, kind=kind, request=request)
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(
            run_create, context=context, request=request, now=datetime.now(UTC)
        )
    except TransactionRefusedError as refusal:
        logger.info(f"_create refused method={method} code={refusal.code.value}")
        return refused_envelope(refusal, operation=method).model_dump(mode="json")
    warnings: tuple[str, ...] = ()
    if committed.envelope is not None and not publish_projection(ctx.bus, committed.envelope):
        warnings = (PROJECTION_DEGRADED,)
    return accepted_envelope(committed.receipt, operation=method, warnings=warnings).model_dump(
        mode="json"
    )


def _register(kind: EntityKind) -> None:
    """Register one kind's fenced create handler under its dotted name.

    Args:
        kind: The kind the handler admits.

    Raises:
        ValueError: The name is already registered.
    """
    method = DOMAIN_CREATE_METHODS[kind]

    async def handler(
        ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
    ) -> dict[str, Any]:
        return await _create(ctx, params, authority, kind=kind)

    handler.__name__ = method.replace(".", "_")
    handler.__doc__ = f"Admit one {kind.value} into the addressed epoch-2 tree."
    native_mutator(method)(handler)


for _kind in DOMAIN_CREATE_METHODS:
    _register(_kind)


__all__ = ["DOMAIN_CREATE_METHODS"]
