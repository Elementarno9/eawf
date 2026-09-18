"""``semantic.call`` and ``semantic.result.read``: the agent tool door.

Two verbs, and between them they are the whole of what a Run's provider
process can reach. ``semantic.call`` takes one typed envelope and one
authority capsule, runs the nine pre-handler checks, and answers with a
receipt; ``semantic.result.read`` hands that receipt back by call
identity, so an answer survives the connection that asked for it.

Neither verb takes an addressed Run. The Run is the one the envelope
names, which is also the one the capsule was sealed for and the one the
receipt is filed under: a separate parameter would let a call aimed at
one Run be judged against another's ledger.

A refusal is an answer, not an error. A call that fails a check returns
its denied receipt with a closed code, the retry class that code implies
and the check that produced it, because a provider process branching on
an exception string cannot build a ladder. The two error paths that do
raise are the ones with no receipt to return: an envelope naming a Run
this root never recorded, and an admitted call for a tool whose handler
is not installed here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import CallId, SemanticCall
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator, require_native_call
from eawf.runtime.daemon.semantic_gateway import (
    SemanticCallReceipt,
    SemanticGatewayError,
    read_receipt,
    serve_semantic_call,
)

logger = logging.getLogger(__name__)


#: How the two verbs are spelled on the wire.
SEMANTIC_CALL_METHOD: Final = "semantic.call"
SEMANTIC_RESULT_READ_METHOD: Final = "semantic.result.read"


class SemanticCallParams(BaseModel):
    """What ``semantic.call`` is asked for.

    Attributes:
        call: The typed envelope, which names the Run, the tool, the
            payload and the digest that binds them.
        capsule: The authority the call claims to run under. It is
            submitted rather than looked up because the daemon stores
            only its digest, and it is refused unless it hashes to the
            digest the Run's recorded binding names.
    """

    model_config = ConfigDict(extra="forbid")

    call: SemanticCall
    capsule: AuthorityCapsule


class SemanticResultReadParams(BaseModel):
    """What ``semantic.result.read`` is asked for.

    Attributes:
        run_ref: The Run whose receipts are searched.
        call_id: The call to describe.
    """

    model_config = ConfigDict(extra="forbid")

    run_ref: RunUrn
    call_id: CallId


def receipt_response(receipt: SemanticCallReceipt) -> dict[str, Any]:
    """Return the wire answer describing one receipt.

    Args:
        receipt: The receipt to render.

    Returns:
        The JSON-mode record. It carries the result envelope verbatim, so
        the closed error code, its retry class and the check that refused
        all reach the caller without being restated here.
    """
    return receipt.model_dump(mode="json")


def _parsed[ParamsT: BaseModel](model: type[ParamsT], params: dict[str, Any]) -> ParamsT:
    """Validate request parameters, or refuse naming only the bad fields.

    The pydantic detail repeats the submitted values, and a refusal that
    reaches terminals and daemon logs must not carry them, so only the
    offending field paths travel.

    Raises:
        DaemonValidationError: The request does not parse.
    """
    try:
        return model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _refused(error: SemanticGatewayError) -> DaemonValidationError:
    """Return the wire form of a call the gateway cannot answer for."""
    return DaemonValidationError(f"validation_failed: {error.code}: {error.detail}")


@native_mutator(SEMANTIC_CALL_METHOD)
async def _call(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Guard one semantic call and answer with the receipt it earned.

    Returns:
        The receipt. A denied one names the check that refused and offers
        no shell command as a remedy, which the error model enforces.

    Raises:
        DaemonValidationError: The envelope names a Run this root never
            recorded, or the admitted tool reaches no handler here.
    """
    request = _parsed(SemanticCallParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        receipt = await asyncio.to_thread(
            serve_semantic_call,
            context,
            call=request.call,
            capsule=request.capsule,
            now=datetime.now(UTC),
        )
    except SemanticGatewayError as error:
        logger.info(f"_call refused code={error.code}")
        raise _refused(error) from error
    return receipt_response(receipt)


@register(SEMANTIC_RESULT_READ_METHOD)
async def _read_result(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the receipt one call was answered with.

    Raises:
        DaemonValidationError: This Run holds no receipt for that call.
    """
    authority = require_native_call(ctx, params)
    request = _parsed(SemanticResultReadParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        receipt = await asyncio.to_thread(
            read_receipt, context, run_ref=str(request.run_ref), call_id=request.call_id
        )
    except SemanticGatewayError as error:
        raise _refused(error) from error
    return receipt_response(receipt)


__all__ = [
    "SEMANTIC_CALL_METHOD",
    "SEMANTIC_RESULT_READ_METHOD",
    "SemanticCallParams",
    "SemanticResultReadParams",
    "receipt_response",
]
