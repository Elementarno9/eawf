"""Doctor check warning about hand-made entries at the top of ``.ea/local``.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

Agents write under the folders ``eawf paths local`` names, and eawf's own
writers use a fixed set of names beside them. Any other entry was made by
hand, and nothing reads it back, so the check names each one and points at
the verb that archives it.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.layout import EA_DIRNAME, LOCAL_DIRNAME
from eawf.observability.doctor.models import CheckResult
from eawf.platform.local_tidy import (
    LEGACY_REFLECT_NAME,
    legacy_reflect_notice,
    unknown_local_entries,
)

CHECK_NAME = "local-layout"


def check_local_layout(*, workspace: Path | None) -> list[CheckResult]:
    """Return one finding per unknown top-level entry of ``<workspace>/.ea/local``.

    Args:
        workspace: The workspace anchor, already resolved by the caller;
            ``None`` when no anchor exists.

    Returns:
        One ``warn`` result per unknown entry, in name order, reading
        ``local-layout: .ea/local/<name> is not a known folder; run eawf local
        tidy --dry-run``, or, for an earlier version's ``reflect`` folder, the
        line saying ``eawf local tidy`` moves it under ``reports``; a single
        ``ok`` result when there is none or no anchor exists. Never ``fail``:
        an unknown entry harms nothing.
    """
    if workspace is None:
        return [CheckResult(name=CHECK_NAME, status="ok", detail="no workspace anchor")]
    local_dir = workspace / EA_DIRNAME / LOCAL_DIRNAME
    unknown = unknown_local_entries(local_dir)
    if not unknown:
        return [CheckResult(name=CHECK_NAME, status="ok", detail=".ea/local holds known folders")]
    legacy = legacy_reflect_notice(local_dir)
    return [
        CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=(
                f"{CHECK_NAME}: {legacy}"
                if legacy is not None and entry.name == LEGACY_REFLECT_NAME
                else f"{CHECK_NAME}: {EA_DIRNAME}/{LOCAL_DIRNAME}/{entry.name} is not a known"
                " folder; run eawf local tidy --dry-run"
            ),
        )
        for entry in unknown
    ]


__all__ = ["CHECK_NAME", "check_local_layout"]
