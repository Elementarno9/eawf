"""The gateway's door for a tool call the host harness made and ran itself.

A Claude Code session calls its own tools, so the daemon never answers them. It can
still account for them the way it accounts for a brokered call: the host's hooks report
each call as it is requested and again once it has run, and this module gives it a real
gateway identity -- a :data:`~eawf.kernel.runtime.semantic.CallId` -- and, once it has
run, a receipt whose result names the call's bounded, scrubbed output as a stored
artifact. Nothing is executed here: the receipt records an answer the host already gave.

The call id is derived from the Run and the host's own id of the call, so the report
before the call and the report after it name the same call without the daemon keeping
anything between them, and a retried hook restates what is already standing: the lines
it would append are named by the call and the phase, and a second result is answered
with the receipt the first one earned.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime
from typing import Final, Literal, Self

from pydantic import model_validator

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.events import ToolPayload
from eawf.kernel.runtime.provider import RuntimeRecord, ToolCapabilityId
from eawf.kernel.runtime.semantic import (
    TOOL_SCHEMA_VERSION,
    CallId,
    ReceiptId,
    SemanticResult,
    SemanticToolErrorCode,
    error_for,
)
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession, canonical_entity_urn
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.tool_events import state_tool_phase

logger = logging.getLogger(__name__)

#: The discriminator of a host call receipt on the receipt ledger.
HOST_RECEIPT_PAYLOAD_KIND: Final = "host_call_receipt"

#: How many hex characters of a digest the minted ids carry.
_ID_HEX: Final = 16

#: The code a host call that ran and failed ends with: a changed request is what
#: would get past it.
HOST_FAILURE_CODE: Final = SemanticToolErrorCode.PAYLOAD_INVALID


class HostCallReceipt(RuntimeRecord):
    """The durable record of what one host tool call was answered with.

    Attributes:
        payload_kind: The discriminator separating it from a brokered receipt.
        schema_version: Version of this record's shape.
        receipt_id: The receipt's own identifier.
        call_id: The call it answers.
        run_ref: The Run the call was made in.
        tool_id: The host tool, in the tool-id grammar.
        result: The answer: the output artifact on success, the error with the
            output as its diagnostic on failure.
        recorded_at: When the daemon filed it.
    """

    payload_kind: Literal["host_call_receipt"] = HOST_RECEIPT_PAYLOAD_KIND
    schema_version: Literal["host-call-receipt/v1"] = "host-call-receipt/v1"
    receipt_id: ReceiptId
    call_id: CallId
    run_ref: RunUrn
    tool_id: ToolCapabilityId
    result: SemanticResult
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _receipt_answers_the_call_it_names(self) -> Self:
        """Refuse a receipt whose result answers another call.

        Raises:
            ValueError: The result's call, Run or receipt differs from the receipt's.
        """
        result = self.result
        if (result.call_id, result.receipt_id) != (self.call_id, self.receipt_id):
            raise ValueError("the result does not answer the receipt's call")
        if result.run_ref != self.run_ref:
            raise ValueError("result.run_ref does not match the receipt's run_ref")
        return self


def host_tool_id(tool_name: str) -> str:
    """Return a host tool's name in the tool-id grammar: lower snake case, letter first."""
    slug = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return (slug if slug[:1].isalpha() else f"tool_{slug}")[:64]


def host_call_id(run_ref: QualifiedUrn, host_call_key: str) -> str:
    """Return the call id one host call is known by within its Run."""
    body = f"{canonical_entity_urn(run_ref)}:{host_call_key}"
    return f"call-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:_ID_HEX]}"


def host_receipts(session: RootSession, run_ref: str | QualifiedUrn) -> tuple[HostCallReceipt, ...]:
    """Return one Run's host call receipts, in ledger order.

    Raises:
        ValidationError: A line claims to be a host call receipt and does not
            validate as one, which means the ledger is corrupt.
    """
    wanted = canonical_entity_urn(run_ref)
    receipts = (
        HostCallReceipt.model_validate(item.payload)
        for item in read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
        if item.payload.get("payload_kind") == HOST_RECEIPT_PAYLOAD_KIND
    )
    return tuple(item for item in receipts if canonical_entity_urn(item.run_ref) == wanted)


def observe_host_call(
    context: Epoch2RootContext,
    *,
    run_ref: QualifiedUrn,
    tool_name: str,
    host_call_key: str,
    output: str | None,
    failed: bool,
    actor: str,
    now: datetime,
) -> tuple[str, HostCallReceipt | None]:
    """Account for one host tool call on its Run's transcript, and receipt it once run.

    Args:
        context: The native context of the root the Run belongs to.
        run_ref: The Run the host session runs as.
        tool_name: The host's name for the tool.
        host_call_key: The host's own id of the call.
        output: What the call returned, raw; ``None`` while it has not run. It is
            bounded and scrubbed before it is stored.
        failed: Whether the call that ran failed.
        actor: The principal the host's reports are attributed to.
        now: The daemon's recording clock.

    Returns:
        The call id, and the receipt once the call has run.
    """
    call_id = host_call_id(run_ref, host_call_key)
    tool_id = host_tool_id(tool_name)

    def state(payload: ToolPayload, session: RootSession) -> None:
        state_tool_phase(
            session,
            run_ref=run_ref,
            payload=payload,
            provenance="provider_native",
            actor=actor,
            now=now,
        )

    with context.session([run_ref]) as session:
        state(ToolPayload(call_ref=call_id, tool_id=tool_id, phase="requested"), session)
        if output is None:
            return call_id, None
        receipt = next((r for r in host_receipts(session, run_ref) if r.call_id == call_id), None)
        if receipt is None:
            receipt = _receipt(
                session,
                run_ref=run_ref,
                call_id=call_id,
                tool_id=tool_id,
                output=output,
                failed=failed,
                now=now,
            )
        state(ToolPayload(call_ref=call_id, tool_id=tool_id, phase="accepted"), session)
        error = receipt.result.error
        state(
            ToolPayload(
                call_ref=call_id,
                tool_id=tool_id,
                phase="result",
                result_ref=receipt.receipt_id if error is None else None,
                error_code=None if error is None else error.code,
            ),
            session,
        )
    logger.info(f"observe_host_call tool={tool_id} call={call_id} status={receipt.result.status}")
    return call_id, receipt


def _receipt(
    session: RootSession,
    *,
    run_ref: QualifiedUrn,
    call_id: str,
    tool_id: str,
    output: str,
    failed: bool,
    now: datetime,
) -> HostCallReceipt:
    """File the call's output as an artifact and the receipt that names it."""
    artifact = file_content(session, run_ref=run_ref, text=output, now=now)
    receipt_id = f"receipt-{secrets.token_hex(_ID_HEX // 2)}"
    result = SemanticResult(
        call_id=call_id,
        run_ref=run_ref,
        receipt_id=receipt_id,
        status="failed" if failed else "succeeded",
        result_schema_version=TOOL_SCHEMA_VERSION,
        output_ref=None if failed else artifact,
        error=(
            error_for(
                HOST_FAILURE_CODE,
                message=f"the host's {tool_id} call ran and failed",
                diagnostic_ref=artifact,
            )
            if failed
            else None
        ),
        completed_at=now,
    )
    receipt = HostCallReceipt(
        receipt_id=receipt_id,
        call_id=call_id,
        run_ref=run_ref,
        tool_id=tool_id,
        result=result,
        recorded_at=now,
    )
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key=call_id,
            status=result.status,
            recorded_at=now,
            payload=receipt.model_dump(mode="json"),
        ),
    )
    return receipt


__all__ = [
    "HOST_FAILURE_CODE",
    "HOST_RECEIPT_PAYLOAD_KIND",
    "HostCallReceipt",
    "host_call_id",
    "host_receipts",
    "host_tool_id",
    "observe_host_call",
]
