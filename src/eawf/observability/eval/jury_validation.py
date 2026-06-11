"""Jury-validation cohort: silver-labelled + gold-subset verdict outcomes (P30-I09-W01).

A reliability scorer (:mod:`eawf.observability.eval.reputation`) can already turn
a stream of :class:`~eawf.observability.eval.reputation.VerdictOutcome` rows into
a held-rate, but it has no *ground truth* to validate the jury against -- no
answer to "was the verdict actually right?". This module builds that validation
substrate: it joins each observed ``VerdictOutcome`` from
:func:`~eawf.observability.eval.reputation.build_verdict_outcomes` to a boolean
``ground_truth`` and packages the result as a :class:`ValidationCohort` of
:class:`LabeledVerdict` rows.

Two label tiers, ordered by trust:

- **Silver** is the cheap, automatic label -- it reuses the held-outcome signal
  the outcome loop already observes from state alone. A refuted wave
  (reactive / rework / revert / reopen, i.e. ``held is False``) is known-bad
  ground truth; a clean closed wave (``held is True``) is known-good. An
  in-flight verdict (``held is None``) has no settled outcome, so it carries no
  silver label and is excluded from the cohort -- never a fabricated truth.
- **Gold** is the operator override -- an append-only :class:`GoldLabel` record
  pins the ground truth for a specific wave by hand, overriding whatever silver
  the held-outcome implied. The gold subset is the high-confidence validation
  set the jury is scored against when the cheap silver signal is wrong or
  absent.

Fail-fast at ingestion: a :class:`GoldLabel` naming a ``wave_id`` absent from
*state* raises :class:`ValueError` -- there is no wave to anchor the label on,
so a typo or stale id is a hard error rather than a silently dropped row.

Honest-empty by construction: with no verdict rows and no gold store the cohort
is ``ValidationCohort(silver=[], gold=[])``. The substrate is empty today (zero
AUDITOR verdict rows on disk), so :func:`build_jury_validation_cohort` returns an
empty cohort right now -- and that empty cohort IS the deliverable. It never
fabricates a label.

The gold store is a plain append-only JSONL file of :class:`GoldLabel` records
under ``<state_dir>/store/`` (one record per line), read honest-empty when the
file is absent. The reducer is pure: no mutation, no git, no daemon.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.models import State
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.paths import store_dir
from eawf.observability.eval.jury import JurorBallot
from eawf.observability.eval.reputation import (
    VerdictOutcome,
    _murphy_decomposition,
    build_verdict_outcomes,
    expected_calibration_error,
)
from eawf.workflow.evidence.rung2 import (
    ENTAIL_THRESHOLD,
    EntailmentScorer,
    LexicalEntailmentScorer,
    score_claim,
)

logger = logging.getLogger(__name__)

#: Honesty-gate floor for the jury-validation reducer: a cohort with fewer
#: labelled verdicts than this refuses to score every metric. The default
#: mirrors the reputation engine's ``min_n`` floor so a jury is validated on
#: the same sample budget a single role's reliability is scored on. Set to one
#: (the minimum a non-degenerate Fleiss / Brier needs) only in a test that
#: pins a tiny cohort.
_DEFAULT_MIN_VALIDATION_N: int = 20

#: Filename of the append-only gold-label store under ``<state_dir>/store/``.
#: A plain JSONL of :class:`GoldLabel` records (one per line), distinct from the
#: typed-envelope stores so it needs no ``StoreKind`` registration.
_GOLD_LABEL_STORE = "gold_label.jsonl"


class LabelSource(StrEnum):
    """Which tier supplied a :class:`LabeledVerdict`'s ground truth.

    :attr:`SILVER` is the cheap automatic label derived from the held-outcome
    signal; :attr:`GOLD` is the operator override that pins the ground truth by
    hand. The source travels with every row so a downstream jury-validation
    scorer can weight the high-confidence gold subset over the cheap silver one.
    """

    SILVER = "silver"
    GOLD = "gold"


class GoldLabel(BaseModel):
    """One operator-pinned ground-truth label for a wave -- an append-only record.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at load rather than silently corrupting the
    gold subset. A gold label is the high-confidence override: it states, by
    hand, whether the wave the verdict was about was actually a good outcome,
    overriding whatever the cheap silver signal implied for that wave.

    Append-only: a wave re-labelled by the operator gets a fresh record appended,
    and the latest record for a ``wave_id`` wins (so a correction supersedes an
    earlier mistake without rewriting history).

    Attributes:
        wave_id: The wave the label is about (the verdict's ``base_id`` /
            ``VerdictOutcome.base_id``). MUST name a wave present in state --
            :func:`build_jury_validation_cohort` raises on an absent id.
        ground_truth: The operator's verdict on the wave -- ``True`` when the
            wave was actually a good outcome, ``False`` when it was bad.
        labeled_at: When the operator pinned the label (UTC). The latest
            ``labeled_at`` per wave wins when a wave carries multiple records.
        note: Optional free-text rationale for the operator label.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    ground_truth: bool
    labeled_at: UtcDatetime
    note: str | None = None


class LabeledVerdict(BaseModel):
    """One verdict outcome joined to a boolean ground truth -- a pure row.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The jury-validation
    scorer reads a stream of these: each row pairs a
    :class:`~eawf.observability.eval.reputation.VerdictOutcome` (the verdict the
    jury produced) with the ground truth it is validated against and the tier
    that supplied that truth.

    Attributes:
        outcome: The joined
            :class:`~eawf.observability.eval.reputation.VerdictOutcome` -- the
            verdict + its realized, state-observable held-outcome.
        ground_truth: Whether the wave was actually a good outcome (``True``) or
            a bad one (``False``).
        label_source: The :class:`LabelSource` tier that supplied
            *ground_truth* -- :attr:`LabelSource.SILVER` (held-outcome derived)
            or :attr:`LabelSource.GOLD` (operator override).
    """

    model_config = ConfigDict(extra="forbid")

    outcome: VerdictOutcome
    ground_truth: bool
    label_source: LabelSource


class ValidationCohort(BaseModel):
    """The jury-validation cohort: a silver set and its gold-override subset.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The cohort splits the
    labelled verdicts by tier so a scorer can validate the jury against the
    cheap silver labels broadly and against the high-confidence gold subset
    strictly.

    Honest-empty: with no observed verdict rows and no gold store both lists are
    empty -- the substrate is empty today, so this is the real result, not a bug.

    Attributes:
        silver: One :class:`LabeledVerdict` per observed verdict outcome whose
            wave is NOT overridden by a gold label, carrying the cheap
            held-outcome-derived ground truth. Ordered as
            :func:`~eawf.observability.eval.reputation.build_verdict_outcomes`
            returns the outcomes.
        gold: One :class:`LabeledVerdict` per observed verdict outcome whose wave
            IS overridden by a gold label, carrying the operator ground truth.
            Same ordering as *silver*.
    """

    model_config = ConfigDict(extra="forbid")

    silver: list[LabeledVerdict]
    gold: list[LabeledVerdict]


def _read_gold_labels(state_path: Path) -> list[GoldLabel]:
    """Read the append-only gold-label store, honest-empty when absent.

    Reads the plain JSONL store at ``<state_dir>/store/gold_label.jsonl`` (one
    :class:`GoldLabel` per line). Returns ``[]`` when the file does not exist --
    the honest-empty path for the gold tier today.

    Args:
        state_path: Path to ``state.json``; the gold-label store resolves under
            its sibling ``store/`` directory.

    Returns:
        Every :class:`GoldLabel` record on disk, oldest-first, or ``[]`` when no
        store file exists.
    """
    path = store_dir(state_path) / _GOLD_LABEL_STORE
    if not path.exists():
        return []
    labels: list[GoldLabel] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        labels.append(GoldLabel.model_validate_json(raw_line))
    return labels


def _latest_gold_by_wave(labels: list[GoldLabel]) -> dict[str, GoldLabel]:
    """Reduce gold records to the latest label per wave (latest ``labeled_at`` wins).

    The store is append-only, so a wave the operator re-labelled carries several
    records; only the most recent one is the live ground truth. Ties on
    ``labeled_at`` resolve to the last record seen (later append wins).

    Args:
        labels: Gold-label records, in store (oldest-first) order.

    Returns:
        A ``wave_id -> GoldLabel`` map holding the live label per wave.
    """
    latest: dict[str, GoldLabel] = {}
    for label in labels:
        current = latest.get(label.wave_id)
        if current is None or label.labeled_at >= current.labeled_at:
            latest[label.wave_id] = label
    return latest


def build_jury_validation_cohort(
    state: State,
    state_path: Path,
    *,
    iter_id: str | None = None,
) -> ValidationCohort:
    """Build the silver-labelled cohort with its gold-override subset -- pure.

    Joins each observed verdict outcome (from
    :func:`~eawf.observability.eval.reputation.build_verdict_outcomes`) to a
    boolean ground truth and splits the result into a silver set and a
    gold-override subset:

    - the cheap **silver** label reuses the held-outcome signal -- a refuted
      wave (``held is False``) is known-bad ground truth, a clean closed wave
      (``held is True``) is known-good. An in-flight verdict (``held is None``)
      has no settled outcome and so no silver label, and is excluded from the
      cohort -- never a fabricated truth;
    - an operator **gold** label (read from the append-only gold-label store)
      OVERRIDES the silver ground truth for its wave; that row moves to the gold
      subset carrying the operator's truth instead of the held-outcome's.

    Fail-fast: a gold label naming a ``wave_id`` absent from *state* raises
    :class:`ValueError` at ingestion -- there is no wave to anchor it on.

    Honest-empty: with no observed verdict rows and no gold store this returns
    ``ValidationCohort(silver=[], gold=[])`` -- the substrate is empty today, so
    that is the real result.

    Args:
        state: Loaded, validated :class:`~eawf.kernel.state.models.State`
            supplying the wave tree the outcomes and gold labels are anchored
            against.
        state_path: Path to ``state.json``; the AUDITOR verdict store and the
            gold-label store both resolve under its sibling ``store/`` directory.
        iter_id: Optional filter passed through to
            :func:`~eawf.observability.eval.reputation.build_verdict_outcomes` --
            restrict the cohort to verdicts whose wave belongs to this iter.
            ``None`` includes every wave with a verdict row.

    Returns:
        The :class:`ValidationCohort` whose *silver* list holds the
        held-outcome-labelled verdicts and whose *gold* list holds the
        operator-overridden subset.

    Raises:
        ValueError: When a gold label names a ``wave_id`` not present in
            *state*.
    """
    gold_by_wave = _latest_gold_by_wave(_read_gold_labels(state_path))
    for wave_id in gold_by_wave:
        if wave_id not in state.waves:
            raise ValueError(f"gold label names unknown wave: {wave_id!r}")

    outcomes = build_verdict_outcomes(state, state_path, iter_id=iter_id)
    silver: list[LabeledVerdict] = []
    gold: list[LabeledVerdict] = []
    for outcome in outcomes:
        if outcome.held is None:
            continue
        gold_label = gold_by_wave.get(outcome.base_id)
        if gold_label is not None:
            gold.append(
                LabeledVerdict(
                    outcome=outcome,
                    ground_truth=gold_label.ground_truth,
                    label_source=LabelSource.GOLD,
                )
            )
        else:
            silver.append(
                LabeledVerdict(
                    outcome=outcome,
                    ground_truth=outcome.held,
                    label_source=LabelSource.SILVER,
                )
            )

    logger.debug(
        f"build_jury_validation_cohort outcomes={len(outcomes)} "
        f"silver={len(silver)} gold={len(gold)} iter={iter_id!r}"
    )
    return ValidationCohort(silver=silver, gold=gold)


# --- jury-validation reducer (P30-I09-W02) --------------------------------
#
# The cohort above is a ground-truth-labelled validation set; this reducer
# scores the JURY against it. It reads the persisted per-juror ballots for
# every labelled wave and reduces four calibration metrics:
#
# - Fleiss kappa over the per-juror binary ballot matrix (pass / refute) --
#   how much the jurors AGREE beyond chance; perfect agreement -> 1.0;
# - the Brier score of the jury's pass-fraction forecast against the cohort
#   ground truth (REUSES :func:`_murphy_decomposition`) -- how well the jury's
#   confidence tracked the realized outcome;
# - the bucketed expected calibration error (ECE) of that same forecast (the
#   first real ECE compute in src/, in :mod:`reputation`);
# - the unanimous-pass-on-known-bad rate -- of the known-bad waves
#   (``ground_truth is False``), the fraction the jury UNANIMOUSLY passed (a
#   false clean -- the jury's worst failure mode).
#
# Refuse-to-score by construction: every metric is ``None`` EXACTLY when the
# labelled cohort holds fewer than ``min_validation_n`` rows -- the type makes
# the refusal unmissable (a caller cannot read a fabricated number out of a
# starved cohort). A wave that carries a labelled verdict but NO recorded
# ballots is a hard error: a verdict was claimed without a jury actually
# running, so the reducer raises rather than silently scoring a phantom jury.
#
# Closed-form only: no scipy / numpy. Fleiss kappa and ECE come from the
# ``math`` stdlib; the Brier reuses the Murphy decomposition.

#: Which binary category a juror's verdict falls into for the Fleiss matrix.
#: A ``PASS`` ballot is a vote that the wave is good; every other verdict
#: (``FAIL`` / ``BLOCKED`` / ``PASS_WITH_FOLLOWUPS``) is read as a non-pass
#: (a refutation or reservation), so the agreement matrix is binary.
_PASS_CATEGORY: int = 0
_REFUTE_CATEGORY: int = 1
_N_CATEGORIES: int = 2


class JuryValidationStatus(StrEnum):
    """Whether the jury-validation reducer scored the cohort or refused.

    :attr:`INSUFFICIENT` is the honest-negative surface: the labelled cohort is
    below the :attr:`JuryValidationConfig.min_validation_n` floor, so every
    numeric field on :class:`JuryValidationReport` stays ``None``.
    :attr:`SCORED` means the cohort cleared the floor and the numbers are real.
    """

    INSUFFICIENT = "insufficient"
    SCORED = "scored"


class JuryValidationConfig(BaseModel):
    """The config leaf governing the jury-validation reducer.

    ``extra="forbid"`` so a drifted config key surfaces as a
    :class:`pydantic.ValidationError` at load rather than silently changing the
    gate.

    Attributes:
        min_validation_n: Honesty-gate floor -- a labelled cohort with fewer
            rows refuses to score every metric (``>= 1``; a zero floor would
            defeat the refuse-to-score guarantee). A starved cohort yields a
            :class:`JuryValidationReport` whose every numeric field is ``None``.
        ece_bins: Number of equal-width buckets the ECE compute partitions the
            jury's forecast into (``>= 1``).
    """

    model_config = ConfigDict(extra="forbid")

    min_validation_n: int = Field(default=_DEFAULT_MIN_VALIDATION_N, ge=1)
    ece_bins: int = Field(default=10, ge=1)


class JuryValidationReport(BaseModel):
    """The jury validated against the ground-truth cohort -- a pure projection.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. Every numeric field is
    ``None`` EXACTLY when :attr:`status` is
    :attr:`JuryValidationStatus.INSUFFICIENT`, so the refuse-to-score contract
    is unmissable in the type: a caller cannot read a number out of an under-N
    cohort because there is no number to read.

    Attributes:
        n: Number of labelled verdicts the cohort carried (``>= 0``).
        status: :attr:`JuryValidationStatus.SCORED` when the cohort cleared the
            :attr:`JuryValidationConfig.min_validation_n` floor, else
            :attr:`JuryValidationStatus.INSUFFICIENT`.
        fleiss_kappa: Fleiss kappa over the per-juror binary ballot matrix
            (pass / refute) -- inter-juror agreement beyond chance, in
            ``[-1.0, 1.0]`` (``1.0`` is perfect agreement). ``None`` when the
            cohort refused to score.
        brier: Mean Brier score in ``[0.0, 1.0]`` (lower is better) of the
            jury's pass-fraction forecast against the cohort ground truth.
            ``None`` when the cohort refused to score.
        ece: Bucketed expected calibration error in ``[0.0, 1.0]`` (lower is
            better) of that same forecast. ``None`` when the cohort refused to
            score.
        unanimous_pass_on_known_bad_rate: Of the known-bad waves
            (``ground_truth is False``), the fraction the jury UNANIMOUSLY
            passed (a false clean), in ``[0.0, 1.0]``. ``None`` when the cohort
            refused to score OR when the cohort carries no known-bad wave (the
            rate is undefined with an empty denominator -- never a fabricated
            zero).
        known_bad_n: Number of known-bad waves in the cohort (``>= 0``), the
            denominator behind :attr:`unanimous_pass_on_known_bad_rate`.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=0)
    status: JuryValidationStatus
    fleiss_kappa: float | None = Field(default=None, ge=-1.0, le=1.0)
    brier: float | None = Field(default=None, ge=0.0, le=1.0)
    ece: float | None = Field(default=None, ge=0.0, le=1.0)
    unanimous_pass_on_known_bad_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    known_bad_n: int = Field(default=0, ge=0)


def _ballots_for_wave(
    wave_id: str,
    ballots_by_wave: Mapping[str, tuple[JurorBallot, ...]],
) -> tuple[JurorBallot, ...]:
    """Return the recorded ballots for *wave_id*, or raise when none ran.

    A labelled verdict asserts the jury reached a verdict on the wave, so its
    ballots MUST be on record. A wave absent from *ballots_by_wave*, or present
    with an empty ballot tuple, means a verdict was claimed without a jury
    actually running -- a hard error, not a silently scored phantom jury.

    Args:
        wave_id: The labelled wave to look up ballots for.
        ballots_by_wave: Per-wave recorded juror ballots.

    Returns:
        The non-empty ballot tuple recorded for *wave_id*.

    Raises:
        ValueError: When *wave_id* has no recorded ballots (absent or empty) --
            a verdict was claimed but no jury ran.
    """
    ballots = ballots_by_wave.get(wave_id)
    if not ballots:
        raise ValueError(f"labelled wave has no recorded jury ballots: {wave_id!r}")
    return ballots


def _ballot_category(ballot: JurorBallot) -> int:
    """Map one binary ballot to its Fleiss category (pass vs refute).

    A ``PASS`` verdict is the pass category; every other binary verdict is a
    non-pass (refutation or reservation). A graded ballot has no verdict, so it
    is read as a non-pass too -- the validation matrix is binary by design.
    """
    if ballot.verdict is AgentReportVerdict.PASS:
        return _PASS_CATEGORY
    return _REFUTE_CATEGORY


def _fleiss_kappa(rating_matrix: list[tuple[int, int]]) -> float:
    """Return Fleiss kappa over a per-item ``(pass_count, refute_count)`` matrix.

    Fleiss kappa measures inter-rater agreement beyond chance for *n* items
    each rated by the SAME number of raters into one of two categories. For
    item ``i`` with ``n_ij`` raters in category ``j`` (over ``r`` raters)::

        P_i  = (1 / (r * (r - 1))) * (sum_j n_ij**2 - r)
        P_bar     = mean_i P_i
        p_j       = (1 / (N * r)) * sum_i n_ij
        P_bar_e   = sum_j p_j**2
        kappa     = (P_bar - P_bar_e) / (1 - P_bar_e)

    Perfect agreement (every item rated unanimously) gives ``P_bar = 1`` and
    so ``kappa = 1.0``. When chance agreement is already total
    (``P_bar_e == 1``, i.e. every rating fell in one category) the formula is
    ``0/0``; that degenerate set is defined as ``1.0`` (the raters could not
    have agreed more), never a ``ZeroDivisionError``.

    Args:
        rating_matrix: One ``(pass_count, refute_count)`` row per item, each
            summing to the same rater count ``r >= 2``. Must be non-empty.

    Returns:
        Fleiss kappa in ``[-1.0, 1.0]``.

    Raises:
        ValueError: When the matrix is empty, when an item has fewer than two
            raters, or when items disagree on the rater count.
    """
    if not rating_matrix:
        raise ValueError("cannot compute fleiss kappa over an empty rating matrix")
    raters = rating_matrix[0][0] + rating_matrix[0][1]
    if raters < 2:
        raise ValueError(f"fleiss kappa needs at least 2 raters per item: {raters!r}")
    for pass_count, refute_count in rating_matrix:
        if pass_count + refute_count != raters:
            raise ValueError(
                f"fleiss kappa rater-count mismatch: expected {raters} "
                f"got {pass_count + refute_count}"
            )

    n_items = len(rating_matrix)
    item_agreements = [
        (pass_count * pass_count + refute_count * refute_count - raters) / (raters * (raters - 1))
        for pass_count, refute_count in rating_matrix
    ]
    p_bar = math.fsum(item_agreements) / n_items

    total_ratings = n_items * raters
    pass_fraction = math.fsum(row[0] for row in rating_matrix) / total_ratings
    refute_fraction = math.fsum(row[1] for row in rating_matrix) / total_ratings
    p_bar_e = pass_fraction * pass_fraction + refute_fraction * refute_fraction

    if p_bar_e >= 1.0:
        return 1.0
    return (p_bar - p_bar_e) / (1.0 - p_bar_e)


def _insufficient_report(*, n: int, known_bad_n: int) -> JuryValidationReport:
    """Return the honest-negative report for a cohort below the min-N floor.

    Every numeric metric stays ``None`` -- the refuse-to-score surface for a
    labelled cohort under :attr:`JuryValidationConfig.min_validation_n`.
    """
    return JuryValidationReport(
        n=n,
        status=JuryValidationStatus.INSUFFICIENT,
        known_bad_n=known_bad_n,
    )


def validate_jury(
    cohort: ValidationCohort,
    ballots_by_wave: Mapping[str, tuple[JurorBallot, ...]],
    config: JuryValidationConfig | None = None,
) -> JuryValidationReport:
    """Validate the jury against the ground-truth cohort -- a pure reducer.

    Joins each labelled verdict in *cohort* (silver + gold) to its persisted
    per-juror ballots and reduces four metrics over the joined set:

    - **Fleiss kappa** over the per-juror binary ballot matrix (pass / refute):
      inter-juror agreement beyond chance. Perfect agreement (every wave rated
      unanimously) -> ``1.0`` (see :func:`_fleiss_kappa`).
    - **Brier** score of the jury's pass-fraction forecast (the fraction of
      jurors that voted PASS on a wave) against the cohort ground truth, reusing
      :func:`~eawf.observability.eval.reputation._murphy_decomposition`.
    - **ECE** (bucketed expected calibration error) of that same forecast via
      :func:`~eawf.observability.eval.reputation.expected_calibration_error`.
    - **unanimous-pass-on-known-bad rate**: of the known-bad waves
      (``ground_truth is False``), the fraction the jury unanimously passed (a
      false clean). A unanimously-passed known-bad cohort -> ``1.0``.

    Refuse-to-score: a cohort holding fewer than
    :attr:`JuryValidationConfig.min_validation_n` labelled verdicts returns
    every metric ``None`` with :attr:`JuryValidationStatus.INSUFFICIENT` -- the
    numbers are never fabricated on a starved cohort.

    Fail-fast: a labelled wave with NO recorded ballots raises
    :class:`ValueError` -- a verdict was claimed but no jury ran, so the cohort
    cannot be scored honestly (see :func:`_ballots_for_wave`).

    The reducer is pure: no mutation, no IO, no git.

    Args:
        cohort: The :class:`ValidationCohort` to validate the jury against. Its
            silver + gold rows are scored as one labelled set; the gold tier's
            operator ground truth is already baked into each row's
            ``ground_truth``.
        ballots_by_wave: Per-wave recorded juror ballots, keyed by the wave id
            (``LabeledVerdict.outcome.base_id``). Every labelled wave MUST have
            a non-empty ballot tuple, else the reducer raises.
        config: The :class:`JuryValidationConfig` supplying the min-N floor and
            the ECE bucket count. ``None`` uses the defaults.

    Returns:
        The :class:`JuryValidationReport`. Every numeric field is ``None``
        exactly when the cohort refused to score (under the min-N floor); the
        unanimous-pass rate is additionally ``None`` when the cohort carries no
        known-bad wave (an undefined rate, never a fabricated zero).

    Raises:
        ValueError: When a labelled wave in *cohort* has no recorded ballots in
            *ballots_by_wave* (a verdict claimed without a jury running).
    """
    cfg = config if config is not None else JuryValidationConfig()
    labelled = [*cohort.silver, *cohort.gold]
    n = len(labelled)
    known_bad_n = sum(1 for row in labelled if row.ground_truth is False)

    # Resolve ballots for every labelled wave first so a phantom-jury wave fails
    # fast even when the cohort is otherwise too small to score.
    wave_ballots = [
        (row, _ballots_for_wave(row.outcome.base_id, ballots_by_wave)) for row in labelled
    ]

    if n < cfg.min_validation_n:
        logger.debug(
            f"validate_jury n={n} min={cfg.min_validation_n} status=insufficient "
            f"known_bad={known_bad_n}"
        )
        return _insufficient_report(n=n, known_bad_n=known_bad_n)

    rating_matrix: list[tuple[int, int]] = []
    forecasts: list[float] = []
    outcomes: list[float] = []
    unanimous_pass_on_known_bad = 0
    for row, ballots in wave_ballots:
        categories = [_ballot_category(ballot) for ballot in ballots]
        pass_count = sum(1 for category in categories if category == _PASS_CATEGORY)
        refute_count = len(categories) - pass_count
        rating_matrix.append((pass_count, refute_count))

        forecasts.append(pass_count / len(categories))
        outcomes.append(1.0 if row.ground_truth else 0.0)

        if row.ground_truth is False and refute_count == 0:
            unanimous_pass_on_known_bad += 1

    fleiss_kappa = _fleiss_kappa(rating_matrix)
    brier, _reliability, _resolution = _murphy_decomposition(forecasts, outcomes)
    ece = expected_calibration_error(forecasts, outcomes, bins=cfg.ece_bins)
    unanimous_rate = (
        unanimous_pass_on_known_bad / known_bad_n if known_bad_n > 0 else None
    )

    logger.debug(
        f"validate_jury n={n} status=scored fleiss={fleiss_kappa:.4f} brier={brier:.4f} "
        f"ece={ece:.4f} known_bad={known_bad_n}"
    )
    return JuryValidationReport(
        n=n,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=fleiss_kappa,
        brier=brier,
        ece=ece,
        unanimous_pass_on_known_bad_rate=unanimous_rate,
        known_bad_n=known_bad_n,
    )


# --- verbosity-bias + faithfulness probes (P30-I09-W03) -------------------
#
# Two adversarial probes that validate the jury for *bias* rather than
# *agreement*. The W02 reducer scores how well the jury tracked the realized
# outcome; these score two specific failure modes a calibrated-on-aggregate jury
# can still carry:
#
# - verbosity bias -- a juror that systematically passes a longer artifact (it
#   confuses length for quality / thoroughness). The probe correlates each
#   juror's pass signal with the judged artifact's byte-length and flags a juror
#   whose pass-rate rises with length above a ceiling (a length-preferring
#   juror);
# - citation unfaithfulness -- an ``evidence_ref`` that resolves on disk yet does
#   NOT entail the claim it cites (a hollow citation: the file is there, but it
#   does not support the claim). The probe scores entailment with the in-process
#   lexical scorer the EviBound rung-2 ships (:class:`LexicalEntailmentScorer`)
#   -- a structural/heuristic check, never a model spawn -- and flags a resolving
#   ref below the entailment floor as unfaithful.
#
# Both probes are None-gated under ``min_validation_n``: a probe with too few
# observations refuses to score (status ``INSUFFICIENT``, every numeric field
# ``None``) rather than fabricating a correlation or a faithfulness rate off a
# starved sample.


class ProbeStatus(StrEnum):
    """Whether a bias probe scored its observations or refused.

    :attr:`INSUFFICIENT` is the honest-negative surface: the observation set is
    below the probe's ``min_validation_n`` floor, so every scored field stays
    ``None``. :attr:`SCORED` means the probe cleared the floor and the numbers
    are real.
    """

    INSUFFICIENT = "insufficient"
    SCORED = "scored"


#: Default Spearman rank-correlation ceiling above which a juror's pass-rate is
#: read as rising with artifact length -- a length-preferring (verbosity-biased)
#: juror. ``0.5`` is a deliberately conservative floor: a juror whose pass signal
#: is more than half-explained by length ordering is flagged for operator review.
_DEFAULT_VERBOSITY_BIAS_CEILING: float = 0.5


class JurorLengthObservation(BaseModel):
    """One juror's pass/refute decision on an artifact of a known byte-length.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The verbosity-bias probe
    reads a stream of these per juror: each row pairs the judged artifact's
    byte-length with whether the juror passed it, so the probe can correlate the
    pass signal with length.

    Attributes:
        juror_id: Stable identifier of the juror that cast the decision (the
            juror is scored across all its observations).
        artifact_bytes: Byte-length of the artifact the juror judged (``>= 0``).
        passed: Whether the juror passed the artifact (``True``) or refuted /
            reserved on it (``False``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    juror_id: str = Field(min_length=1)
    artifact_bytes: int = Field(ge=0)
    passed: bool


class VerbosityBiasConfig(BaseModel):
    """The config governing the verbosity-bias probe.

    ``extra="forbid"`` so a drifted config key surfaces as a
    :class:`pydantic.ValidationError` at load rather than silently changing the
    flag threshold.

    Attributes:
        min_validation_n: Honesty-gate floor -- a juror with fewer length
            observations refuses to score its correlation (``>= 2``; a single
            observation has no rank order to correlate). A starved juror yields a
            ``None`` correlation and an unflagged row.
        verbosity_bias_ceiling: Spearman rank-correlation ceiling above which a
            juror is flagged length-preferring, in ``[-1.0, 1.0]``. A juror whose
            length/pass correlation strictly exceeds this is flagged.
    """

    model_config = ConfigDict(extra="forbid")

    min_validation_n: int = Field(default=_DEFAULT_MIN_VALIDATION_N, ge=2)
    verbosity_bias_ceiling: float = Field(
        default=_DEFAULT_VERBOSITY_BIAS_CEILING, ge=-1.0, le=1.0
    )


class JurorVerbosityBias(BaseModel):
    """One juror's verbosity-bias score over its length observations.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. Every numeric field is
    ``None`` EXACTLY when the juror carried fewer observations than the probe's
    ``min_validation_n`` floor (or when its lengths / passes are constant, so no
    rank correlation is defined) -- the refuse-to-score contract is unmissable in
    the type.

    Attributes:
        juror_id: The juror this row scores.
        n: Number of length observations the juror carried (``>= 0``).
        length_pass_correlation: Spearman rank correlation between artifact
            byte-length and the juror's pass signal, in ``[-1.0, 1.0]``. A
            positive value means the juror passes longer artifacts more often.
            ``None`` when the juror refused to score (too few observations) or
            when the correlation is undefined (constant lengths or constant
            passes -- never a fabricated zero).
        length_preferring: ``True`` when *length_pass_correlation* strictly
            exceeds the probe's ``verbosity_bias_ceiling`` -- a length-preferring
            (verbosity-biased) juror. ``False`` otherwise, including when the
            correlation is ``None`` (an unscored juror is never flagged).
    """

    model_config = ConfigDict(extra="forbid")

    juror_id: str = Field(min_length=1)
    n: int = Field(ge=0)
    length_pass_correlation: float | None = Field(default=None, ge=-1.0, le=1.0)
    length_preferring: bool = False


class VerbosityBiasReport(BaseModel):
    """The verbosity-bias probe over every juror -- a pure projection.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The whole report is
    ``INSUFFICIENT`` (and :attr:`jurors` empty) EXACTLY when the total
    observation count is below the probe's ``min_validation_n`` floor, so the
    refuse-to-score contract is unmissable: a caller cannot read a per-juror
    correlation out of a starved observation set.

    Attributes:
        n: Total number of length observations across all jurors (``>= 0``).
        status: :attr:`ProbeStatus.SCORED` when the observation set cleared the
            floor, else :attr:`ProbeStatus.INSUFFICIENT`.
        jurors: One :class:`JurorVerbosityBias` per scored juror, sorted by
            juror id. Empty when the probe refused to score.
        flagged_juror_ids: The ids of the jurors flagged length-preferring, in
            juror-id order. Empty when none are flagged or the probe refused to
            score.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=0)
    status: ProbeStatus
    jurors: tuple[JurorVerbosityBias, ...] = ()
    flagged_juror_ids: tuple[str, ...] = ()


def _rank(values: list[float]) -> list[float]:
    """Return fractional (tie-averaged) ranks of *values*, ascending.

    Ties share the average of the ranks they would occupy, so a constant column
    produces all-equal ranks (which the caller reads as an undefined
    correlation). Used by the Spearman correlation in
    :func:`_spearman_correlation`.

    Args:
        values: The values to rank.

    Returns:
        One rank per input value, in input order.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def _spearman_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Return the Spearman rank correlation between *xs* and *ys*, or ``None``.

    Spearman's rho is the Pearson correlation of the fractional ranks of the two
    series. It captures a monotone relationship (a juror whose pass-rate rises
    with length scores positive) without assuming linearity. Returns ``None``
    when either series is constant -- a constant column has zero rank variance,
    so the correlation is undefined and is never fabricated as a zero.

    Args:
        xs: First series (artifact byte-lengths).
        ys: Second series (pass signals, ``0.0`` / ``1.0``), aligned to *xs*.

    Returns:
        Spearman rho in ``[-1.0, 1.0]``, or ``None`` when undefined.

    Raises:
        ValueError: When *xs* and *ys* differ in length.
    """
    if len(xs) != len(ys):
        raise ValueError(f"spearman length mismatch: {len(xs)} vs {len(ys)}")
    rank_x = _rank(xs)
    rank_y = _rank(ys)
    n = len(xs)
    mean_x = math.fsum(rank_x) / n
    mean_y = math.fsum(rank_y) / n
    cov = math.fsum((rx - mean_x) * (ry - mean_y) for rx, ry in zip(rank_x, rank_y, strict=True))
    var_x = math.fsum((rx - mean_x) ** 2 for rx in rank_x)
    var_y = math.fsum((ry - mean_y) ** 2 for ry in rank_y)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    rho = cov / math.sqrt(var_x * var_y)
    return max(-1.0, min(1.0, rho))


def measure_verbosity_bias(
    observations: list[JurorLengthObservation],
    config: VerbosityBiasConfig | None = None,
) -> VerbosityBiasReport:
    """Probe each juror for verbosity bias -- a pure reducer.

    Groups *observations* by juror and, for each juror, correlates the judged
    artifact's byte-length with the juror's pass signal (Spearman rank
    correlation, :func:`_spearman_correlation`). A juror whose correlation
    strictly exceeds the :attr:`VerbosityBiasConfig.verbosity_bias_ceiling` is a
    length-preferring (verbosity-biased) juror -- it passes longer artifacts more
    often -- and is flagged.

    Refuse-to-score: when the TOTAL observation count is below
    :attr:`VerbosityBiasConfig.min_validation_n` the whole report is
    :attr:`ProbeStatus.INSUFFICIENT` with an empty :attr:`VerbosityBiasReport.jurors`
    -- the numbers are never fabricated on a starved sample. Above the floor, a
    per-juror correlation is still ``None`` (and the juror is unflagged) when that
    juror carries fewer than two observations or when its lengths / passes are
    constant (an undefined rank correlation, never a fabricated zero).

    The reducer is pure: no mutation, no IO, no spawn.

    Args:
        observations: One :class:`JurorLengthObservation` per (juror, artifact)
            decision. May be empty (the probe refuses to score).
        config: The :class:`VerbosityBiasConfig` supplying the min-N floor and
            the flag ceiling. ``None`` uses the defaults.

    Returns:
        The :class:`VerbosityBiasReport`. Every per-juror correlation is ``None``
        exactly when that juror refused to score; the whole report is
        ``INSUFFICIENT`` exactly when the total observation count is below the
        floor.
    """
    cfg = config if config is not None else VerbosityBiasConfig()
    n = len(observations)
    if n < cfg.min_validation_n:
        logger.debug(
            f"measure_verbosity_bias n={n} min={cfg.min_validation_n} status=insufficient"
        )
        return VerbosityBiasReport(n=n, status=ProbeStatus.INSUFFICIENT)

    by_juror: dict[str, list[JurorLengthObservation]] = {}
    for observation in observations:
        by_juror.setdefault(observation.juror_id, []).append(observation)

    juror_rows: list[JurorVerbosityBias] = []
    flagged: list[str] = []
    for juror_id in sorted(by_juror):
        rows = by_juror[juror_id]
        if len(rows) < 2:
            juror_rows.append(JurorVerbosityBias(juror_id=juror_id, n=len(rows)))
            continue
        lengths = [float(row.artifact_bytes) for row in rows]
        passes = [1.0 if row.passed else 0.0 for row in rows]
        correlation = _spearman_correlation(lengths, passes)
        length_preferring = correlation is not None and correlation > cfg.verbosity_bias_ceiling
        if length_preferring:
            flagged.append(juror_id)
        juror_rows.append(
            JurorVerbosityBias(
                juror_id=juror_id,
                n=len(rows),
                length_pass_correlation=correlation,
                length_preferring=length_preferring,
            )
        )

    logger.debug(
        f"measure_verbosity_bias n={n} status=scored jurors={len(juror_rows)} "
        f"flagged={len(flagged)}"
    )
    return VerbosityBiasReport(
        n=n,
        status=ProbeStatus.SCORED,
        jurors=tuple(juror_rows),
        flagged_juror_ids=tuple(flagged),
    )


class CitedEvidenceRef(BaseModel):
    """One claim joined to a resolving / non-resolving evidence reference.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The faithfulness probe
    reads a stream of these: each row pairs a claim with the evidence reference
    it cites, whether that reference RESOLVES on disk, and the resolved evidence
    text (when it resolves) the entailment is scored against.

    Faithfulness is only ever asked of a RESOLVING ref -- a ref that does not
    resolve is a separate (rung-1) failure, not an unfaithfulness. So a row with
    ``resolved=False`` is carried for the count but is never scored for
    entailment.

    Attributes:
        ref: The evidence reference string the claim cites (a repo-relative
            path, URN, or dense marker).
        claim: The claim text the reference is meant to support (the entailment
            hypothesis).
        resolved: Whether the reference resolves on disk (a rung-1 pass).
            ``True`` rows are scored for entailment; ``False`` rows are counted
            but not scored.
        evidence_text: The resolved evidence text (the entailment premise) for a
            ``resolved=True`` row, or ``None`` for an unresolved ref. A resolving
            ref MUST carry its evidence text, else the probe cannot score it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    resolved: bool
    evidence_text: str | None = None


class FaithfulnessConfig(BaseModel):
    """The config governing the citation-faithfulness probe.

    ``extra="forbid"`` so a drifted config key surfaces as a
    :class:`pydantic.ValidationError` at load rather than silently changing the
    entailment floor.

    Attributes:
        min_validation_n: Honesty-gate floor -- an observation set with fewer
            cited refs refuses to score every field (``>= 1``). A starved set
            yields a :class:`FaithfulnessReport` whose every numeric field is
            ``None``.
        entail_threshold: Entailment-probability floor at or above which a
            resolving ref's citation is read as faithful, in ``[0.0, 1.0]``.
            Defaults to the EviBound rung-2 :data:`~eawf.workflow.evidence.rung2.ENTAIL_THRESHOLD`
            so the probe shares the rung-2 entailment bar. A resolving ref scoring
            below this is flagged unfaithful.
    """

    model_config = ConfigDict(extra="forbid")

    min_validation_n: int = Field(default=_DEFAULT_MIN_VALIDATION_N, ge=1)
    entail_threshold: float = Field(default=ENTAIL_THRESHOLD, ge=0.0, le=1.0)


class EvidenceRefFaithfulness(BaseModel):
    """One cited evidence ref scored for faithfulness to its claim.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. A resolving ref carries a
    scored entailment probability and a faithfulness bit; an unresolved ref is
    carried unscored (faithfulness is only ever asked of a resolving ref).

    Attributes:
        ref: The evidence reference that was scored.
        claim: The claim the reference cites.
        resolved: Whether the reference resolved on disk.
        scored: Whether the entailment was scored (``True`` only for a resolving
            ref carrying its evidence text).
        entailment_probability: The lexical entailment probability of the claim
            against the resolved evidence, in ``[0.0, 1.0]``, or ``None`` for an
            unscored (unresolved) ref.
        faithful: ``True`` when a scored ref's entailment probability is at or
            above the :attr:`FaithfulnessConfig.entail_threshold` -- the
            resolving citation actually entails its claim. ``False`` when a
            scored ref falls below the floor (a resolving-but-non-entailing ref:
            unfaithful). ``None`` for an unscored ref.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    resolved: bool
    scored: bool
    entailment_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    faithful: bool | None = None


class FaithfulnessReport(BaseModel):
    """The citation-faithfulness probe over every cited ref -- a pure projection.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. The whole report is
    ``INSUFFICIENT`` (and :attr:`refs` empty, every rate ``None``) EXACTLY when
    the cited-ref count is below the probe's ``min_validation_n`` floor, so the
    refuse-to-score contract is unmissable.

    Attributes:
        n: Total number of cited refs the probe read (``>= 0``).
        status: :attr:`ProbeStatus.SCORED` when the cited-ref count cleared the
            floor, else :attr:`ProbeStatus.INSUFFICIENT`.
        scored_n: Number of refs that were scored for entailment (the resolving
            refs), ``>= 0``. The denominator behind
            :attr:`unfaithful_rate`.
        unfaithful_n: Number of resolving refs whose citation did NOT entail its
            claim (resolving-but-non-entailing), ``>= 0``.
        unfaithful_rate: Of the scored (resolving) refs, the fraction flagged
            unfaithful, in ``[0.0, 1.0]``. ``None`` when the probe refused to
            score OR when no ref resolved (an undefined rate with an empty
            denominator -- never a fabricated zero).
        refs: One :class:`EvidenceRefFaithfulness` per cited ref, in input order.
            Empty when the probe refused to score.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=0)
    status: ProbeStatus
    scored_n: int = Field(default=0, ge=0)
    unfaithful_n: int = Field(default=0, ge=0)
    unfaithful_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    refs: tuple[EvidenceRefFaithfulness, ...] = ()


def measure_faithfulness(
    cited_refs: list[CitedEvidenceRef],
    config: FaithfulnessConfig | None = None,
    *,
    scorer: EntailmentScorer | None = None,
) -> FaithfulnessReport:
    """Probe each cited evidence ref for faithfulness to its claim -- a pure reducer.

    For every RESOLVING ref (a ref that resolves on disk, the rung-1 pass) the
    probe scores whether the resolved evidence actually entails the claim it
    cites, using the in-process lexical scorer the EviBound rung-2 ships
    (:class:`~eawf.workflow.evidence.rung2.LexicalEntailmentScorer`) -- a
    structural/heuristic content-overlap check, NEVER a model spawn. A resolving
    ref whose entailment probability falls below the
    :attr:`FaithfulnessConfig.entail_threshold` is a resolving-but-non-entailing
    ref: a hollow citation (the file is there, but it does not support the claim),
    flagged unfaithful.

    An unresolved ref is carried for the count but never scored -- a ref that does
    not resolve is a separate rung-1 failure, not an unfaithfulness.

    Refuse-to-score: when the TOTAL cited-ref count is below
    :attr:`FaithfulnessConfig.min_validation_n` the whole report is
    :attr:`ProbeStatus.INSUFFICIENT` with an empty :attr:`FaithfulnessReport.refs`
    and a ``None`` :attr:`FaithfulnessReport.unfaithful_rate` -- the numbers are
    never fabricated on a starved sample. Above the floor, the unfaithful rate is
    still ``None`` when no ref resolved (an undefined rate with an empty
    denominator, never a fabricated zero).

    The reducer is pure: no mutation, no IO, no spawn. The entailment scorer runs
    in-process.

    Args:
        cited_refs: One :class:`CitedEvidenceRef` per (claim, evidence ref). May
            be empty (the probe refuses to score).
        config: The :class:`FaithfulnessConfig` supplying the min-N floor and the
            entailment floor. ``None`` uses the defaults.
        scorer: The in-process
            :class:`~eawf.workflow.evidence.rung2.EntailmentScorer` backend.
            ``None`` uses the zero-dependency
            :class:`~eawf.workflow.evidence.rung2.LexicalEntailmentScorer`.

    Returns:
        The :class:`FaithfulnessReport`. Every numeric field is ``None`` exactly
        when the probe refused to score; the unfaithful rate is additionally
        ``None`` when no ref resolved (an empty denominator).

    Raises:
        ValueError: When a resolving ref carries no evidence text -- a resolving
            ref MUST supply its resolved premise, else it cannot be scored.
    """
    cfg = config if config is not None else FaithfulnessConfig()
    backend: EntailmentScorer = scorer if scorer is not None else LexicalEntailmentScorer()
    n = len(cited_refs)
    if n < cfg.min_validation_n:
        logger.debug(
            f"measure_faithfulness n={n} min={cfg.min_validation_n} status=insufficient"
        )
        return FaithfulnessReport(n=n, status=ProbeStatus.INSUFFICIENT)

    refs: list[EvidenceRefFaithfulness] = []
    scored_n = 0
    unfaithful_n = 0
    for cited in cited_refs:
        if not cited.resolved:
            refs.append(
                EvidenceRefFaithfulness(
                    ref=cited.ref,
                    claim=cited.claim,
                    resolved=False,
                    scored=False,
                )
            )
            continue
        if cited.evidence_text is None:
            raise ValueError(f"resolving ref carries no evidence text: {cited.ref!r}")
        result = score_claim(cited.claim, cited.evidence_text, scorer=backend)
        faithful = result.probability >= cfg.entail_threshold
        scored_n += 1
        if not faithful:
            unfaithful_n += 1
        refs.append(
            EvidenceRefFaithfulness(
                ref=cited.ref,
                claim=cited.claim,
                resolved=True,
                scored=True,
                entailment_probability=result.probability,
                faithful=faithful,
            )
        )

    unfaithful_rate = unfaithful_n / scored_n if scored_n > 0 else None
    logger.debug(
        f"measure_faithfulness n={n} status=scored scored={scored_n} "
        f"unfaithful={unfaithful_n}"
    )
    return FaithfulnessReport(
        n=n,
        status=ProbeStatus.SCORED,
        scored_n=scored_n,
        unfaithful_n=unfaithful_n,
        unfaithful_rate=unfaithful_rate,
        refs=tuple(refs),
    )


__all__ = [
    "CitedEvidenceRef",
    "EvidenceRefFaithfulness",
    "FaithfulnessConfig",
    "FaithfulnessReport",
    "GoldLabel",
    "JurorLengthObservation",
    "JurorVerbosityBias",
    "JuryValidationConfig",
    "JuryValidationReport",
    "JuryValidationStatus",
    "LabelSource",
    "LabeledVerdict",
    "ProbeStatus",
    "ValidationCohort",
    "VerbosityBiasConfig",
    "VerbosityBiasReport",
    "build_jury_validation_cohort",
    "measure_faithfulness",
    "measure_verbosity_bias",
    "validate_jury",
]
