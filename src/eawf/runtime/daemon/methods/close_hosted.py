"""The daemon-hosted close entry for a caller with no interactive session.

A close that no agent session is attached to used to have one route past a
gate-bearing wave's falsifiers: the daemonless bypass, which skips the gates
entirely behind an operator waiver. ``close.host`` is that route's
replacement. It creates and schedules its attempt through the SAME helpers
:mod:`eawf.runtime.daemon.methods.close` uses for ``close.submit``, so the
gate set, the gate receipt ids and the
:class:`~eawf.kernel.state.enums.CloseFailureKind` vocabulary are one
implementation rather than two that happen to agree.

The entry lives in its own module so the durable close engine keeps one
reason to change; the hosted lane's own contract (its params, its response
envelope, its waiver tally) changes with the operator surface instead.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.close import (
    _TERMINAL_STATUSES,
    CloseStatusResult,
    CloseSubmitParams,
    _attempt_payload,
    _create_attempt,
    _repo_root,
    _schedule,
    _state_path,
)

logger = logging.getLogger(__name__)


class CloseHostParams(CloseSubmitParams):
    """Parameters for ``close.host`` -- a close with no interactive session.

    Deliberately the SAME input set as :class:`CloseSubmitParams`, adding no
    field of its own. A close's durable identity is its frozen inputs, not who
    issued it, so a hosted close and an interactive close of the same wave
    revision resolve to the same idempotency key, the same attempt id and
    therefore the same gate receipt ids. Introducing an issuer-shaped field
    here would fork that identity and make the two lanes incomparable.
    """


class HostedCloseResult(CloseStatusResult):
    """``close.host`` response: the durable attempt plus its bypass tally.

    Attributes:
        hosted: Always ``True`` -- the daemon answered, so it owns the close
            and runs every gate out of process. The field is explicit so a
            caller reading the payload never has to infer the lane from the
            method it happened to call.
        waiver_count: Persisted waiver rows the wave carries. A hosted close
            adds none, so a non-zero value is the operator's own prior waiver
            and not a bypass this close took.
    """

    hosted: bool
    waiver_count: int = Field(ge=0)


@register("close.host")
async def host(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Host one gate-bearing close for a caller with no interactive session.

    Creation and scheduling go through the same
    :func:`~eawf.runtime.daemon.methods.close._create_attempt` /
    :func:`~eawf.runtime.daemon.methods.close._schedule` pair ``close.submit``
    uses, so both lanes produce identical gates, receipts and failure kinds.

    Gate work runs in the crash-isolated child interpreter the close worker
    binds (:func:`~eawf.runtime.daemon.gate_execution.run_gate_out_of_process`,
    against the sandboxed runtime dir + ledger of
    :func:`~eawf.runtime.daemon.gate_execution.gate_sandbox`), so a gate that
    hard-exits leaves the daemon serving and the attempt resumable.

    Args:
        ctx: Daemon method context (state path, event path, WAL dir).
        params: Raw JSON-RPC params validated as :class:`CloseHostParams`.

    Returns:
        A :class:`HostedCloseResult` payload: the durable attempt, whether a
        worker was started, the hosted flag, and the wave's waiver tally.

    Raises:
        ValueError: The wave is unknown, cannot start a close, or its frozen
            inputs no longer match live authority -- identical to
            ``close.submit``, which shares the creation path.
    """
    from eawf.kernel.store.paths import store_dir
    from eawf.workflow.verify.hosted_close import count_scope_waivers

    args = CloseHostParams.model_validate(params)
    repo_root = _repo_root(ctx, args.repo_root)
    attempt = _create_attempt(
        ctx,
        repo_root=repo_root,
        args=CloseSubmitParams.model_validate(args.model_dump()),
    )
    backgrounded = False
    if attempt.status not in _TERMINAL_STATUSES:
        backgrounded = _schedule(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
        )
    waiver_count = count_scope_waivers(
        store_dir(_state_path(ctx, repo_root)),
        scope_id=args.wave_id,
    )
    logger.info(
        f"host wave={args.wave_id!r} attempt={attempt.id!r} "
        f"backgrounded={backgrounded} waiver_count={waiver_count}"
    )
    return HostedCloseResult(
        attempt=_attempt_payload(attempt),
        backgrounded=backgrounded,
        hosted=True,
        waiver_count=waiver_count,
    ).model_dump(mode="json")


__all__ = [
    "CloseHostParams",
    "HostedCloseResult",
    "host",
]
