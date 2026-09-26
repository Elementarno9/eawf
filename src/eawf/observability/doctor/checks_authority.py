"""Doctor check reporting which authority epoch the workspace tree is in.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

The verdict per gap follows what the gap means for the next native write:

- epoch 2 and a plain undeclared tree are both healthy (``ok``): the first
  is a cut-over tree, the second is every ordinary epoch-1 repository.
- An undeclared tree that nonetheless carries the epoch marker, and a
  declared tree with no marker, are half-done cutovers (``warn``): the
  tree still runs on epoch 1 although one of the two files says otherwise.
- A marker that does not parse is broken (``fail``): nobody can say which
  generation the tree would read from.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.epoch2.authority import AuthorityGap, resolve_authority
from eawf.observability.doctor.models import CheckResult

CHECK_NAME = "authority_epoch"


def check_authority_epoch(*, workspace: Path | None) -> CheckResult:
    """Report the authority epoch of ``<workspace>/.ea`` and why epoch 2 is withheld.

    Args:
        workspace: The workspace anchor (the ``.ea/`` parent), already
            resolved by the caller; ``None`` when no anchor exists.

    Returns:
        The ``authority_epoch`` check result.
    """
    if workspace is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no workspace anchor")
    root = Path(workspace) / ".ea"
    authority = resolve_authority(root)
    if authority.gap is None:
        return CheckResult(
            name=CHECK_NAME,
            status="ok",
            detail=f"epoch 2 (generation {authority.generation_id})",
        )
    marker = f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}"
    if authority.gap is AuthorityGap.UNDECLARED:
        if (root / GENERATIONS_DIRNAME / MARKER_FILENAME).exists():
            return CheckResult(
                name=CHECK_NAME,
                status="warn",
                detail=f"epoch 1 (undeclared): {marker} exists but the tree is not declared",
            )
        return CheckResult(name=CHECK_NAME, status="ok", detail="epoch 1 (undeclared)")
    if authority.gap is AuthorityGap.MARKER_ABSENT:
        return CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=f"epoch 1 (marker_absent): the tree is declared but {marker} is missing",
        )
    return CheckResult(
        name=CHECK_NAME,
        status="fail",
        detail=f"epoch 1 (marker_unreadable): {marker} does not parse",
    )


__all__ = ["CHECK_NAME", "check_authority_epoch"]
