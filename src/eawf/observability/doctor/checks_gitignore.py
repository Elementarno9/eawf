"""Doctor check flagging a managed ``.gitignore`` block an older release wrote.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

A block is written once by ``eawf init`` and then only rewritten by a
render, so a tree initialised by an earlier release keeps that release's
block, and machine-local paths added since show up as committable. A
repository without a block opted out of it and is not judged.
"""

from __future__ import annotations

from pathlib import Path

from eawf.observability.doctor.models import CheckResult
from eawf.platform.install.gitignore_writer import plan_gitignore_block
from eawf.platform.install.managed_block import ManagedBlockError

CHECK_NAME = "gitignore_block"


def check_gitignore_block(*, workspace: Path | None) -> CheckResult:
    """Report whether ``<workspace>/.gitignore``'s managed block is current.

    Args:
        workspace: The workspace anchor, already resolved by the caller;
            ``None`` when no anchor exists.

    Returns:
        ``ok`` when the block is current or absent, ``warn`` naming the
        missing patterns and ``eawf sync`` when it is outdated, and ``warn``
        when its markers cannot be spliced, which only a hand repair fixes.
    """
    if workspace is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no workspace anchor")
    try:
        plan = plan_gitignore_block(Path(workspace))
    except ManagedBlockError as exc:
        return CheckResult(name=CHECK_NAME, status="warn", detail=str(exc))
    if not plan.stale:
        return CheckResult(name=CHECK_NAME, status="ok", detail="managed block current or absent")
    missing = ", ".join(plan.added) if plan.added else "none; its lines differ"
    return CheckResult(
        name=CHECK_NAME,
        status="warn",
        detail=f"the managed .gitignore block is outdated (missing: {missing}); run `eawf sync`",
    )


__all__ = ["CHECK_NAME", "check_gitignore_block"]
