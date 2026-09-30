"""``runtime.host.tool.deny``: a host tool call the data-loss guard refused, as a decision.

The host harness's pre-tool hook refuses a call matching one of the four data-loss
patterns before the call runs, without waiting on the daemon. It then reports the
refusal here, and the verb files it as a :class:`SandboxDecision` on the Run the host
session runs as, so a denial is on the record beside the gateway's own. The rule's value
and the policy revision come from the guard's own table, never from the request.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.runtime.sandbox_decision import SandboxDecision, SandboxDecisionOutcome
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.host_calls import host_call_id, host_tool_id
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.runtimes.host_transcript import HostHarness
from eawf.runtime.sandbox.data_loss import DATA_LOSS_POLICY_REVISION, RULE_VALUES

logger = logging.getLogger(__name__)

#: Record one host tool call the data-loss guard refused.
HOST_TOOL_DENY_METHOD: Final = "runtime.host.tool.deny"

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]

#: The rules a denial may name: the four patterns and the fail-closed refusal.
DenyRule = Literal[
    "foreign_worktree_mutation",
    "out_of_root_worktree",
    "drifted_commit",
    "canonical_store_edit",
    "unjudged",
]


class HostToolDeny(BaseModel):
    """What the guard reports about a call it refused.

    Attributes:
        harness: The host harness whose call was refused.
        host_session_id: The session, or the subagent, the call was made in.
        tool_name: The host's name for the tool.
        host_call_key: The host's own id of the call.
        rule: The pattern that refused, or ``unjudged`` when the guard could not
            judge the call and so refused it.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    tool_name: _HostText
    host_call_key: _HostText
    rule: DenyRule


def _deny(
    context: Epoch2RootContext, authority: RootAuthority, args: HostToolDeny, *, now: datetime
) -> SandboxDecision:
    """File the refusal as a decision on the Run the session runs as."""
    run = host_run(authority, args.host_session_id)
    tool_id = host_tool_id(args.tool_name)
    decision = SandboxDecision(
        call_id=host_call_id(run, args.host_call_key),
        run_ref=run,
        tool_id=tool_id,
        decision=SandboxDecisionOutcome.DENIED,
        rule=f"data_loss.{args.rule}",
        rule_value=RULE_VALUES[args.rule],
        reason=f"{tool_id} · {args.rule}",
        policy_revision=DATA_LOSS_POLICY_REVISION,
        decided_at=now,
    )
    with context.session([run]) as session:
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.RECEIPT,
                record_key=decision.key,
                status=decision.decision.value,
                recorded_at=now,
                payload=decision.model_dump(mode="json"),
            ),
        )
    logger.info(f"host_tool_deny run={run.entity_key} rule={args.rule} tool={tool_id}")
    return decision


@native_mutator(HOST_TOOL_DENY_METHOD)
async def _deny_host_tool(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one host tool call the data-loss guard refused."""
    args = native_params(HostToolDeny, params)
    context = ctx.native_root_context(authority.root)
    decision = await asyncio.to_thread(_deny, context, authority, args, now=datetime.now(UTC))
    return {"run_ref": str(decision.run_ref), "decision_key": decision.key}


__all__ = [
    "HOST_TOOL_DENY_METHOD",
    "DenyRule",
    "HostToolDeny",
]
