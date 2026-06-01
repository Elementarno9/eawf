"""Min-N-gated self-eval surface (P29-I01-W05).

The verdict store starts empty, so the FIRST honest deliverable is a
dashboard that *refuses to score* until a real cohort accrues. The point
of this module is the opposite of a vanity metric: below a hard minimum
cohort size it emits an explicit "insufficient data" surface rather than
a Goodhartable ``0%`` / ``100%`` / ``NaN`` pass rate.

The cohort is the set of typed agent-report verdicts already persisted to
the eight role-report stores (read via
:func:`eawf.workflow.agent_report.rollup.iter_agent_reports`). Each report
carries an :class:`~eawf.kernel.state.enums.AgentReportVerdict`; this module
counts those verdicts and gates a pass-rate on
:data:`MIN_SELF_EVAL_N`.

Two layers, kept separate so the gate is testable without I/O:

- :func:`summarize_self_eval` — the **pure** reducer over a verdict
  tuple. It is the refuse-to-score gate: below :data:`MIN_SELF_EVAL_N`
  it returns :attr:`SelfEvalStatus.INSUFFICIENT_DATA` with
  ``pass_rate=None``; at or above it returns
  :attr:`SelfEvalStatus.SCORED` with a real pass rate.
- :func:`compute_self_eval` — the thin store-reading entry that pulls the
  cohort off disk and defers to the reducer. No scoring lives here.

Light per-dimension scoring is deferred to a later wave once the cohort is
large enough to be meaningful; this wave ships only the honest-negative
gate so no fake number can ever be emitted from an empty store.
"""

from __future__ import annotations

import logging
from collections import Counter
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.workflow.agent_report.rollup import iter_agent_reports

logger = logging.getLogger(__name__)

#: Hard minimum cohort size below which self-eval REFUSES to score. A cohort
#: smaller than this yields an honest "insufficient data" surface instead of
#: a pass rate, so a near-empty verdict store can never produce a
#: Goodhartable ``0%`` / ``100%`` / ``NaN`` number. Sized so a handful of
#: reports is not mistaken for a calibrated signal.
MIN_SELF_EVAL_N: int = 5

#: Verdicts that count as a "pass" when a scored pass rate is computed.
#: ``PASS_WITH_FOLLOWUPS`` is a pass with bookkeeping, not a failure, so it
#: is included; ``FAIL`` / ``BLOCKED`` are not.
_PASS_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS}
)


class SelfEvalStatus(StrEnum):
    """Whether self-eval produced a real number or refused.

    :attr:`INSUFFICIENT_DATA` is the honest-negative surface: the cohort is
    below :data:`MIN_SELF_EVAL_N`, so no pass rate is emitted.
    :attr:`SCORED` means the cohort cleared the gate and :attr:`pass_rate`
    on the surface is a real, defensible number.
    """

    INSUFFICIENT_DATA = "insufficient_data"
    SCORED = "scored"


class SelfEvalSurface(BaseModel):
    """Honest-negative self-eval surface over a verdict cohort.

    Frozen and ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction rather than silently
    skewing a downstream render.

    The :attr:`pass_rate` field is ``None`` exactly when :attr:`status` is
    :attr:`SelfEvalStatus.INSUFFICIENT_DATA` — the type makes the
    refuse-to-score contract unmissable: a caller cannot read a number out
    of an under-N cohort because there is no number to read.

    Attributes:
        status: :attr:`SelfEvalStatus.SCORED` when the cohort cleared
            :data:`MIN_SELF_EVAL_N`, else
            :attr:`SelfEvalStatus.INSUFFICIENT_DATA`.
        cohort_size: Number of verdicts in the cohort (``>= 0``).
        min_n: The hard minimum-N gate that was applied. Echoed onto the
            surface so a render can state the bar the cohort was held to.
        pass_rate: Fraction of the cohort that passed (``PASS`` /
            ``PASS_WITH_FOLLOWUPS``), in ``[0.0, 1.0]``; ``None`` when the
            cohort is below :attr:`min_n` (refuse-to-score).
        verdict_breakdown: Count per :class:`AgentReportVerdict` value,
            always present (even for an empty cohort, where every count is
            absent) so the surface is transparent about what it saw.
        note: One operator-facing line explaining the status — either why
            the surface refused to score or what the pass rate covers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SelfEvalStatus
    cohort_size: int = Field(ge=0)
    min_n: int = Field(ge=1)
    pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    verdict_breakdown: dict[str, int] = Field(default_factory=dict)
    note: str


def _verdict_breakdown(verdicts: tuple[AgentReportVerdict, ...]) -> dict[str, int]:
    """Count verdicts by value, sorted by verdict name for stable output."""
    counts = Counter(verdict.value for verdict in verdicts)
    return {
        verdict.value: counts[verdict.value]
        for verdict in AgentReportVerdict
        if counts[verdict.value]
    }


def summarize_self_eval(
    verdicts: tuple[AgentReportVerdict, ...],
    *,
    min_n: int = MIN_SELF_EVAL_N,
) -> SelfEvalSurface:
    """Reduce a verdict cohort into a min-N-gated self-eval surface.

    This is the refuse-to-score gate. Below *min_n* the surface reports
    :attr:`SelfEvalStatus.INSUFFICIENT_DATA` with ``pass_rate=None`` — an
    empty or under-N cohort never yields a ``0%`` / ``100%`` / ``NaN``
    number. At or above *min_n* it reports
    :attr:`SelfEvalStatus.SCORED` with a real pass rate.

    Pure: no I/O, no store access. :func:`compute_self_eval` reads the
    cohort off disk and defers here.

    Args:
        verdicts: The cohort of agent-report verdicts to summarise. May be
            empty.
        min_n: Hard minimum cohort size to clear before a pass rate is
            emitted. Defaults to :data:`MIN_SELF_EVAL_N`.

    Returns:
        A :class:`SelfEvalSurface` whose :attr:`pass_rate` is ``None`` iff
        the cohort is below *min_n*.

    Raises:
        ValueError: When *min_n* is less than one — a zero or negative gate
            would defeat the refuse-to-score guarantee.
    """
    if min_n < 1:
        raise ValueError(f"min_n must be >= 1 to gate scoring: {min_n!r}")

    cohort_size = len(verdicts)
    breakdown = _verdict_breakdown(verdicts)

    if cohort_size < min_n:
        logger.debug(f"summarize_self_eval refuse cohort_size={cohort_size} min_n={min_n}")
        return SelfEvalSurface(
            status=SelfEvalStatus.INSUFFICIENT_DATA,
            cohort_size=cohort_size,
            min_n=min_n,
            pass_rate=None,
            verdict_breakdown=breakdown,
            note=(
                f"insufficient data: {cohort_size} verdicts below minimum N={min_n}; "
                "refusing to score"
            ),
        )

    passed = sum(1 for verdict in verdicts if verdict in _PASS_VERDICTS)
    pass_rate = passed / cohort_size
    logger.debug(f"summarize_self_eval scored cohort_size={cohort_size} pass_rate={pass_rate:.4f}")
    return SelfEvalSurface(
        status=SelfEvalStatus.SCORED,
        cohort_size=cohort_size,
        min_n=min_n,
        pass_rate=pass_rate,
        verdict_breakdown=breakdown,
        note=(
            f"scored over {cohort_size} verdicts (>= minimum N={min_n}); "
            f"pass rate counts pass + pass-with-followups"
        ),
    )


def compute_self_eval(
    state_path: Path,
    *,
    role: AgentSessionRole | None = None,
    scope_id: str | None = None,
    min_n: int = MIN_SELF_EVAL_N,
) -> SelfEvalSurface:
    """Read the agent-report verdict cohort off disk and summarise it.

    Thin store-reading entry: the cohort is pulled via
    :func:`eawf.workflow.agent_report.rollup.iter_agent_reports` and handed
    to the pure :func:`summarize_self_eval` reducer. No scoring logic lives
    here — the gate and the pass-rate math stay in the reducer so they are
    testable without touching disk.

    Args:
        state_path: Path to ``state.json``; report stores resolve relative
            to its parent ``store/`` directory.
        role: Optional role filter — restrict the cohort to one
            :class:`~eawf.kernel.state.enums.AgentSessionRole`.
        scope_id: Optional scope filter — restrict the cohort to reports
            scoped to one entity.
        min_n: Hard minimum cohort size. Defaults to :data:`MIN_SELF_EVAL_N`.

    Returns:
        A :class:`SelfEvalSurface`; honest-negative when the on-disk cohort
        is below *min_n*.
    """
    rows = iter_agent_reports(state_path, role=role, scope_id=scope_id)
    verdicts = tuple(row.payload.body.verdict for row in rows)
    return summarize_self_eval(verdicts, min_n=min_n)


__all__ = [
    "MIN_SELF_EVAL_N",
    "SelfEvalStatus",
    "SelfEvalSurface",
    "compute_self_eval",
    "summarize_self_eval",
]
