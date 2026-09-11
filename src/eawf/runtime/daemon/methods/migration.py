"""Daemon-served read-only planning for the epoch-1 to epoch-2 cutover.

Plan mode writes nothing, so it needs none of the authority machinery a
mutation does: no lock, no WAL, no generation. It is served here anyway
because the plan's approval digest is the token a later apply is checked
against, and an apply is a mutation the daemon alone may run. Computing
the digest on the same side of the wire that will later verify it keeps
the two from drifting apart.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.plan_mode import (
    EPOCH2_PLAN_METHOD,
    Epoch2PlanRequest,
    plan_cutover,
    plan_envelope,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register

logger = logging.getLogger(__name__)


@register(EPOCH2_PLAN_METHOD)
async def plan_epoch2(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Serve a read-only cutover plan over one staged epoch-1 corpus.

    Args:
        ctx: Server context. Unused: plan mode touches no daemon-owned
            state, which is the property that makes it safe to serve
            without a lock.
        params: The request, validated through
            :class:`~eawf.kernel.migration.epoch2.plan_mode.Epoch2PlanRequest`.

    Returns:
        The plan envelope.

    Raises:
        DaemonValidationError: When a request field is malformed or an
            importer rule refuses the corpus. The refusal carries the
            rule's stable failure code so a caller matches on the code
            rather than on prose.
    """
    del ctx
    try:
        request = Epoch2PlanRequest.model_validate(params)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    try:
        plan = plan_cutover(request, sealed_at=datetime.now(UTC))
    except MigrationRuleError as error:
        raise DaemonValidationError(f"validation_failed: {error.code}: {error}") from error
    logger.info(
        f"plan_epoch2 approval={plan.approval_digest[:12]} "
        f"unresolved={len(plan.manifest.unresolved_rows)}"
    )
    return plan_envelope(plan)


__all__ = ["plan_epoch2"]
