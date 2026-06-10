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


__all__ = [
    "GoldLabel",
    "JuryValidationConfig",
    "JuryValidationReport",
    "JuryValidationStatus",
    "LabelSource",
    "LabeledVerdict",
    "ValidationCohort",
    "build_jury_validation_cohort",
    "validate_jury",
]
