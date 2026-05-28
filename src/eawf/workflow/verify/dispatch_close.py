"""Post-execution verify gate for the daemon dispatch runner (P28-I03-W57).

The dispatch runner's pre-W57 close path accepts an executor report
unconditionally — the runner emits the typed ``agent_end`` envelope and
returns. That left an open seam: a report whose verdict is
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.FAIL` or
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.BLOCKED` could still
drive a wave-close path forward, defeating the v0.4 verify spine.

This module owns the **deterministic** close-readiness check the
runner consults after the subagent returns and before the wave can
move toward CLOSED. The check is pure: it inspects only the typed
:class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody` and
returns a :class:`VerifyResult`. The runner converts a
``VerifyResult(passed=False, ...)`` into a fail-fast
:class:`DispatchCloseBlockedError` so the close path stops at the
first verifiable failure rather than silently accepting an unverified
attempt.

The gate is intentionally narrow: it does **not** re-run the readiness
projection (:func:`eawf.workflow.verify.readiness.compute`); that is the
operator-facing wave-close advisory. This gate is the daemon-side
runtime invariant that an executor report MUST claim success before the
runner reports the dispatch as close-ready.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    ExecutorReportBody,
)

logger = logging.getLogger(__name__)

#: Verdicts the runner treats as close-ready. ``PASS`` is a clean
#: dispatch; ``PASS_WITH_FOLLOWUPS`` carries follow-ups the operator
#: tracks but does not block on (e.g. a V5 runtime fallback).
_CLOSE_READY_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS_WITH_FOLLOWUPS,
    }
)


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :func:`verify_close_readiness`.

    Attributes:
        passed: ``True`` iff every check fired clean. ``False`` blocks
            the close path; the runner raises
            :class:`DispatchCloseBlockedError` with the :attr:`reasons`
            so the operator sees the precise refusal.
        verdict: The report verdict the check inspected; surfaced so
            callers can log / report the precise failure mode without
            re-reading the report.
        reasons: One short string per failing check. Empty on a clean
            pass. Reasons stay terse so they fit in a single log line
            and a CLI error message.
    """

    passed: bool
    verdict: AgentReportVerdict
    reasons: tuple[str, ...] = field(default_factory=tuple)


class DispatchCloseBlockedError(RuntimeError):
    """Raised by the dispatch runner when the verify gate refuses close.

    Carries the :class:`VerifyResult` so callers (and tests) can
    inspect the structured refusal alongside the human-readable
    message. The ``wave_id`` attribute names the wave whose close was
    blocked.
    """

    def __init__(self, *, wave_id: str, result: VerifyResult) -> None:
        self.wave_id = wave_id
        self.result = result
        reasons = "; ".join(result.reasons) if result.reasons else "no reasons recorded"
        super().__init__(
            f"verify gate blocked dispatch close for wave {wave_id!r}: "
            f"verdict={result.verdict.value} reasons=[{reasons}]"
        )


def verify_close_readiness(wave_id: str, report: AgentReportBody) -> VerifyResult:
    """Return a :class:`VerifyResult` for *report* against *wave_id*.

    The deterministic check has three rungs, evaluated in order:

    1. The report verdict MUST be a member of
       :data:`_CLOSE_READY_VERDICTS` (``PASS`` or
       ``PASS_WITH_FOLLOWUPS``). A ``FAIL`` or ``BLOCKED`` verdict is
       the most common refusal and stops the gate immediately.
    2. The report body's ``summary`` MUST be non-empty after
       stripping whitespace — every typed report body inherits the
       1..4000-char summary contract from
       :class:`~eawf.kernel.store.kinds.agent_report.AgentReportCommonBody`,
       but a whitespace-only summary slips past the min-length check
       and renders as a blank executor report. The gate refuses it.
    3. For an :class:`ExecutorReportBody`, the body's ``wave_id``
       MUST equal the dispatched *wave_id*. A mismatch means the
       executor reported on a different wave than the runner served,
       which is a fail-fast inconsistency.

    The gate intentionally does **not** require a ``commit_sha``: the
    runner falls back to the serving attempt id when the executor
    omits one (see
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`).

    Args:
        wave_id: The wave the runner served. Compared against
            :attr:`ExecutorReportBody.wave_id` for the executor path.
        report: The typed ``AgentReportBody`` the runner persisted.

    Returns:
        :class:`VerifyResult` with ``passed`` reflecting every check.
        A failure surfaces every reason that fired (not just the
        first), so the operator sees the full picture in one log line.
    """
    reasons: list[str] = []
    verdict = report.verdict

    if verdict not in _CLOSE_READY_VERDICTS:
        reasons.append(f"verdict={verdict.value} not in close-ready set")

    if not report.summary.strip():
        reasons.append("report summary is blank")

    if isinstance(report, ExecutorReportBody) and report.wave_id != wave_id:
        reasons.append(
            f"executor body wave_id={report.wave_id!r} disagrees with dispatched wave={wave_id!r}"
        )

    passed = not reasons
    result = VerifyResult(
        passed=passed,
        verdict=verdict,
        reasons=tuple(reasons),
    )
    logger.info(
        f"verify_close_readiness wave={wave_id} passed={passed} "
        f"verdict={verdict.value} reasons={len(reasons)}"
    )
    return result


__all__ = [
    "DispatchCloseBlockedError",
    "VerifyResult",
    "verify_close_readiness",
]
