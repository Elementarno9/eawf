"""The sandbox-decision record: one authorisation a Run's call was given or refused.

The gateway answers every call a provider process sends by walking its checks, and the
walk ends in exactly one of two places: a check refused the call, or every check passed
and the grant admitted it. A :class:`SandboxDecision` states which, for which Run and
tool, naming the rule that decided with the value it held and the revision of the policy
the rule belonged to. An admitted call is recorded as well as a refused one, because a
log of denials alone cannot say what the sandbox let through.

A decision never carries the raw target of the call. The paths or arguments a call named
stay on its receipt, which only the diagnostics read, so a surface drawing decisions
cannot print one by accident.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from enum import StrEnum
from typing import Final, Literal

from eawf.kernel.runtime.provider import RuntimeRecord, ToolCapabilityId
from eawf.kernel.runtime.semantic import CallId, SemanticToolId
from eawf.kernel.state.epoch2.base import NonEmptyStr, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord

logger = logging.getLogger(__name__)

#: The discriminator a decision line carries in the receipt ledger it shares.
SANDBOX_DECISION_KIND: Final = "sandbox_decision"

#: The prefix a decision's ledger key carries before the call it decided.
SANDBOX_DECISION_KEY_PREFIX: Final = "SBD-"


class SandboxDecisionOutcome(StrEnum):
    """What the sandbox answered a call with."""

    ALLOWED = "allowed"
    DENIED = "denied"


class SandboxDecision(RuntimeRecord):
    """One authorisation decision the gateway made for one call.

    Attributes:
        payload_kind: The discriminator separating a decision line from a receipt.
        schema_version: Version of this record's shape.
        call_id: The call decided.
        run_ref: The Run that made the call.
        tool_id: The tool the call named: a catalog tool, or a host tool in the
            tool-id grammar when a host harness guard refused the host's own call.
        decision: Whether the call was allowed or denied.
        rule: The rule that decided: the check that refused, or the grant that admitted.
        rule_value: The value that rule held when it decided.
        reason: Why, in words fit for a row: the tool and the refusal code, never the
            raw target.
        policy_revision: The revision of the sandbox policy the rule belonged to.
        decided_at: When the gateway decided.
    """

    payload_kind: Literal["sandbox_decision"] = SANDBOX_DECISION_KIND
    schema_version: Literal["sandbox-decision/v1"] = "sandbox-decision/v1"
    call_id: CallId
    run_ref: RunUrn
    tool_id: SemanticToolId | ToolCapabilityId
    decision: SandboxDecisionOutcome
    rule: NonEmptyStr
    rule_value: NonEmptyStr
    reason: NonEmptyStr
    policy_revision: StrictPositiveInt
    decided_at: UtcDatetime

    @property
    def key(self) -> str:
        """Return the ledger key the decision is filed under."""
        return f"{SANDBOX_DECISION_KEY_PREFIX}{self.call_id}"


def sandbox_decisions(records: Iterable[LedgerRecord]) -> tuple[SandboxDecision, ...]:
    """Return every decision among ledger lines, in ledger order.

    Args:
        records: The receipt ledger's lines.

    Returns:
        The decisions; a line of any other kind is skipped.

    Raises:
        pydantic.ValidationError: A line claims to be a decision and does not validate
            as one, which means the ledger is corrupt.
    """
    found = tuple(
        SandboxDecision.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == SANDBOX_DECISION_KIND
    )
    logger.debug(f"sandbox_decisions decisions={len(found)}")
    return found


__all__ = [
    "SANDBOX_DECISION_KEY_PREFIX",
    "SANDBOX_DECISION_KIND",
    "SandboxDecision",
    "SandboxDecisionOutcome",
    "sandbox_decisions",
]
