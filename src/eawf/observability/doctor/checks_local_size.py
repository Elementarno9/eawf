"""Doctor check warning when a repository's ``.ea/local`` store grows large.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

Nothing reads ``.ea/local`` back once a spike ends, so virtual environments,
models and installs left there grow the store without bound. The warning
names the scratch root such artefacts belong under and the verb that
reclaims entries whose retention has passed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictInt, StrictStr, TypeAdapter, ValidationError

from eawf.kernel.config.layered import get_dotted, merge_config
from eawf.observability.doctor.models import CheckResult

CHECK_NAME = "local_size"
#: Bytes in one MB as ``local.size_warn_mb`` counts it.
_MB = 1024 * 1024
_THRESHOLD: TypeAdapter[int] = TypeAdapter(Annotated[StrictInt, Field(ge=1)])
_SCRATCH_ROOT: TypeAdapter[str] = TypeAdapter(Annotated[StrictStr, Field(min_length=1)])


def _local_size_bytes(local_root: Path) -> int:
    """Return the apparent size of the files under *local_root*, not following links."""
    total = 0
    for entry in local_root.rglob("*"):
        try:
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def check_local_size(*, workspace: Path | None) -> CheckResult:
    """Report whether ``<workspace>/.ea/local`` has grown past ``local.size_warn_mb``.

    Args:
        workspace: The workspace anchor, already resolved by the caller;
            ``None`` when no anchor exists.

    Returns:
        ``ok`` with the measured size when it is under the threshold or no
        anchor exists; ``warn`` with the measured size, the configured
        ``local.scratch_root`` and ``eawf local gc`` once it reaches the
        threshold; ``warn`` naming the leaf when a layer states an unusable
        ``local.size_warn_mb`` or ``local.scratch_root``.
    """
    if workspace is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no workspace anchor")
    merged, _sources = merge_config(workspace=workspace, repo=workspace)
    leaf = "local.size_warn_mb"
    try:
        threshold_mb = _THRESHOLD.validate_python(get_dotted(merged, leaf))
        leaf = "local.scratch_root"
        scratch_root = _SCRATCH_ROOT.validate_python(get_dotted(merged, leaf))
    except ValidationError as exc:
        return CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=f"{leaf} is unusable: {exc.errors()[0]['msg']}",
        )
    size_mb = _local_size_bytes(workspace / ".ea" / "local") / _MB
    if size_mb < threshold_mb:
        return CheckResult(
            name=CHECK_NAME,
            status="ok",
            detail=f".ea/local is {size_mb:.1f} MB (warns at {threshold_mb} MB)",
        )
    return CheckResult(
        name=CHECK_NAME,
        status="warn",
        detail=(
            f".ea/local is {size_mb:.1f} MB (>= {threshold_mb} MB); move virtual environments, "
            f"models, installs and archives under local.scratch_root ({scratch_root}) and run "
            "`eawf local gc`"
        ),
    )


__all__ = ["CHECK_NAME", "check_local_size"]
