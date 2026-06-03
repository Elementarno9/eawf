"""Tests for the Layer-3 LLM clarity-judge contract (P29-I07-W08).

The contract ships spawn-free: a criterion set (reused from the shared
clarity dimensions), a judge prompt, a minority-veto rollup, and golden
calibration anchors. These tests exercise the full round-trip and the
worked judgments the doc-clarity brief specifies, with NO live spawn — the
rollup reduces already-collected auditor bodies, so every test builds the
bodies directly.

Covered:

* The round-trip criterion-set -> prompt -> rollup -> EvidenceRecord, with
  a unanimous-pass panel landing a ``pass`` evidence row.
* The worked negative fixture: a ``description == title`` artifact scores
  ``why = 0`` and ``not_a_title_duplicate = 0`` (both blocking for the
  description surface), so each juror votes ``fail`` and the panel reduces
  to ``fail``.
* Minority-veto over three jurors: a single ``fail`` ballot vetoes the
  panel; a split with no veto routes to ``NEEDS_USER`` (evidence
  ``blocked``).
* Pointwise (not pairwise) scoring: the prompt asks for a per-dimension
  score on this artifact alone, one criterion per dimension, with no
  pairwise comparison framing.
* Zero new wire types: the rollup consumes
  :class:`AuditorReportBody` + :class:`CriterionVerdict` and emits an
  :class:`EvidenceRecord` — no new model is defined by the contract.
* Boundary + error paths: empty panel, a juror that scored no dimensions,
  an empty artifact, a single-juror panel, and anchor construction guards.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.store.kinds.agent_report import AuditorReportBody, CriterionVerdict
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.platform.lint.clarity_anchors import (
    ANCHOR_DIMENSION_KEYS,
    ANCHOR_SCORE_MAX,
    CALIBRATION_ANCHORS,
    DESCRIPTION_EQUALS_TITLE_ANCHOR,
    ClarityAnchor,
    negative_anchors,
    positive_anchors,
)
from eawf.platform.profiles.clarity import NEWCOMER_TEST_DIMENSIONS
from eawf.workflow.dispatch.clarity_judge import (
    CLARITY_DESCRIPTION_SURFACE,
    PASS_DIMENSION_SCORE,
    ClarityJudgeResult,
    build_clarity_judge_prompt,
    clarity_criteria,
    juror_verdict_from_criteria,
    parse_clarity_judge_body,
    rollup_clarity_judges,
)

_FIXED_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:wave:PROJ/P29-I07-W08"


def _criteria_from_scores(scores: dict[str, int]) -> list[CriterionVerdict]:
    """Build one CriterionVerdict per dimension from a per-dimension score map.

    The criterion label is the dimension's human label (the criterion-name
    slot the judge stamps); ``passed`` is the pointwise threshold the prompt
    asks for (score at or above :data:`PASS_DIMENSION_SCORE` passes).
    """
    key_to_label = {dim.key: dim.label for dim in NEWCOMER_TEST_DIMENSIONS}
    return [
        CriterionVerdict(criterion=key_to_label[key], passed=scores[key] >= PASS_DIMENSION_SCORE)
        for key in ANCHOR_DIMENSION_KEYS
    ]


def _judge_body(scores: dict[str, int], *, verdict: AgentReportVerdict) -> AuditorReportBody:
    """Build a clarity-judge AuditorReportBody scoring each dimension per *scores*."""
    return AuditorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="clarity judge ballot",
        target_id=_SCOPE,
        criteria=_criteria_from_scores(scores),
    )


def _all_pass_scores() -> dict[str, int]:
    """Return a per-dimension score map at the max anchor for every dimension."""
    return dict.fromkeys(ANCHOR_DIMENSION_KEYS, ANCHOR_SCORE_MAX)


# --------------------------------------------------------------------------
# Criterion set
# --------------------------------------------------------------------------


def test_clarity_criteria_reuses_shared_dimensions() -> None:
    """The criterion set IS the six shared clarity dimensions, in order."""
    assert clarity_criteria() == tuple(dim.label for dim in NEWCOMER_TEST_DIMENSIONS)
    assert len(clarity_criteria()) == 6


# --------------------------------------------------------------------------
# Judge prompt — pointwise, anchor-bearing
# --------------------------------------------------------------------------


def test_prompt_is_pointwise_with_one_criterion_per_dimension() -> None:
    """The prompt scores this artifact alone (pointwise), one criterion per dimension."""
    prompt = build_clarity_judge_prompt(
        "Some prose about a wave that a newcomer should follow.",
        surface=CLARITY_DESCRIPTION_SURFACE,
    )
    assert "pointwise" in prompt
    assert "do not compare this artifact against another" in prompt
    # One criterion per dimension: every dimension key and label appears.
    for dim in NEWCOMER_TEST_DIMENSIONS:
        assert dim.key in prompt
        assert dim.label in prompt
    # The output contract asks for exactly one criterion entry per dimension
    # (newline-insensitive: the rendered sentence may soft-wrap).
    collapsed = " ".join(prompt.split())
    assert "exactly one `criteria` entry per dimension above" in collapsed


def test_prompt_embeds_calibration_anchors() -> None:
    """The prompt embeds every calibration anchor's sample + expected scores."""
    prompt = build_clarity_judge_prompt("artifact text", surface="pr_bullet")
    for anchor in CALIBRATION_ANCHORS:
        assert anchor.anchor_id in prompt
        # The anchor's per-dimension scores are rendered (spot-check why_present).
        assert f"why_present={anchor.scores['why_present']}" in prompt


def test_prompt_marks_blocking_dimensions_on_description_surface() -> None:
    """On the description surface the prompt flags the two blocking dimensions."""
    prompt = build_clarity_judge_prompt("artifact", surface=CLARITY_DESCRIPTION_SURFACE)
    assert "BLOCKING for this surface" in prompt


def test_prompt_no_blocking_marker_off_description_surface() -> None:
    """Off the description surface no dimension is marked blocking."""
    prompt = build_clarity_judge_prompt("artifact", surface="docstring")
    assert "BLOCKING for this surface" not in prompt


def test_prompt_empty_artifact_raises() -> None:
    """An empty artifact has nothing to judge -> ValueError."""
    with pytest.raises(ValueError, match="artifact_text must be non-empty"):
        build_clarity_judge_prompt("   ", surface="docstring")


# --------------------------------------------------------------------------
# Per-juror reduction (blocking-dimension rule)
# --------------------------------------------------------------------------


def test_juror_all_pass_is_clean_pass() -> None:
    """A juror that passes every dimension casts a clean PASS."""
    criteria = _criteria_from_scores(_all_pass_scores())
    verdict = juror_verdict_from_criteria(criteria, surface=CLARITY_DESCRIPTION_SURFACE)
    assert verdict is AgentReportVerdict.PASS


def test_juror_blocking_fail_sinks_to_fail() -> None:
    """A failed blocking dimension (why=0) sinks the juror to FAIL on description surface."""
    scores = _all_pass_scores()
    scores["why_present"] = 0
    criteria = _criteria_from_scores(scores)
    verdict = juror_verdict_from_criteria(criteria, surface=CLARITY_DESCRIPTION_SURFACE)
    assert verdict is AgentReportVerdict.FAIL


def test_juror_nonblocking_fail_is_pass_with_followups() -> None:
    """A failed non-blocking dimension is a tracked nit (PASS_WITH_FOLLOWUPS)."""
    scores = _all_pass_scores()
    scores["scannable"] = 0
    criteria = _criteria_from_scores(scores)
    verdict = juror_verdict_from_criteria(criteria, surface=CLARITY_DESCRIPTION_SURFACE)
    assert verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS


def test_juror_blocking_dimension_not_blocking_off_description_surface() -> None:
    """why=0 off the description surface is only a followup, not a block."""
    scores = _all_pass_scores()
    scores["why_present"] = 0
    criteria = _criteria_from_scores(scores)
    verdict = juror_verdict_from_criteria(criteria, surface="docstring")
    assert verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS


def test_juror_empty_criteria_raises() -> None:
    """A juror that scored no dimension cannot cast a ballot -> ValueError."""
    with pytest.raises(ValueError, match="must score at least one dimension"):
        juror_verdict_from_criteria([], surface="docstring")


# --------------------------------------------------------------------------
# Round-trip: criterion-set -> prompt -> rollup -> EvidenceRecord
# --------------------------------------------------------------------------


def test_round_trip_unanimous_pass_lands_pass_evidence() -> None:
    """A unanimous-pass panel reduces to PASS and lands a pass EvidenceRecord."""
    # criterion-set -> prompt (proves the prompt builds from the shared set).
    prompt = build_clarity_judge_prompt(
        "A motivation-first description a newcomer can follow.",
        surface=CLARITY_DESCRIPTION_SURFACE,
    )
    assert "Clarity judge" in prompt

    bodies = [_judge_body(_all_pass_scores(), verdict=AgentReportVerdict.PASS) for _ in range(3)]
    result = rollup_clarity_judges(
        bodies,
        scope_id=_SCOPE,
        surface=CLARITY_DESCRIPTION_SURFACE,
        refs=("D-DOC-CLARITY-01",),
        now=_FIXED_NOW,
    )

    assert isinstance(result, ClarityJudgeResult)
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.juror_verdicts == (AgentReportVerdict.PASS,) * 3

    evidence = result.evidence
    assert isinstance(evidence, EvidenceRecord)
    assert evidence.evidence_kind == "jury"
    assert evidence.produced_by == "agent"
    assert evidence.status == "pass"
    assert evidence.scope_id == _SCOPE
    assert evidence.refs == ["D-DOC-CLARITY-01"]
    assert evidence.created_at == _FIXED_NOW
    assert evidence.metrics == {"juror_count": 3, "veto_count": 0}


def test_round_trip_worked_negative_description_equals_title() -> None:
    """The brief's worked negative: description==title -> why=0 + not-a-dup=0 -> fail."""
    anchor = DESCRIPTION_EQUALS_TITLE_ANCHOR
    # The anchor itself encodes the worked judgment from the brief.
    assert anchor.scores["why_present"] == 0
    assert anchor.scores["not_a_title_duplicate"] == 0

    # Each juror scores the anchor's per-dimension scores; the per-juror
    # reduction must sink to FAIL on the description surface.
    juror_verdict = juror_verdict_from_criteria(
        _criteria_from_scores(anchor.scores),
        surface=CLARITY_DESCRIPTION_SURFACE,
    )
    assert juror_verdict is AgentReportVerdict.FAIL

    bodies = [_judge_body(anchor.scores, verdict=AgentReportVerdict.FAIL) for _ in range(3)]
    result = rollup_clarity_judges(
        bodies,
        scope_id=_SCOPE,
        surface=CLARITY_DESCRIPTION_SURFACE,
        now=_FIXED_NOW,
    )
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.evidence.status == "fail"
    assert result.evidence.metrics == {"juror_count": 3, "veto_count": 3}


# --------------------------------------------------------------------------
# Minority-veto over three jurors
# --------------------------------------------------------------------------


def test_minority_veto_single_fail_vetoes_panel() -> None:
    """One FAIL ballot among three vetoes the whole panel to FAIL."""
    pass_scores = _all_pass_scores()
    fail_scores = _all_pass_scores()
    fail_scores["why_present"] = 0  # blocking -> this juror votes fail
    bodies = [
        _judge_body(pass_scores, verdict=AgentReportVerdict.PASS),
        _judge_body(pass_scores, verdict=AgentReportVerdict.PASS),
        _judge_body(fail_scores, verdict=AgentReportVerdict.FAIL),
    ]
    result = rollup_clarity_judges(bodies, scope_id=_SCOPE, surface=CLARITY_DESCRIPTION_SURFACE)
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.aggregate.veto_count == 1
    assert result.evidence.status == "fail"


def test_minority_veto_split_without_veto_needs_user() -> None:
    """A pass + pass-with-followups split with no veto routes to NEEDS_USER (blocked)."""
    pass_scores = _all_pass_scores()
    followup_scores = _all_pass_scores()
    followup_scores["scannable"] = 0  # non-blocking -> pass-with-followups
    bodies = [
        _judge_body(pass_scores, verdict=AgentReportVerdict.PASS),
        _judge_body(followup_scores, verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS),
        _judge_body(followup_scores, verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS),
    ]
    result = rollup_clarity_judges(bodies, scope_id=_SCOPE, surface=CLARITY_DESCRIPTION_SURFACE)
    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.needs_user is True
    assert result.evidence.status == "blocked"
    assert result.aggregate.veto_count == 0


def test_rollup_single_juror_panel() -> None:
    """A single-juror panel reduces over that lone ballot (boundary case)."""
    bodies = [_judge_body(_all_pass_scores(), verdict=AgentReportVerdict.PASS)]
    result = rollup_clarity_judges(bodies, scope_id=_SCOPE, surface="docstring")
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.aggregate.ballot_count == 1
    assert result.juror_verdicts == (AgentReportVerdict.PASS,)


def test_rollup_empty_panel_raises() -> None:
    """An empty panel has nothing to reduce -> ValueError."""
    with pytest.raises(ValueError, match="at least one juror body"):
        rollup_clarity_judges([], scope_id=_SCOPE, surface="docstring")


# --------------------------------------------------------------------------
# Forced-schema validator (the live-juror seam, exercised without a spawn)
# --------------------------------------------------------------------------


def test_parse_clarity_judge_body_accepts_auditor_body() -> None:
    """A well-formed auditor body validates through the forced-schema adapter."""
    raw = {
        "role": "auditor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "clear",
        "target_id": _SCOPE,
        "criteria": [{"criterion": dim.label, "passed": True} for dim in NEWCOMER_TEST_DIMENSIONS],
    }
    body = parse_clarity_judge_body(raw)
    assert isinstance(body, AuditorReportBody)
    assert len(body.criteria) == 6


def test_parse_clarity_judge_body_rejects_non_auditor_role() -> None:
    """A non-auditor body fails the role discriminator (re-ask, never escapes)."""
    from pydantic import ValidationError

    raw = {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "wrong role",
        "wave_id": "P29-I07-W08",
        "outcome": "done",
    }
    with pytest.raises(ValidationError):
        parse_clarity_judge_body(raw)


# --------------------------------------------------------------------------
# Calibration anchors
# --------------------------------------------------------------------------


def test_anchors_cover_both_polarities_and_worked_surfaces() -> None:
    """The calibration set carries positive + negative anchors over the named surfaces."""
    assert len(positive_anchors()) >= 2
    assert len(negative_anchors()) >= 2
    surfaces = {a.surface for a in CALIBRATION_ANCHORS}
    assert "docstring" in surfaces
    assert "pr_bullet" in surfaces
    assert CLARITY_DESCRIPTION_SURFACE in surfaces


def test_anchor_scores_key_every_dimension() -> None:
    """Every anchor scores exactly the six canonical dimensions, in range."""
    for anchor in CALIBRATION_ANCHORS:
        assert set(anchor.scores) == set(ANCHOR_DIMENSION_KEYS)
        for value in anchor.scores.values():
            assert 0 <= value <= ANCHOR_SCORE_MAX


def test_anchor_missing_dimension_raises() -> None:
    """An anchor missing a dimension score fails construction."""
    partial = dict.fromkeys(ANCHOR_DIMENSION_KEYS[:-1], ANCHOR_SCORE_MAX)
    with pytest.raises(ValueError, match="missing dimension scores"):
        ClarityAnchor(
            anchor_id="bad",
            surface="docstring",
            polarity="positive",
            sample="x",
            scores=partial,
            rationale="missing one dimension",
        )


def test_anchor_unknown_dimension_raises() -> None:
    """An anchor with an unknown dimension key fails construction."""
    bad = dict.fromkeys(ANCHOR_DIMENSION_KEYS, ANCHOR_SCORE_MAX)
    bad["not_a_real_dimension"] = 2
    with pytest.raises(ValueError, match="unknown dimensions"):
        ClarityAnchor(
            anchor_id="bad",
            surface="docstring",
            polarity="positive",
            sample="x",
            scores=bad,
            rationale="extra dimension",
        )


def test_anchor_out_of_range_score_raises() -> None:
    """An anchor score above the max anchor fails construction."""
    bad = dict.fromkeys(ANCHOR_DIMENSION_KEYS, ANCHOR_SCORE_MAX)
    bad["audience_fit"] = ANCHOR_SCORE_MAX + 1
    with pytest.raises(ValueError, match=r"outside 0\.\."):
        ClarityAnchor(
            anchor_id="bad",
            surface="docstring",
            polarity="positive",
            sample="x",
            scores=bad,
            rationale="score too high",
        )
