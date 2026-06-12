"""L1-L5 context-pruning reducers over the research-campaign Claim ledger.

An orchestrator that drives a long-running research campaign cannot feed the
whole :class:`~eawf.kernel.state.models.Claim` ledger into a downstream context
window once the ledger grows — the ledger is the campaign's full memory, but a
single dispatch only needs the *relevant, live* slice of it. These reducers are
that slice: five escalating pruning levels (:class:`PruneLevel` ``L1`` .. ``L5``)
that each drop more of the ledger than the last, so the orchestrator picks the
weakest level that fits the budget and only escalates when the survivor set is
still too large.

Pure reducers
-------------
Every level is a **pure reducer** over the ledger in exactly the sense
:mod:`eawf.kernel.spec.saturation` is: given the same ledger (and the same
:class:`PruneConfig` / ``now`` / query) it always returns the same
:class:`PruningResult`, performs no I/O, mutates nothing, and never raises on
the read path. A dropped claim is recorded as data on the result (the
``dropped`` ids and the per-claim :class:`DropReason`), not signalled by an
exception. The reducer consumes the W12 state-resident ledger; it does not
redefine or mutate it.

The escalating levels
---------------------
Each level is a strict superset of the prior level's drops — ``L<n>`` keeps a
subset of what ``L<n-1>`` keeps — so escalating never *re-admits* a claim the
weaker level already dropped:

* **L1 — drop dead rows.** Drop only ``SUPERSEDED`` claims (kept in the ledger
  for traceability, never relevant to a fresh dispatch) and claims whose
  ``answers_question_id`` points at a ``DROPPED`` question (the question is out
  of scope, so the answer is dead context). The lightest prune: it removes only
  provably-dead rows and keeps every live assertion.
* **L2 — drop resolved contradictions.** L1 plus drop ``REFUTED`` claims. A
  refuted claim is a recorded negative result; it stays in the durable ledger
  for traceability but is noise in a forward-looking context window.
* **L3 — collapse lexical duplicates.** L2 plus collapse claims whose
  *normalised title* (casefolded, punctuation-stripped, whitespace-squeezed)
  collides, keeping the newest survivor of each collision and dropping the
  rest. This is the first lexical pass — a keyword-level dedupe, not a semantic
  one (see the keyword-recall note below).
* **L4 — decay by age.** L3 plus drop claims whose ``created_at`` is older than
  :attr:`PruneConfig.retention_window` measured back from ``now`` — UNLESS the
  claim still backs a live (non-terminal) question, in which case it is
  retained regardless of age because it is load-bearing for an open gap. Focuses
  the budget on recent evidence without dropping the support under an unresolved
  question.
* **L5 — keep the top-K by keyword recall.** L4 plus rank the survivors by
  lexical keyword overlap against :attr:`PruneConfig.query` and keep only the
  top :attr:`PruneConfig.top_k` (ties broken newest-first, then by id for
  determinism). The most aggressive level: it answers "which claims are
  *about* the thing the dispatch is working on" with a pure lexical recall
  score. With no query, L5 degrades to a recency+id ordering so the level still
  produces a deterministic top-K.

Keyword-recall first; no vector / graph index (deliberate)
----------------------------------------------------------
The relevance primitive at ``L3`` (dedupe) and ``L5`` (top-K) is **lexical
keyword recall** — token-set overlap over normalised titles / descriptions.
There is deliberately **no embedding-vector index and no claim-graph traversal
index** in this module. Vector / graph retrieval is YAGNI until a *demonstrated*
recall failure: keyword recall is cheap, fully deterministic (a property these
pure reducers depend on), needs no model or build step, and is debuggable by
eye. An embedding index would add a build artifact, a similarity-threshold knob,
and non-determinism (model drift) for a recall lift that no observed campaign
has yet shown it needs. When a real recall miss is recorded against a closed
ledger, a vector / graph tier can be added as an ``L6`` without disturbing
``L1`` .. ``L5``; until then the lexical tier is the whole story.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum

from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion

logger = logging.getLogger(__name__)

#: Trailing window the L4 age-decay level retains claims within. A claim whose
#: ``created_at`` is older than this (measured back from ``now``) is dropped at
#: L4 unless it still backs a live question.
DEFAULT_RETENTION_WINDOW: timedelta = timedelta(days=7)

#: Default survivor cap for the L5 keyword-recall top-K level. A campaign rarely
#: needs more than this many claims of live context in a single dispatch; the
#: orchestrator overrides it per budget.
DEFAULT_TOP_K: int = 25

#: Claim statuses that L2+ treats as a recorded-but-dead contradiction. Kept
#: separate from the L1 dead-row set so the level boundaries stay legible.
_REFUTED_STATUSES: frozenset[ClaimStatus] = frozenset({ClaimStatus.REFUTED})

#: Question statuses that leave a question live (non-terminal). A claim that
#: backs a question in one of these states is age-exempt at L4.
_LIVE_QUESTION_STATUSES: frozenset[OpenQuestionStatus] = frozenset(
    {OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED}
)

#: Token splitter for the lexical keyword passes (L3 dedupe key + L5 recall
#: score). Splits on any run of non-alphanumeric characters so punctuation and
#: spacing never change a token set.
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")


class PruneLevel(IntEnum):
    """The five escalating context-pruning levels, ordered weakest to strongest.

    An :class:`enum.IntEnum` (not a :class:`enum.StrEnum`) because the levels
    have a meaningful order: ``L1 < L2 < ... < L5``, and a reducer escalates by
    comparing levels numerically. Each level drops a superset of the prior
    level's claims.
    """

    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5


class DropReason(IntEnum):
    """Why a claim was dropped, recorded as data on :class:`PruningResult`.

    Ordered by the level that first introduces the reason so a reader can map a
    reason back to the level. A single claim is attributed to the *first* reason
    that drops it as levels escalate (a SUPERSEDED claim dropped at L1 keeps the
    :attr:`SUPERSEDED` reason at L5, it is not re-attributed to a later rule).
    """

    #: L1: claim status is ``SUPERSEDED`` (dead, kept only for traceability).
    SUPERSEDED = 1
    #: L1: claim answers a question the question ledger marks ``DROPPED``.
    ANSWERS_DROPPED_QUESTION = 2
    #: L2: claim status is ``REFUTED`` (a recorded negative result).
    REFUTED = 3
    #: L3: claim's normalised title collided with a newer survivor.
    LEXICAL_DUPLICATE = 4
    #: L4: claim is older than the retention window and backs no live question.
    AGED_OUT = 5
    #: L5: claim fell below the top-K keyword-recall cut.
    BELOW_RECALL_CUT = 6


@dataclass(frozen=True)
class DroppedClaim:
    """One dropped claim id plus the reason it was pruned.

    Attributes:
        claim_id: Id of the dropped claim.
        reason: The :class:`DropReason` that removed it — the first rule to
            drop it as the levels escalated.
    """

    claim_id: str
    reason: DropReason


@dataclass(frozen=True)
class PruneConfig:
    """Tunable knobs for the L1-L5 reducers (all optional, all defaulted).

    Passing the same config + ``now`` + ledger to :func:`prune` always yields
    the same :class:`PruningResult` — the config carries no hidden state.

    Attributes:
        retention_window: L4 age-decay window. A claim older than this (back
            from ``now``) is dropped at L4 unless it backs a live question.
            Defaults to :data:`DEFAULT_RETENTION_WINDOW`.
        top_k: L5 survivor cap after keyword-recall ranking. Defaults to
            :data:`DEFAULT_TOP_K`.
        query: Free-text anchor the L5 recall score ranks against. Tokenised
            with the same lexical splitter as the L3 dedupe key. ``None`` (the
            default) makes L5 a pure recency+id ordering — still deterministic,
            just unranked by relevance.
    """

    retention_window: timedelta = DEFAULT_RETENTION_WINDOW
    top_k: int = DEFAULT_TOP_K
    query: str | None = None


@dataclass(frozen=True)
class PruningResult:
    """Typed outcome of a single :func:`prune` pass at one :class:`PruneLevel`.

    Produced only by :func:`prune` — never hand-constructed on the call path.
    The :attr:`kept` ids (in original ledger order) are the relevant slice the
    orchestrator feeds downstream; :attr:`dropped` records every removed claim
    with the reason so the prune is auditable.

    Attributes:
        level: The :class:`PruneLevel` this result was produced at.
        kept: Ids of the surviving claims, in original ledger order. At L5 this
            is the recall-ranked top-K re-sorted back into ledger order so the
            survivor set reads stably regardless of level.
        dropped: One :class:`DroppedClaim` per removed claim, in original ledger
            order, each carrying the first :class:`DropReason` that dropped it.
        input_count: Number of claims the reducer was handed (the full ledger
            size, including rows dropped at L1).
    """

    level: PruneLevel
    kept: tuple[str, ...]
    dropped: tuple[DroppedClaim, ...]
    input_count: int

    @property
    def kept_count(self) -> int:
        """Number of surviving claims."""
        return len(self.kept)

    def dropped_for(self, reason: DropReason) -> tuple[str, ...]:
        """Return the ids dropped for *reason*, in ledger order.

        Args:
            reason: The :class:`DropReason` to filter on.

        Returns:
            A tuple of claim ids whose :attr:`DroppedClaim.reason` equals
            *reason*. Empty when no claim was dropped for that reason.
        """
        return tuple(d.claim_id for d in self.dropped if d.reason is reason)


def _normalise_title(text: str) -> str:
    """Casefold + collapse to a punctuation-free, single-spaced lexical key.

    The L3 dedupe key. Two titles that differ only in case, punctuation, or
    internal whitespace map to the same key, so ``"Use a vector index."`` and
    ``"use a vector  index"`` collide.

    Args:
        text: The raw title to normalise.

    Returns:
        The normalised token-joined key (may be empty for an all-punctuation
        title).
    """
    tokens = [t for t in _TOKEN_SPLIT_RE.split(text.casefold()) if t]
    return " ".join(tokens)


def _token_set(text: str) -> frozenset[str]:
    """Tokenise *text* into a lexical token set for keyword-recall scoring."""
    return frozenset(t for t in _TOKEN_SPLIT_RE.split(text.casefold()) if t)


def _recall_score(claim: Claim, query_tokens: frozenset[str]) -> int:
    """Lexical keyword-overlap score of *claim* against *query_tokens*.

    The L5 relevance primitive: the count of distinct query tokens that appear
    in the claim's normalised title-plus-description token set. A higher score
    means more keyword overlap. Deliberately a plain set-intersection size — no
    TF-IDF weighting, no embedding similarity, no graph distance — so the score
    is integer, deterministic, and explainable.

    Args:
        claim: The claim to score.
        query_tokens: The tokenised query (empty when no query is configured,
            which makes every score zero so L5 falls back to recency+id order).

    Returns:
        The number of overlapping distinct tokens.
    """
    if not query_tokens:
        return 0
    haystack = claim.title if claim.description is None else f"{claim.title} {claim.description}"
    return len(_token_set(haystack) & query_tokens)


def prune(
    level: PruneLevel,
    claims: Iterable[Claim],
    open_questions: Iterable[OpenQuestion],
    *,
    now: datetime,
    config: PruneConfig | None = None,
) -> PruningResult:
    """Reduce the Claim ledger to the relevant slice at one pruning *level*.

    Pure: the same ``level`` + ledger + ``open_questions`` + ``now`` + ``config``
    always yield the same :class:`PruningResult`; no I/O, no mutation, no raise
    on the read path. A dropped claim is recorded on the result, not raised.

    The levels escalate cumulatively — ``prune(L<n>, ...)`` drops everything
    ``prune(L<n-1>, ...)`` drops plus the new rule for ``L<n>`` — so a survivor
    at level ``n`` is always a survivor at every level below ``n``. See the
    module docstring for the per-level rules.

    Args:
        level: Which :class:`PruneLevel` to prune at (``L1`` weakest ..
            ``L5`` strongest).
        claims: The Claim ledger — every row, including ``SUPERSEDED`` rows.
        open_questions: The OpenQuestion ledger — needed by L1 (DROPPED-question
            answers) and L4 (live-question age exemption).
        now: The reference instant the L4 retention window is measured back
            from. Passed in (not read from the clock) so the reducer stays pure.
        config: Optional :class:`PruneConfig` knobs; defaults are used when
            ``None``.

    Returns:
        A :class:`PruningResult` carrying the survivor ids in ledger order and
        every dropped claim with its reason.
    """
    cfg = config if config is not None else PruneConfig()
    claim_list: list[Claim] = list(claims)
    question_list: list[OpenQuestion] = list(open_questions)

    dropped_reason: dict[str, DropReason] = {}
    _apply_l1(claim_list, question_list, dropped_reason)
    if level >= PruneLevel.L2:
        _apply_l2(claim_list, dropped_reason)
    if level >= PruneLevel.L3:
        _apply_l3(claim_list, dropped_reason)
    if level >= PruneLevel.L4:
        _apply_l4(claim_list, question_list, dropped_reason, now=now, config=cfg)
    if level >= PruneLevel.L5:
        _apply_l5(claim_list, dropped_reason, config=cfg)

    kept = tuple(c.id for c in claim_list if c.id not in dropped_reason)
    dropped = tuple(
        DroppedClaim(claim_id=c.id, reason=dropped_reason[c.id])
        for c in claim_list
        if c.id in dropped_reason
    )
    logger.debug(
        f"prune level={level.name} input={len(claim_list)} kept={len(kept)} dropped={len(dropped)}"
    )
    return PruningResult(
        level=level,
        kept=kept,
        dropped=dropped,
        input_count=len(claim_list),
    )


def _apply_l1(
    claims: Sequence[Claim],
    questions: Sequence[OpenQuestion],
    dropped: dict[str, DropReason],
) -> None:
    """L1: drop SUPERSEDED claims and claims answering a DROPPED question."""
    dropped_question_ids = {q.id for q in questions if q.status is OpenQuestionStatus.DROPPED}
    for claim in claims:
        if claim.id in dropped:
            continue
        if claim.status is ClaimStatus.SUPERSEDED:
            dropped[claim.id] = DropReason.SUPERSEDED
        elif (
            claim.answers_question_id is not None
            and claim.answers_question_id in dropped_question_ids
        ):
            dropped[claim.id] = DropReason.ANSWERS_DROPPED_QUESTION


def _apply_l2(claims: Sequence[Claim], dropped: dict[str, DropReason]) -> None:
    """L2: drop REFUTED claims (recorded negative results, noise in context)."""
    for claim in claims:
        if claim.id in dropped:
            continue
        if claim.status in _REFUTED_STATUSES:
            dropped[claim.id] = DropReason.REFUTED


def _apply_l3(claims: Sequence[Claim], dropped: dict[str, DropReason]) -> None:
    """L3: collapse lexical-title duplicates, keeping the newest survivor.

    Among the not-yet-dropped claims, group by normalised title. For any group
    of size > 1 keep the newest (latest ``created_at``, id-tiebroken for
    determinism) and drop the rest as :attr:`DropReason.LEXICAL_DUPLICATE`. An
    empty normalised title (all-punctuation) is never deduped against another —
    such titles are treated as distinct so a stray punctuation row never
    swallows a real claim.
    """
    survivors = [c for c in claims if c.id not in dropped]
    by_key: dict[str, list[Claim]] = {}
    for claim in survivors:
        key = _normalise_title(claim.title)
        if not key:
            continue
        by_key.setdefault(key, []).append(claim)
    for group in by_key.values():
        if len(group) < 2:
            continue
        # Keep the newest; ties broken by id so the survivor is deterministic.
        winner = max(group, key=lambda c: (c.created_at, c.id))
        for claim in group:
            if claim.id != winner.id:
                dropped[claim.id] = DropReason.LEXICAL_DUPLICATE


def _apply_l4(
    claims: Sequence[Claim],
    questions: Sequence[OpenQuestion],
    dropped: dict[str, DropReason],
    *,
    now: datetime,
    config: PruneConfig,
) -> None:
    """L4: drop claims older than the retention window unless they back a live question."""
    cutoff = now - config.retention_window
    live_question_ids = {q.id for q in questions if q.status in _LIVE_QUESTION_STATUSES}
    for claim in claims:
        if claim.id in dropped:
            continue
        if claim.created_at >= cutoff:
            continue
        backs_live_question = (
            claim.answers_question_id is not None and claim.answers_question_id in live_question_ids
        )
        if not backs_live_question:
            dropped[claim.id] = DropReason.AGED_OUT


def _apply_l5(
    claims: Sequence[Claim],
    dropped: dict[str, DropReason],
    *,
    config: PruneConfig,
) -> None:
    """L5: keep the top-K survivors by keyword recall; drop the rest.

    Ranks the not-yet-dropped claims by :func:`_recall_score` against the
    configured query (descending), breaking ties by ``created_at`` (newest
    first) then id, and drops everything past :attr:`PruneConfig.top_k` as
    :attr:`DropReason.BELOW_RECALL_CUT`. A non-positive ``top_k`` keeps nothing;
    a ``top_k`` at or above the survivor count keeps everything (no drop). With
    no query every score is zero, so the cut is a pure recency+id ordering.
    """
    survivors = [c for c in claims if c.id not in dropped]
    if config.top_k >= len(survivors):
        return
    query_tokens = _token_set(config.query) if config.query is not None else frozenset()
    ranked = sorted(
        survivors,
        key=lambda c: (-_recall_score(c, query_tokens), _neg_epoch(c.created_at), c.id),
    )
    keep_ids = {c.id for c in ranked[: config.top_k]} if config.top_k > 0 else set()
    for claim in survivors:
        if claim.id not in keep_ids:
            dropped[claim.id] = DropReason.BELOW_RECALL_CUT


def _neg_epoch(moment: datetime) -> float:
    """Negated POSIX timestamp so a *newest-first* sort key stays ascending.

    ``sorted`` is ascending; negating the epoch makes a later instant sort
    before an earlier one without a separate ``reverse`` pass that would also
    flip the id tiebreaker.
    """
    return -moment.timestamp()


def prune_round_carryover(
    claims: Iterable[Claim],
    open_questions: Iterable[OpenQuestion],
    *,
    now: datetime,
) -> PruningResult:
    """Prune the claim ledger carried between campaign rounds (the L1 reducer).

    The between-rounds wire the bounded round loop calls after each round
    reconciles its findings: it runs the lightest :attr:`PruneLevel.L1` reducer
    over the accumulated ledger so the next round (and the synthesis) work over
    only the *live* claims -- the provably-dead rows (``SUPERSEDED`` claims +
    claims answering a ``DROPPED`` question) are dropped from the carried-forward
    context, while every live assertion is kept.

    L1 is the deliberate floor for the between-rounds prune: a campaign mid-run
    must not lose a live (possibly still-converging) claim, so the loop never
    escalates past dropping the provably-dead rows. The stronger levels
    (L2-L5) stay reserved for the orchestrator's per-dispatch context-budget
    fit (the P31 catalog), not the between-rounds carryover.

    Pure: the same ledgers + ``now`` always yield the same
    :class:`PruningResult` (it delegates to :func:`prune` at ``L1``).

    Args:
        claims: The accumulated Claim ledger after the round reconciled.
        open_questions: The OpenQuestion ledger (L1 needs the DROPPED-question
            set to drop claims answering a dropped question).
        now: The reference instant (unused by L1's rules but threaded so the
            signature matches the loop's per-round clock).

    Returns:
        The :class:`PruningResult` at :attr:`PruneLevel.L1` -- the survivor
        ids the next round carries plus every dropped dead row.
    """
    return prune(PruneLevel.L1, claims, open_questions, now=now)


__all__ = [
    "DEFAULT_RETENTION_WINDOW",
    "DEFAULT_TOP_K",
    "DropReason",
    "DroppedClaim",
    "PruneConfig",
    "PruneLevel",
    "PruningResult",
    "prune",
    "prune_round_carryover",
]
