"""Daemon-served planning, applying and export for the epoch-2 cutover.

Plan mode and export write nothing, so they need none of the authority
machinery a mutation does: no lock, no WAL, no generation. They are served
here anyway because the plan's approval digest is the token the apply is
checked against, and computing it on the same side of the wire that later
verifies it keeps the two from drifting apart.

The apply is the mutation, and it is the reason all three live together.
It takes the authority locks itself rather than going through the generic
mutation path, because what it locks is every authority surface at once
for the whole window rather than one document for one compare-and-swap.
Every refusal it can raise carries a stable importer code, so a caller
routes on ``migration_not_quiescent`` or ``workspace_not_registered``
rather than on prose.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.migration.epoch2.apply import (
    EPOCH2_APPLY_METHOD,
    Epoch2ApplyRequest,
    apply_cutover,
    apply_envelope,
)
from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.export import (
    EPOCH2_EXPORT_METHOD,
    Epoch2ExportRequest,
    export_epoch1,
)
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


@register(EPOCH2_APPLY_METHOD)
async def apply_epoch2(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Apply one approved cutover plan into a declared disposable tree.

    Args:
        ctx: Server context. Unused: the apply holds the authority locks
            itself for the whole write window, so it does not borrow the
            per-mutation lock the generic path provides.
        params: The request, validated through
            :class:`~eawf.kernel.migration.epoch2.apply.Epoch2ApplyRequest`.

    Returns:
        The apply envelope. ``status`` is ``already-selected`` with
        ``journal_rows`` at zero when the approved generation was already
        selected, which is the idempotent case and writes nothing.

    Raises:
        DaemonValidationError: When a request field is malformed or the
            cutover is refused. The refusal carries the importer's stable
            failure code -- ``migration_target_not_disposable``,
            ``workspace_not_registered``, ``migration_not_quiescent``,
            ``migration_plan_digest_stale`` among them -- so a caller
            matches on the code rather than on prose.
    """
    del ctx
    try:
        request = Epoch2ApplyRequest.model_validate(params)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    try:
        result = apply_cutover(request, applied_at=datetime.now(UTC))
    except MigrationRuleError as error:
        raise DaemonValidationError(f"validation_failed: {error.code}: {error}") from error
    logger.info(
        f"apply_epoch2 generation={result.generation_id} applied={result.applied} "
        f"journal_rows={result.journal_rows}"
    )
    return apply_envelope(result)


@register(EPOCH2_EXPORT_METHOD)
async def export_epoch2(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Serve a read-only export of every declared epoch-1 collection.

    Args:
        ctx: Server context. Unused: the export has no write path.
        params: The request, validated through
            :class:`~eawf.kernel.migration.epoch2.export.Epoch2ExportRequest`.

    Returns:
        The export envelope.

    Raises:
        DaemonValidationError: When a request field is malformed or the
            corpus cannot be read through the declared surfaces.
    """
    del ctx
    try:
        request = Epoch2ExportRequest.model_validate(params)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    try:
        payload = export_epoch1(request)
    except MigrationRuleError as error:
        raise DaemonValidationError(f"validation_failed: {error.code}: {error}") from error
    return payload


__all__ = ["apply_epoch2", "export_epoch2", "plan_epoch2"]
