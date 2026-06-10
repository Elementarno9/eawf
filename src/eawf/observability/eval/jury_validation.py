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
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.models import State
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.paths import store_dir
from eawf.observability.eval.reputation import VerdictOutcome, build_verdict_outcomes

logger = logging.getLogger(__name__)

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


__all__ = [
    "GoldLabel",
    "LabelSource",
    "LabeledVerdict",
    "ValidationCohort",
    "build_jury_validation_cohort",
]
