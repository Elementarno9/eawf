"""Cross-vendor disjoint-family jury convener (P29-I04-W08).

The single-auditor close gate
(:func:`eawf.workflow.dispatch.verdict.verify_wave_verdict_gate`) blocks a
wave's close on ONE fresh-context auditor verdict. For a high-risk wave that
is not enough independence: a single auditor shares the executor's runtime
family, so a vendor-correlated blind spot (a model that systematically
mis-reads the same class of diff) sails past unchallenged. This module is the
multi-juror upgrade -- it convenes **three disjoint-family jurors**
(``claude-code`` + ``codex`` + ``opencode``), each an independent
fresh-context auditor whose spawn is bound to a *different* vendor's adapter,
collects each juror's :class:`~eawf.kernel.state.enums.AgentReportVerdict`,
and reduces the ballots through the pure jury reducer
(:func:`eawf.observability.eval.jury.aggregate_jury`).

Independence by construction. Each juror runs through
:func:`eawf.workflow.dispatch.verdict.produce_wave_verdict`, which renders an
auditor-only prompt carrying solely the diff base + the wave's
``success_criteria`` -- never the executor's narrative and never another
juror's ballot. The jurors are convened one at a time; their ballots are
reduced ONLY after all of them have returned. There is no peer channel: a
juror cannot read, influence, or even observe that another juror exists. The
correlation the jury defends against is exactly the shared-vendor blind spot,
so the three runtimes are deliberately disjoint families.

Abstention + quorum. A juror whose runtime is unavailable (its bound spawn
raises, or the bounded re-ask loop exhausts without a schema-valid body) does
not crash the convener -- it is recorded as a non-vote (an abstention) and the
reduction runs over the jurors that *did* vote. Independence makes one juror's
failure orthogonal to the others, so a transient outage on one vendor must not
sink (or silently pass) the whole vote. A reduction is only trustworthy with
enough independent ballots, so when fewer than :data:`JURY_QUORUM` jurors voted
the convener surfaces :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
rather than reducing over a lone ballot -- a sub-quorum jury is an operator
decision, not a machine pass.

The convener is **injected + testable**: it takes a per-runtime spawn factory
(:data:`SpawnFactory`) rather than importing an adapter, so production binds
each runtime's :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
(via :func:`eawf.runtime.runtimes.selector.select_adapter`) while a test binds
recording stubs -- no real subprocess, no network, no cost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.models import State, Wave
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.workflow.dispatch.llm_assist import DEFAULT_MAX_ATTEMPTS, SpawnFn
from eawf.workflow.dispatch.verdict import produce_wave_verdict

logger = logging.getLogger(__name__)

#: The three disjoint vendor families the jury convenes one juror from each of.
#: They are deliberately drawn from distinct vendors so a shared-model blind
#: spot cannot correlate the ballots -- the independence the jury exists to
#: provide is vendor-disjointness, not merely three sessions of one runtime.
JURY_RUNTIME_FAMILIES: tuple[str, ...] = ("claude-code", "codex", "opencode")

#: Minimum number of jurors that must cast a ballot for the reduction to be
#: trusted. With fewer than this many votes the convener surfaces
#: :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER` instead
#: of reducing over a lone (or zero) ballot -- a sub-quorum jury is an operator
#: decision. Two of three keeps the jury live when one vendor is down while
#: still requiring genuine cross-vendor agreement.
JURY_QUORUM: int = 2

#: A factory that returns the :data:`~eawf.workflow.dispatch.llm_assist.SpawnFn`
#: bound to one runtime family. Production binds the resolved adapter's
#: :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session` (with the
#: model + cwd captured in the closure); a test binds a recording stub. The
#: convener calls it once per juror so each juror spawns on its OWN vendor.
type SpawnFactory = Callable[[str], SpawnFn]


class JurorOutcome(BaseModel):
    """One juror's contribution to the cross-vendor jury.

    A juror either voted (carries :attr:`verdict`, leaves :attr:`error`
    ``None``) or abstained (carries :attr:`error`, leaves :attr:`verdict`
    ``None``). An abstention is a non-vote, never a silent pass: it is recorded
    so the operator can see which vendor was unavailable and the reduction runs
    over the jurors that did vote.

    Attributes:
        runtime: The runtime family this juror was bound to (one of
            :data:`JURY_RUNTIME_FAMILIES`).
        verdict: The juror's fresh-context auditor verdict, or ``None`` when the
            juror abstained.
        session_id: The fresh AUDITOR session id that authored the verdict, or
            ``None`` for an abstention.
        attempts_used: Number of bounded re-ask spawns the juror burned to
            obtain its body, or ``None`` for an abstention.
        error: Short reason the juror abstained (the exception class + message),
            or ``None`` when the juror voted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str = Field(min_length=1)
    verdict: AgentReportVerdict | None = None
    session_id: str | None = None
    attempts_used: int | None = Field(default=None, ge=1)
    error: str | None = Field(default=None, max_length=500)

    @property
    def voted(self) -> bool:
        """Return whether this juror cast a ballot (did not abstain)."""
        return self.verdict is not None


class CrossVendorJuryResult(BaseModel):
    """Reduced outcome of a convened cross-vendor jury.

    Attributes:
        wave_id: The wave whose close gate convened the jury.
        outcome: The resolved :class:`~eawf.observability.eval.jury.JuryAggregateOutcome`.
            :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
            covers both a genuine split with no veto AND a sub-quorum jury (too
            few jurors voted to trust a reduction).
        aggregate: The :class:`~eawf.observability.eval.jury.JuryAggregate` the
            reducer produced over the cast ballots, or ``None`` when the jury
            fell below :data:`JURY_QUORUM` (no reduction was run).
        jurors: One :class:`JurorOutcome` per convened runtime family, in the
            order they were convened. Carries both votes and abstentions.
        voted_count: Number of jurors that cast a ballot.
        abstained_count: Number of jurors that abstained (runtime unavailable).
        reasons: One short string per signal that drove the outcome (sub-quorum
            note, plus the reducer's own reasons when a reduction ran). Empty
            only for a clean unanimous pass at full quorum.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    wave_id: str = Field(min_length=1)
    outcome: JuryAggregateOutcome
    aggregate: JuryAggregate | None = None
    jurors: tuple[JurorOutcome, ...]
    voted_count: int = Field(ge=0)
    abstained_count: int = Field(ge=0)
    reasons: tuple[str, ...] = ()

    @property
    def needs_user(self) -> bool:
        """Return whether the outcome routes to the operator-pause surface.

        ``True`` for a split-with-no-veto or a sub-quorum jury -- the caller
        surfaces an unresolvable vote to the operator rather than silently
        closing the wave.
        """
        return self.outcome is JuryAggregateOutcome.NEEDS_USER


async def _convene_one_juror(
    *,
    state: State,
    state_path: Path,
    events_path: Path,
    wave: Wave,
    runtime: str,
    spawn: SpawnFn,
    repo_root: Path | None,
    max_attempts: int,
    now: datetime | None,
) -> JurorOutcome:
    """Convene a single disjoint-family juror and return its outcome.

    Runs one fresh-context auditor through
    :func:`eawf.workflow.dispatch.verdict.produce_wave_verdict` bound to
    *runtime*'s spawn. The juror reads only the diff base + the wave's success
    criteria (the producer renders a fresh-context auditor prompt); it never
    sees another juror's ballot. Any failure -- the bound spawn raising, the
    bounded re-ask loop exhausting, or a self-report rejection -- is caught and
    converted into an abstention (a :class:`JurorOutcome` with ``verdict=None``
    and a populated ``error``) so one vendor's outage does not crash the
    convener.

    Args:
        state: Validated state, mutated in place by the juror's auditor-session
            registration.
        state_path: Path to ``state.json`` for the auditor report store.
        events_path: Path to ``event.jsonl`` for the session-start event.
        wave: The wave under audit.
        runtime: The runtime family this juror spawns on.
        spawn: The :data:`~eawf.workflow.dispatch.llm_assist.SpawnFn` bound to
            *runtime*.
        repo_root: Repository root forwarded to the diff-base derivation.
        max_attempts: Bounded re-ask ceiling forwarded to the producer.
        now: Optional fixed timestamp for the session start.

    Returns:
        The juror's :class:`JurorOutcome` -- a vote on success, an abstention on
        any failure.
    """
    try:
        result = await produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=spawn,
            runtime=runtime,
            repo_root=repo_root,
            max_attempts=max_attempts,
            now=now,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        logger.warning(f"_convene_one_juror wave={wave.id} runtime={runtime!r} status=abstained")
        return JurorOutcome(runtime=runtime, error=reason)
    logger.info(
        f"_convene_one_juror wave={wave.id} runtime={runtime!r} "
        f"verdict={result.verdict.value!r} status=voted"
    )
    return JurorOutcome(
        runtime=runtime,
        verdict=result.verdict,
        session_id=result.auditor_session_id,
        attempts_used=result.assist_result.attempts_used,
    )


def _reduce_jury(
    *,
    wave_id: str,
    jurors: tuple[JurorOutcome, ...],
    quorum: int,
) -> CrossVendorJuryResult:
    """Reduce the convened jurors' ballots into a typed result.

    Pure function -- no spawn, no I/O. Filters the cast ballots out of *jurors*,
    enforces the *quorum* (too few votes routes to ``NEEDS_USER`` with no
    reduction), and otherwise reduces the binary ballots through
    :func:`eawf.observability.eval.jury.aggregate_jury`, mapping the aggregate's
    outcome + reasons onto the cross-vendor result.

    Args:
        wave_id: The wave the jury was convened for.
        jurors: The convened jurors (votes + abstentions).
        quorum: Minimum number of cast ballots required to trust a reduction.

    Returns:
        The :class:`CrossVendorJuryResult`.
    """
    votes = tuple(j for j in jurors if j.voted)
    voted_count = len(votes)
    abstained_count = len(jurors) - voted_count

    if voted_count < quorum:
        reason = (
            f"sub-quorum jury: {voted_count} of {len(jurors)} jurors voted "
            f"(quorum {quorum}); routing to operator"
        )
        logger.info(f"_reduce_jury wave={wave_id} outcome=needs_user reason=sub-quorum")
        return CrossVendorJuryResult(
            wave_id=wave_id,
            outcome=JuryAggregateOutcome.NEEDS_USER,
            aggregate=None,
            jurors=jurors,
            voted_count=voted_count,
            abstained_count=abstained_count,
            reasons=(reason,),
        )

    ballots = tuple(
        JurorBallot(
            juror_id=juror.runtime,
            acceptance_style="binary",
            # ``voted`` guarantees verdict is not None for every vote.
            verdict=juror.verdict,
        )
        for juror in votes
    )
    aggregate = aggregate_jury(ballots)
    reasons = aggregate.reasons
    if abstained_count:
        reasons = (
            f"{abstained_count} of {len(jurors)} jurors abstained (runtime unavailable)",
            *reasons,
        )
    logger.info(
        f"_reduce_jury wave={wave_id} outcome={aggregate.outcome.value} "
        f"voted={voted_count} abstained={abstained_count} veto_count={aggregate.veto_count}"
    )
    return CrossVendorJuryResult(
        wave_id=wave_id,
        outcome=aggregate.outcome,
        aggregate=aggregate,
        jurors=jurors,
        voted_count=voted_count,
        abstained_count=abstained_count,
        reasons=reasons,
    )


async def convene_cross_vendor_jury(
    *,
    state: State,
    state_path: Path,
    events_path: Path,
    wave: Wave,
    spawn_factory: SpawnFactory,
    runtimes: tuple[str, ...] = JURY_RUNTIME_FAMILIES,
    quorum: int = JURY_QUORUM,
    repo_root: Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> CrossVendorJuryResult:
    """Convene a disjoint-family jury at the wave close gate and reduce it.

    The ordered steps:

    1. For each runtime in *runtimes* (default the three disjoint families
       :data:`JURY_RUNTIME_FAMILIES`), bind its spawn via *spawn_factory* and
       convene one fresh-context auditor juror through
       :func:`eawf.workflow.dispatch.verdict.produce_wave_verdict`. The jurors
       are convened one at a time and each reads ONLY the diff base + the wave's
       success criteria -- never another juror's ballot (independence by
       construction; no peer channel). A juror whose runtime is unavailable
       abstains (recorded, not crashed).
    2. Reduce the cast ballots through
       :func:`eawf.observability.eval.jury.aggregate_jury` (binary minority-veto)
       -- but only after EVERY juror has returned. A jury below *quorum* routes
       to :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
       with no reduction.

    A :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
    outcome (a genuine split with no veto, or a sub-quorum jury) is the
    operator-pause signal: the caller surfaces it rather than silently closing
    the wave. A single ``FAIL`` / ``BLOCKED`` ballot vetoes the vote to
    ``FAIL``; a unanimous ``PASS`` clears to ``PASS``.

    The injected *spawn_factory* is the testability seam: production binds each
    runtime's :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
    (resolved via :func:`eawf.runtime.runtimes.selector.select_adapter`); a test
    binds recording stubs returning canned auditor bodies, so no real subprocess
    runs.

    Args:
        state: Loaded, validated state -- mutated in place as each juror
            registers its fresh AUDITOR session. The caller persists it through
            the canonical writer.
        state_path: Path to ``state.json``; each juror's auditor report store
            resolves under its sibling ``store/`` directory.
        events_path: Path to ``event.jsonl`` for the per-juror session-start
            event.
        wave: The wave under audit.
        spawn_factory: Per-runtime spawn factory. Called once per juror to bind
            that juror's spawn to its own vendor's adapter.
        runtimes: The runtime families to convene one juror from each of.
            Defaults to the three disjoint families.
        quorum: Minimum number of cast ballots to trust a reduction. Below this
            the outcome is ``NEEDS_USER``. Defaults to :data:`JURY_QUORUM`.
        repo_root: Repository root forwarded to each juror's diff-base
            derivation. ``None`` falls back to the process cwd.
        max_attempts: Bounded re-ask ceiling forwarded to each juror.
        now: Optional fixed timestamp for each juror's session start.

    Returns:
        The reduced :class:`CrossVendorJuryResult` carrying the outcome, the
        underlying aggregate (or ``None`` below quorum), and one
        :class:`JurorOutcome` per convened runtime family.

    Raises:
        ValueError: When *runtimes* is empty -- a jury needs at least one
            runtime family to convene.
    """
    if not runtimes:
        raise ValueError("cannot convene a jury over no runtime families")

    logger.info(
        f"convene_cross_vendor_jury wave={wave.id} runtimes={len(runtimes)} quorum={quorum}"
    )
    jurors: list[JurorOutcome] = []
    for runtime in runtimes:
        spawn = spawn_factory(runtime)
        juror = await _convene_one_juror(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            runtime=runtime,
            spawn=spawn,
            repo_root=repo_root,
            max_attempts=max_attempts,
            now=now,
        )
        jurors.append(juror)

    return _reduce_jury(wave_id=wave.id, jurors=tuple(jurors), quorum=quorum)


__all__ = [
    "JURY_QUORUM",
    "JURY_RUNTIME_FAMILIES",
    "CrossVendorJuryResult",
    "JurorOutcome",
    "SpawnFactory",
    "convene_cross_vendor_jury",
]
