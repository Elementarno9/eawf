"""Tests for :mod:`eawf.kernel.spec.pruning`.

Pins the L1-L5 escalating context-pruning reducers over the Claim ledger:

1. Each level drops what its rule names AND a superset of the weaker levels:
   L1 SUPERSEDED + answers-DROPPED-question; L2 + REFUTED; L3 + lexical-title
   duplicate (newest survives); L4 + aged-out (live-question exempt); L5 + the
   top-K keyword-recall cut.
2. Empty ledger -> empty kept / empty dropped at every level.
3. Large ledger -> top-K cut keeps exactly K survivors at L5.
4. Keyword-recall hit / miss -> a query-matching claim outranks a non-matching
   one at the L5 cut; with no query L5 degrades to a deterministic recency+id
   order.
5. The reducers are pure: same inputs -> equal result, input ledgers unmutated.
6. :meth:`PruningResult.dropped_for` filters by reason; the level cumulativity
   invariant holds (survivors shrink monotonically L1 -> L5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eawf.kernel.spec.pruning import (
    DEFAULT_RETENTION_WINDOW,
    DEFAULT_TOP_K,
    DroppedClaim,
    DropReason,
    PruneConfig,
    PruneLevel,
    PruningResult,
    prune,
)
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:campaign:OWNER/RES-42"

# Recent enough to survive the L4 age-decay window by default.
_RECENT = _NOW - timedelta(hours=1)
# Comfortably older than the default retention window so L4 ages it out.
_OLD = _NOW - DEFAULT_RETENTION_WINDOW - timedelta(days=1)


def _claim(
    *,
    claim_id: str,
    title: str = "State the claim",
    description: str | None = None,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    created_at: datetime = _RECENT,
    evidence_refs: list[str] | None = None,
    answers_question_id: str | None = None,
    superseded_by: str | None = None,
) -> Claim:
    """Return a Claim on recent, evidence-backed, terminal-ok defaults."""
    return Claim(
        id=claim_id,
        scope_id=_SCOPE,
        title=title,
        description=description,
        status=status,
        evidence_refs=["src/eawf/kernel/spec/pruning.py"]
        if evidence_refs is None
        else evidence_refs,
        answers_question_id=answers_question_id,
        created_at=created_at,
        superseded_by=superseded_by,
    )


def _question(
    *,
    question_id: str,
    status: OpenQuestionStatus = OpenQuestionStatus.OPEN,
) -> OpenQuestion:
    """Return an OpenQuestion on minimal valid defaults (OPEN by default)."""
    return OpenQuestion(
        id=question_id,
        scope_id=_SCOPE,
        title="Frame the question",
        status=status,
        created_at=_OLD,
        resolved_at=_NOW
        if status in (OpenQuestionStatus.ANSWERED, OpenQuestionStatus.DROPPED)
        else None,
    )


def _prune(
    level: PruneLevel,
    claims: list[Claim],
    questions: list[OpenQuestion] | None = None,
    *,
    config: PruneConfig | None = None,
) -> PruningResult:
    """Prune at the fixed reference instant with the given (or default) config."""
    return prune(level, claims, questions or [], now=_NOW, config=config)


# L1: dead-row prune ------------------------------------------------------


def test_l1_drops_superseded_claim() -> None:
    claims = [
        _claim(claim_id="C0", status=ClaimStatus.SUPERSEDED, superseded_by="C1"),
        _claim(claim_id="C1"),
    ]
    result = _prune(PruneLevel.L1, claims)

    assert result.kept == ("C1",)
    assert result.dropped_for(DropReason.SUPERSEDED) == ("C0",)
    assert result.input_count == 2
    assert result.kept_count == 1


def test_l1_drops_claim_answering_dropped_question() -> None:
    claims = [_claim(claim_id="C1", answers_question_id="Q1")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.DROPPED)]
    result = _prune(PruneLevel.L1, claims, questions)

    assert result.kept == ()
    assert result.dropped_for(DropReason.ANSWERS_DROPPED_QUESTION) == ("C1",)


def test_l1_keeps_live_claim_answering_open_question() -> None:
    claims = [_claim(claim_id="C1", answers_question_id="Q1")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)]
    result = _prune(PruneLevel.L1, claims, questions)

    assert result.kept == ("C1",)
    assert result.dropped == ()


def test_l1_keeps_refuted_claim() -> None:
    # REFUTED is an L2 drop, not L1; L1 must keep it.
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED)]
    result = _prune(PruneLevel.L1, claims)

    assert result.kept == ("C1",)


# L2: + refuted -----------------------------------------------------------


def test_l2_drops_refuted_claim() -> None:
    claims = [
        _claim(claim_id="C1", status=ClaimStatus.REFUTED),
        _claim(claim_id="C2", status=ClaimStatus.SUPPORTED),
    ]
    result = _prune(PruneLevel.L2, claims)

    assert result.kept == ("C2",)
    assert result.dropped_for(DropReason.REFUTED) == ("C1",)


def test_l2_still_drops_l1_rows() -> None:
    # L2 is a superset of L1: the SUPERSEDED row stays dropped with its L1 reason.
    claims = [
        _claim(claim_id="C0", status=ClaimStatus.SUPERSEDED),
        _claim(claim_id="C1", status=ClaimStatus.REFUTED),
        _claim(claim_id="C2"),
    ]
    result = _prune(PruneLevel.L2, claims)

    assert result.kept == ("C2",)
    assert result.dropped_for(DropReason.SUPERSEDED) == ("C0",)
    assert result.dropped_for(DropReason.REFUTED) == ("C1",)


# L3: + lexical dedupe ----------------------------------------------------


def test_l3_collapses_lexical_title_duplicates_keeping_newest() -> None:
    older = _NOW - timedelta(hours=5)
    newer = _NOW - timedelta(hours=1)
    claims = [
        _claim(claim_id="C1", title="Use a vector index", created_at=older),
        _claim(claim_id="C2", title="use a  vector index.", created_at=newer),
    ]
    result = _prune(PruneLevel.L3, claims)

    # Normalised titles collide (case / punctuation / spacing) -> newest wins.
    assert result.kept == ("C2",)
    assert result.dropped_for(DropReason.LEXICAL_DUPLICATE) == ("C1",)


def test_l3_keeps_distinct_titles() -> None:
    claims = [
        _claim(claim_id="C1", title="Use a vector index"),
        _claim(claim_id="C2", title="Use a keyword index"),
    ]
    result = _prune(PruneLevel.L3, claims)

    assert result.kept == ("C1", "C2")
    assert result.dropped == ()


def test_l3_empty_title_keys_are_not_deduped() -> None:
    # All-punctuation titles normalise to "" and must NOT collapse together.
    claims = [
        _claim(claim_id="C1", title="!!!"),
        _claim(claim_id="C2", title="---"),
    ]
    result = _prune(PruneLevel.L3, claims)

    assert set(result.kept) == {"C1", "C2"}


# L4: + age decay ---------------------------------------------------------


def test_l4_drops_aged_claim() -> None:
    claims = [
        _claim(claim_id="C1", title="Aged claim one", created_at=_OLD),
        _claim(claim_id="C2", title="Recent claim two", created_at=_RECENT),
    ]
    result = _prune(PruneLevel.L4, claims)

    assert result.kept == ("C2",)
    assert result.dropped_for(DropReason.AGED_OUT) == ("C1",)


def test_l4_retention_boundary_is_inclusive() -> None:
    # A claim exactly at the cutoff (now - window) is retained (uses >= cutoff).
    at_cutoff = _NOW - DEFAULT_RETENTION_WINDOW
    claims = [_claim(claim_id="C1", created_at=at_cutoff)]
    result = _prune(PruneLevel.L4, claims)

    assert result.kept == ("C1",)


def test_l4_exempts_aged_claim_backing_live_question() -> None:
    # Aged, but it answers an OPEN question -> load-bearing, so retained.
    claims = [_claim(claim_id="C1", created_at=_OLD, answers_question_id="Q1")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)]
    result = _prune(PruneLevel.L4, claims, questions)

    assert result.kept == ("C1",)
    assert result.dropped == ()


def test_l4_ages_out_claim_backing_answered_question() -> None:
    # ANSWERED is terminal, not live -> no age exemption.
    claims = [_claim(claim_id="C1", created_at=_OLD, answers_question_id="Q1")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.ANSWERED)]
    result = _prune(PruneLevel.L4, claims, questions)

    assert result.kept == ()
    assert result.dropped_for(DropReason.AGED_OUT) == ("C1",)


# L5: + keyword-recall top-K ---------------------------------------------


def test_l5_keeps_top_k_by_keyword_recall_hit() -> None:
    claims = [
        _claim(claim_id="C1", title="Vector index recall tuning"),
        _claim(claim_id="C2", title="Unrelated billing ledger note"),
    ]
    config = PruneConfig(query="vector index", top_k=1)
    result = _prune(PruneLevel.L5, claims, config=config)

    # The query-matching claim survives the top-1 cut; the miss is dropped.
    assert result.kept == ("C1",)
    assert result.dropped_for(DropReason.BELOW_RECALL_CUT) == ("C2",)


def test_l5_recall_miss_when_no_token_overlap() -> None:
    claims = [
        _claim(claim_id="C1", title="Billing ledger reconciliation"),
        _claim(claim_id="C2", title="Sandbox deny-list enforcement"),
    ]
    config = PruneConfig(query="vector embedding similarity", top_k=1)
    result = _prune(PruneLevel.L5, claims, config=config)

    # No claim overlaps the query -> all score 0, tie broken newest-then-id.
    # Both built at the same _RECENT default, so id breaks the tie: C1 wins.
    assert result.kept == ("C1",)
    assert result.kept_count == 1


def test_l5_recall_scores_description_tokens() -> None:
    claims = [
        _claim(claim_id="C1", title="Index note", description="vector recall budget"),
        _claim(claim_id="C2", title="Index note two"),
    ]
    config = PruneConfig(query="vector recall", top_k=1)
    result = _prune(PruneLevel.L5, claims, config=config)

    assert result.kept == ("C1",)


def test_l5_no_query_degrades_to_recency_then_id() -> None:
    older = _NOW - timedelta(hours=5)
    newer = _NOW - timedelta(hours=1)
    claims = [
        _claim(claim_id="C1", created_at=older),
        _claim(claim_id="C2", created_at=newer),
    ]
    result = _prune(PruneLevel.L5, claims, config=PruneConfig(top_k=1))

    # Newest survives when no query ranks relevance.
    assert result.kept == ("C2",)


def test_l5_kept_preserves_ledger_order() -> None:
    # Survivors are re-sorted back into ledger order regardless of recall rank.
    claims = [
        _claim(claim_id="C1", title="alpha vector"),
        _claim(claim_id="C2", title="beta keyword"),
        _claim(claim_id="C3", title="gamma vector keyword"),
    ]
    config = PruneConfig(query="vector keyword", top_k=2)
    result = _prune(PruneLevel.L5, claims, config=config)

    # C3 (overlap 2) and C1-or-C2 (overlap 1) survive; kept is in ledger order.
    assert result.kept == tuple(c for c in ("C1", "C2", "C3") if c in result.kept)
    assert "C3" in result.kept
    assert result.kept_count == 2


def test_l5_top_k_at_or_above_survivors_drops_nothing() -> None:
    claims = [
        _claim(claim_id="C1", title="First distinct claim"),
        _claim(claim_id="C2", title="Second distinct claim"),
    ]
    result = _prune(PruneLevel.L5, claims, config=PruneConfig(top_k=2))

    assert set(result.kept) == {"C1", "C2"}
    assert result.dropped == ()


def test_l5_top_k_zero_keeps_nothing() -> None:
    claims = [
        _claim(claim_id="C1", title="First distinct claim"),
        _claim(claim_id="C2", title="Second distinct claim"),
    ]
    result = _prune(PruneLevel.L5, claims, config=PruneConfig(top_k=0))

    assert result.kept == ()
    assert result.dropped_for(DropReason.BELOW_RECALL_CUT) == ("C1", "C2")


# Empty ledger ------------------------------------------------------------


def test_empty_ledger_keeps_and_drops_nothing_at_every_level() -> None:
    for level in PruneLevel:
        result = _prune(level, [])
        assert result.kept == ()
        assert result.dropped == ()
        assert result.input_count == 0
        assert result.kept_count == 0
        assert result.level is level


# Large ledger ------------------------------------------------------------


def test_large_ledger_l5_keeps_exactly_top_k() -> None:
    # 200 live, recent, distinct, evidence-backed claims; L5 caps at top_k.
    claims = [
        _claim(
            claim_id=f"C{i:03d}",
            title=f"Distinct claim number {i}",
            created_at=_NOW - timedelta(minutes=i),
        )
        for i in range(200)
    ]
    result = _prune(PruneLevel.L5, claims, config=PruneConfig(top_k=DEFAULT_TOP_K))

    assert result.input_count == 200
    assert result.kept_count == DEFAULT_TOP_K
    assert len(result.dropped) == 200 - DEFAULT_TOP_K
    # No query -> recency order; the 25 newest (lowest minute offset) survive.
    assert set(result.kept) == {f"C{i:03d}" for i in range(DEFAULT_TOP_K)}


def test_large_ledger_levels_shrink_monotonically() -> None:
    # A mixed ledger; survivor count is non-increasing as the level escalates.
    claims = [
        _claim(claim_id="S0", status=ClaimStatus.SUPERSEDED),
        _claim(claim_id="R0", status=ClaimStatus.REFUTED),
        _claim(claim_id="D0", title="Same title", created_at=_NOW - timedelta(hours=2)),
        _claim(claim_id="D1", title="same title.", created_at=_NOW - timedelta(hours=1)),
        _claim(claim_id="A0", created_at=_OLD),
        _claim(claim_id="L0", title="Live recent one"),
        _claim(claim_id="L1", title="Live recent two"),
        _claim(claim_id="L2", title="Live recent three"),
    ]
    counts = [_prune(level, claims, config=PruneConfig(top_k=2)).kept_count for level in PruneLevel]

    assert counts == sorted(counts, reverse=True)
    # And every later level keeps a subset of an earlier level's survivors.
    kept_by_level = {
        level: set(_prune(level, claims, config=PruneConfig(top_k=2)).kept) for level in PruneLevel
    }
    assert kept_by_level[PruneLevel.L5] <= kept_by_level[PruneLevel.L4]
    assert kept_by_level[PruneLevel.L4] <= kept_by_level[PruneLevel.L3]
    assert kept_by_level[PruneLevel.L3] <= kept_by_level[PruneLevel.L2]
    assert kept_by_level[PruneLevel.L2] <= kept_by_level[PruneLevel.L1]


# Purity ------------------------------------------------------------------


def test_prune_is_pure_same_inputs_same_result() -> None:
    claims = [_claim(claim_id="C1"), _claim(claim_id="C2", title="Other claim")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)]
    config = PruneConfig(query="claim", top_k=1)

    first = prune(PruneLevel.L5, claims, questions, now=_NOW, config=config)
    second = prune(PruneLevel.L5, claims, questions, now=_NOW, config=config)

    assert first == second


def test_prune_does_not_mutate_input_ledgers() -> None:
    claims = [_claim(claim_id="C1"), _claim(claim_id="C2", status=ClaimStatus.REFUTED)]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.DROPPED)]

    _prune(PruneLevel.L5, list(claims), list(questions))

    assert len(claims) == 2
    assert len(questions) == 1
    assert claims[1].status is ClaimStatus.REFUTED


# Result helpers ----------------------------------------------------------


def test_dropped_for_unknown_reason_is_empty() -> None:
    claims = [_claim(claim_id="C1")]
    result = _prune(PruneLevel.L1, claims)

    assert result.dropped_for(DropReason.AGED_OUT) == ()


def test_dropped_claim_carries_first_reason_under_escalation() -> None:
    # A SUPERSEDED claim is dropped at L1; at L5 it keeps the L1 reason, it is
    # not re-attributed to a later rule.
    claims = [
        _claim(claim_id="C0", status=ClaimStatus.SUPERSEDED, created_at=_OLD),
        _claim(claim_id="C1"),
    ]
    result = _prune(PruneLevel.L5, claims, config=PruneConfig(top_k=10))

    assert DroppedClaim(claim_id="C0", reason=DropReason.SUPERSEDED) in result.dropped


def test_default_constants_exposed() -> None:
    assert DEFAULT_TOP_K == 25
    assert timedelta(days=7) == DEFAULT_RETENTION_WINDOW


def test_prune_level_is_ordered() -> None:
    assert PruneLevel.L1 < PruneLevel.L5
    assert isinstance(PruningResult, type)
