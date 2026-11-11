"""Doctor check ``claude.statusline``: which Claude Code status line wins, and does it work.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.
"""

from __future__ import annotations

from pathlib import Path

from eawf.observability.doctor.models import CheckResult, CheckStatus

CHECK_NAME = "claude.statusline"


def check_claude_statusline(*, workspace: Path | None) -> CheckResult:
    """Report the Claude Code status line entry that wins in *workspace*.

    The full doctor reads the settings files and ``PATH`` only; ``eawf
    statusline doctor`` also times one render. A missing or shadowed line
    degrades nothing eawf records, so it warns; a broken one fails.

    Args:
        workspace: The workspace anchor; ``None`` reads from the cwd.

    Returns:
        The ``claude.statusline`` result, its detail naming the verdict,
        the layer holding eawf's entry and the fix.
    """
    from eawf.runtime.runtimes.claude.statusline_install import diagnose_statusline

    report = diagnose_statusline(cwd=workspace or Path.cwd(), render=False)
    status: CheckStatus = (
        "ok" if report.verdict == "ok" else "fail" if report.verdict == "broken" else "warn"
    )
    detail = f"verdict {report.verdict}, layer {report.layer}"
    if report.fix is not None:
        detail = f"{detail}; {report.fix}"
    return CheckResult(name=CHECK_NAME, status=status, detail=detail)


__all__ = ["CHECK_NAME", "check_claude_statusline"]
