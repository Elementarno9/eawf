"""``budget_notice.*`` JSON-RPC methods: one recipient's notices and dispositions.

The daemon owns the notice ledger's writes. A client lists the notices
addressed to the principal it acts as, takes delivery of the revisions not
yet delivered to that principal, and records what the principal did to
one of them. None of these touches the work a notice describes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.types import UtcDatetime
from eawf.runtime.budget.notice_inbox import (
    NoticeDisposition,
    NoticeDispositionError,
    deliver_pending,
    dispose_notice,
    inbox_for,
)
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    note_cross_root_serve,
    register,
)


class _PrincipalParams(BaseModel):
    """Params naming the recipient a call acts for, and the tree it is about.

    ``repo_root`` addresses a tree other than the one the daemon was started on, as a
    console attached to a repository does; omitted, the daemon's own tree is meant.
    """

    model_config = ConfigDict(extra="forbid")

    principal: PrincipalKey
    repo_root: str | None = None


class _DisposeParams(BaseModel):
    """Params of ``budget_notice.dispose``."""

    model_config = ConfigDict(extra="forbid")

    notice_key: str
    principal: PrincipalKey
    disposition: NoticeDisposition
    expected_revision: StrictPositiveInt
    snooze_until: UtcDatetime | None = None
    repo_root: str | None = None


def _ledger_path(ctx: MethodContext, repo_root: str | None) -> Path:
    """Return the notice ledger beside the addressed tree's state file."""
    note_cross_root_serve(ctx, repo_root=repo_root, command="budget_notice")
    if repo_root:
        return notices_path(Path(repo_root) / ".ea" / "state.json")
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    return notices_path(Path(ctx.state_path))


@register("budget_notice.list")
async def list_budget_notices(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the principal's active, acknowledged and history notices."""
    args = _PrincipalParams.model_validate(params)
    ledger = load_notice_ledger(_ledger_path(ctx, args.repo_root))
    inbox = inbox_for(ledger, principal=args.principal, now=datetime.now(UTC))
    return {
        bucket: [notice.model_dump(mode="json") for notice in getattr(inbox, bucket)]
        for bucket in ("active", "acknowledged", "history")
    }


@register("budget_notice.deliver")
async def deliver_budget_notices(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Deliver the principal's undelivered revisions, each at most once."""
    args = _PrincipalParams.model_validate(params)
    delivered = await asyncio.to_thread(
        deliver_pending,
        _ledger_path(ctx, args.repo_root),
        principal=args.principal,
        now=datetime.now(UTC),
    )
    return {"delivered": [notice.model_dump(mode="json") for notice in delivered]}


@register("budget_notice.dispose")
async def dispose_budget_notice(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Record the principal's disposition of one notice at the revision they saw."""
    args = _DisposeParams.model_validate(params)
    try:
        notice = await asyncio.to_thread(
            dispose_notice,
            _ledger_path(ctx, args.repo_root),
            notice_key=args.notice_key,
            principal=args.principal,
            disposition=args.disposition,
            expected_revision=args.expected_revision,
            at=datetime.now(UTC),
            snooze_until=args.snooze_until,
        )
    except NoticeDispositionError as refusal:
        raise DaemonValidationError(f"validation_failed: {refusal}") from refusal
    return {"notice": notice.model_dump(mode="json")}


__all__ = [
    "deliver_budget_notices",
    "dispose_budget_notice",
    "list_budget_notices",
]
