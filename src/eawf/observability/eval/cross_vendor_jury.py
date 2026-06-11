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

Per-rubric-item reduction (P29-I08-W04). The holistic path collects ONE
verdict per juror and reduces it once. The per-item path
(:func:`reduce_per_item_ballots`) lifts that to ONE vote per rubric item per
juror: each juror returns a :class:`PerItemJurorBallot` of
:class:`RubricItemVote` rows (one per jury-scorable behaviour id), and the
reducer resolves each item independently before folding the item verdicts into
a wave-level outcome. The per-item reducer reuses the holistic
:func:`~eawf.observability.eval.jury.aggregate_jury` per item, so the
minority-veto (one credible refutation kills the item) and split (-> NEEDS_USER)
semantics are byte-identical to the holistic path -- only the granularity
changes. The wave-level fold mirrors the holistic escalation order: any item
FAIL -> wave FAIL; else any item NEEDS_USER -> wave NEEDS_USER; else PASS. The
reducer is **pure** (no spawn, no I/O), so it is deterministically testable over
canned ballots; the holistic :func:`convene_cross_vendor_jury` API is unchanged
for existing callers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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


class RubricItemVote(BaseModel):
    """One juror's vote on a single rubric item.

    The per-item upgrade of a holistic ballot: instead of one verdict per
    juror, a juror returns one of these per rubric item id so the reduction can
    veto (and cite) at item granularity. A failing vote SHOULD carry a
    *refutation* -- the credible-refutation text that justifies the veto under
    the refute-first rubric -- so the result can name which item failed and why.

    Attributes:
        item_id: The rubric behaviour id (a ``B<n>`` label) this vote scores.
            Matched against the convened rubric's item ids by the reducer.
        passed: Whether this juror could NOT refute the item (the refute-first
            pass) -- ``True`` passes the item, ``False`` votes to fail it.
        refutation: The credible-refutation text justifying a ``passed=False``
            vote, or ``None``. A failing vote with a refutation is a veto; a
            failing vote with no refutation is a non-veto non-pass.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1)
    passed: bool
    refutation: str | None = Field(default=None, max_length=2000)


class PerItemJurorBallot(BaseModel):
    """One juror's per-item ballot: a vote for every rubric item.

    Replaces the holistic one-verdict-per-juror ballot with one
    :class:`RubricItemVote` per rubric item id, so the reduction is per item
    rather than per ballot. The reducer enforces that a ballot's votes cover the
    convened rubric's item ids exactly -- a vote on an unknown item id is
    rejected (see :func:`reduce_per_item_ballots`).

    Attributes:
        juror: The runtime family (juror id) that cast this ballot, one of
            :data:`JURY_RUNTIME_FAMILIES`. Mirrors
            :attr:`JurorOutcome.runtime`.
        votes: One :class:`RubricItemVote` per rubric item the juror scored.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    juror: str = Field(min_length=1)
    votes: tuple[RubricItemVote, ...]


#: Forced-schema adapter for a single juror's per-item ballot. The live spec-jury
#: ballot fn (:func:`eawf.workflow.dispatch.spec_jury.live_per_item_ballot_fn`)
#: hands this to the bounded re-ask loop so a spawn that returns a malformed
#: ballot (wrong keys, a non-bool vote) fails validation and the loop re-asks
#: rather than admitting an off-schema body.
_PER_ITEM_BALLOT_ADAPTER: TypeAdapter[PerItemJurorBallot] = TypeAdapter(PerItemJurorBallot)


def parse_per_item_ballot(raw: object) -> PerItemJurorBallot:
    """Validate *raw* as a :class:`PerItemJurorBallot`.

    The forced-schema validator the live spec-jury ballot fn hands its bounded
    re-ask loop. It validates *raw* directly against
    :data:`_PER_ITEM_BALLOT_ADAPTER`, so a spawn that returns a ballot shape with
    the wrong keys (or a non-bool vote) fails the schema and raises
    :class:`pydantic.ValidationError` -- which the loop catches, classifies as a
    schema mismatch, and re-asks (then exhausts typed). Raising a
    ``ValidationError`` (not a plain ``ValueError``) is load-bearing: the loop
    only catches ``json.JSONDecodeError`` + ``ValidationError``, so a plain
    ``ValueError`` would escape the bounded retry uncaught.

    Args:
        raw: The JSON-decoded spawn output.

    Returns:
        The validated :class:`PerItemJurorBallot`.

    Raises:
        pydantic.ValidationError: When *raw* is not a valid per-item ballot
            (wrong keys, a non-bool vote, or a missing field).
    """
    return _PER_ITEM_BALLOT_ADAPTER.validate_python(raw)


class PerItemVerdict(BaseModel):
    """The reduced verdict for one rubric item across all jurors.

    Attributes:
        item_id: The rubric behaviour id this verdict resolves.
        outcome: The per-item :class:`~eawf.observability.eval.jury.JuryAggregateOutcome`
            -- ``PASS`` when every juror passed the item, ``FAIL`` when any
            juror vetoed it with a refutation (minority-veto), ``NEEDS_USER``
            when the jurors split with no veto.
        veto_count: Number of jurors that vetoed this item (a ``passed=False``
            vote carrying a refutation).
        refutations: The refutation text from every juror that vetoed this item,
            in juror order, so a caller can cite which item failed and why.
        reasons: One short string per signal that drove the per-item outcome.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1)
    outcome: JuryAggregateOutcome
    veto_count: int = Field(ge=0)
    refutations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class PerItemJuryResult(BaseModel):
    """Reduced outcome of a per-rubric-item jury vote.

    Carries one :class:`PerItemVerdict` per rubric item plus the wave-level fold
    over them. The fold mirrors the holistic reducer's escalation order: any
    item ``FAIL`` sinks the wave to ``FAIL``; absent a fail, any item
    ``NEEDS_USER`` routes the wave to ``NEEDS_USER``; only when every item
    passes does the wave clear to ``PASS``.

    Attributes:
        outcome: The wave-level fold over the per-item verdicts.
        items: One :class:`PerItemVerdict` per convened rubric item, in the
            order the item ids were supplied. Empty when the rubric is empty.
        reasons: One short string per wave-level signal (the folded items'
            outcomes). Empty for a clean all-pass (including an empty rubric).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: JuryAggregateOutcome
    items: tuple[PerItemVerdict, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def needs_user(self) -> bool:
        """Return whether the wave-level fold routes to the operator-pause surface."""
        return self.outcome is JuryAggregateOutcome.NEEDS_USER

    @property
    def failed_item_ids(self) -> tuple[str, ...]:
        """Return the ids of the items whose per-item verdict is ``FAIL``."""
        return tuple(
            item.item_id for item in self.items if item.outcome is JuryAggregateOutcome.FAIL
        )


def _vote_to_ballot(juror: str, vote: RubricItemVote) -> JurorBallot:
    """Map one juror's :class:`RubricItemVote` onto a binary :class:`JurorBallot`.

    The mapping is the load-bearing seam that lets the per-item reducer reuse
    the holistic :func:`~eawf.observability.eval.jury.aggregate_jury`: a passing
    vote becomes ``PASS``; a failing vote with a refutation becomes a ``FAIL``
    veto (refute-first -- one credible refutation kills the item); a failing
    vote with no refutation becomes ``PASS_WITH_FOLLOWUPS``, a non-veto non-pass
    the holistic reducer reads as a split when mixed with a ``PASS``.

    Args:
        juror: The juror id (carried onto the ballot's ``juror_id``).
        vote: The juror's vote on one rubric item.

    Returns:
        The equivalent binary :class:`JurorBallot`.
    """
    if vote.passed:
        verdict = AgentReportVerdict.PASS
    elif vote.refutation is not None:
        verdict = AgentReportVerdict.FAIL
    else:
        verdict = AgentReportVerdict.PASS_WITH_FOLLOWUPS
    return JurorBallot(juror_id=juror, acceptance_style="binary", verdict=verdict)


def _fold_wave_outcome(
    items: tuple[PerItemVerdict, ...],
) -> tuple[JuryAggregateOutcome, tuple[str, ...]]:
    """Fold the per-item verdicts into the wave-level outcome + reasons.

    Mirrors the holistic reducer's escalation order so the per-item path and the
    holistic path agree on which signal dominates: any item ``FAIL`` sinks the
    wave to ``FAIL`` first; absent a fail, any item ``NEEDS_USER`` routes the
    wave to ``NEEDS_USER``; only an all-pass rubric (or an empty one) clears to
    ``PASS``.

    Args:
        items: The reduced per-item verdicts.

    Returns:
        The wave-level outcome and its reasons (empty for a clean all-pass).
    """
    failed = tuple(i.item_id for i in items if i.outcome is JuryAggregateOutcome.FAIL)
    if failed:
        return JuryAggregateOutcome.FAIL, (
            f"per-item veto: {len(failed)} of {len(items)} rubric items failed "
            f"({', '.join(failed)})",
        )
    unresolved = tuple(i.item_id for i in items if i.outcome is JuryAggregateOutcome.NEEDS_USER)
    if unresolved:
        return JuryAggregateOutcome.NEEDS_USER, (
            f"per-item split: {len(unresolved)} of {len(items)} rubric items "
            f"unresolved ({', '.join(unresolved)}); routing to operator",
        )
    return JuryAggregateOutcome.PASS, ()


def reduce_per_item_ballots(
    ballots: tuple[PerItemJurorBallot, ...],
    rubric_item_ids: tuple[str, ...],
) -> PerItemJuryResult:
    """Reduce per-item juror ballots into a per-item + wave-level result.

    Pure function -- no spawn, no I/O. For each id in *rubric_item_ids* it
    collects every juror's vote on that item, maps each vote onto a binary
    :class:`~eawf.observability.eval.jury.JurorBallot` (:func:`_vote_to_ballot`),
    and reduces them through the holistic
    :func:`~eawf.observability.eval.jury.aggregate_jury` -- so the per-item
    minority-veto + split semantics are byte-identical to the holistic reducer's,
    merely lifted from one ballot per juror to one per (juror, item):

    - every juror passes the item -> item ``PASS``;
    - any juror vetoes the item (a ``passed=False`` vote with a refutation) ->
      item ``FAIL`` (minority-veto: one credible refutation kills the item);
    - the jurors split with no veto -> item ``NEEDS_USER``.

    The wave-level outcome folds the per-item verdicts in the holistic
    reducer's escalation order (:func:`_fold_wave_outcome`): any item ``FAIL``
    -> wave ``FAIL``; else any item ``NEEDS_USER`` -> wave ``NEEDS_USER``; else
    ``PASS``.

    Boundary: an empty *rubric_item_ids* (no jury-scorable items) reduces to a
    clean ``PASS`` with no per-item verdicts -- a wave with nothing to score has
    nothing to veto. An item id no juror voted on reduces to ``NEEDS_USER`` for
    that item (no ballots is an unresolved item, not a silent pass).

    Args:
        ballots: One :class:`PerItemJurorBallot` per juror that voted. May be
            empty (every item then resolves to ``NEEDS_USER`` from zero votes,
            unless the rubric itself is empty).
        rubric_item_ids: The rubric behaviour ids to reduce, in render order.
            The reduction is scoped to exactly these ids.

    Returns:
        The :class:`PerItemJuryResult` carrying the per-item verdicts, the
        wave-level fold, and the cited refutations.

    Raises:
        ValueError: When any ballot carries a vote on an item id absent from
            *rubric_item_ids* -- an off-rubric vote is a malformed ballot, not a
            silently-dropped one.
    """
    known = frozenset(rubric_item_ids)
    for ballot in ballots:
        for vote in ballot.votes:
            if vote.item_id not in known:
                raise ValueError(
                    f"ballot from juror {ballot.juror!r} votes on unknown rubric "
                    f"item: {vote.item_id!r}"
                )

    items: list[PerItemVerdict] = []
    for item_id in rubric_item_ids:
        item_ballots = tuple(
            _vote_to_ballot(ballot.juror, vote)
            for ballot in ballots
            for vote in ballot.votes
            if vote.item_id == item_id
        )
        refutations = tuple(
            vote.refutation
            for ballot in ballots
            for vote in ballot.votes
            if vote.item_id == item_id and not vote.passed and vote.refutation is not None
        )
        if not item_ballots:
            items.append(
                PerItemVerdict(
                    item_id=item_id,
                    outcome=JuryAggregateOutcome.NEEDS_USER,
                    veto_count=0,
                    refutations=(),
                    reasons=("no juror voted on this rubric item; routing to operator",),
                )
            )
            continue
        aggregate = aggregate_jury(item_ballots)
        items.append(
            PerItemVerdict(
                item_id=item_id,
                outcome=aggregate.outcome,
                veto_count=aggregate.veto_count,
                refutations=refutations,
                reasons=aggregate.reasons,
            )
        )

    items_tuple = tuple(items)
    outcome, reasons = _fold_wave_outcome(items_tuple)
    logger.info(
        f"reduce_per_item_ballots items={len(items_tuple)} outcome={outcome.value} "
        f"ballots={len(ballots)}"
    )
    return PerItemJuryResult(outcome=outcome, items=items_tuple, reasons=reasons)


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
    "PerItemJurorBallot",
    "PerItemJuryResult",
    "PerItemVerdict",
    "RubricItemVote",
    "SpawnFactory",
    "convene_cross_vendor_jury",
    "parse_per_item_ballot",
    "reduce_per_item_ballots",
]
