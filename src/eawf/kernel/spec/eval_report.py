"""EvalReport — pre-registered build-report metric set.

A *build-report* is the project's self-evaluation of what a phase (or the
whole build) produced: how many waves closed, how much estimated effort
(EU) was delivered, what fraction of success criteria carry evidence, how
many closed waves pin a commit SHA. Those figures eventually ship in a
public write-up (GitHub issue #1). The integrity risk is that a narrative
number drifts from the repo — a "947 waves" claim that no longer recounts
once state moves.

:class:`EvalReport` pins the metric set BEFORE the write-up ships. Each
:class:`EvalMetric` declares (a) a stable ``id`` + human ``label``, (b) the
:class:`MetricSource` the figure derives from, (c) the ``recount_key`` that
names the pure recompute function in the recount tool, and (d) the
pre-registered ``declared_value``. The companion ``tools/recount_build_report.py``
re-derives each metric from repo data and exits nonzero on any figure it
cannot reproduce, so the report's numbers are reproducible from repo data
alone rather than taken on trust.

The model carries no recompute logic itself — it is the typed contract the
recount tool validates against. Keeping the declaration (schema) separate
from the recomputation (tool) means the pre-registered figures live in a
committed, strict, schema-validated document while the recount stays a pure
function of the same repo data.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from eawf.kernel.spec.common import _StrictModel


class MetricSource(StrEnum):
    """Repo-data source a build-report metric is recounted from.

    The source names WHERE the recount tool reads the ground truth, so a
    reviewer can trace each declared figure back to a committed, reproducible
    artifact rather than a narrator's memory.

    - ``STATE`` — derived from ``.ea/state.json`` (the committed source of
      truth for lifecycle entities: waves, phases, success criteria,
      estimates, actuals).
    - ``GIT`` — derived from the git history of the repo (commit counts,
      authorship, tags).
    - ``TELEMETRY`` — derived from the telemetry store (session durations,
      token + cost rollups).
    """

    STATE = "state"
    GIT = "git"
    TELEMETRY = "telemetry"


class MetricUnit(StrEnum):
    """Unit a build-report metric is measured in.

    Pins how the bare ``declared_value`` float is read so a count is never
    silently compared against a ratio.

    - ``COUNT`` — a whole-number tally (waves, phases, criteria, commits).
    - ``EU`` — effort units (one EU is ~30 minutes of agent-driven session
      time; see :data:`eawf.workflow.estimation.buckets.BUCKET_EU`).
    - ``RATIO`` — a dimensionless fraction in ``[0, 1]`` (coverage ratios).
    - ``USD`` — a dollar figure (API-equivalent build cost).
    """

    COUNT = "count"
    EU = "eu"
    RATIO = "ratio"
    USD = "usd"


class EvalMetric(_StrictModel):
    """One pre-registered build-report figure with its recount binding.

    ``recount_key`` is the stable identifier the recount tool dispatches on:
    the tool owns a registry mapping each key to a pure recompute function
    over repo data. A metric whose ``recount_key`` has no registered
    recomputer is, by construction, not reproducible — the tool reports it
    as an unrecounted figure and exits nonzero.

    ``tolerance`` admits float wobble for non-integer metrics (ratios, EU,
    USD); a ``COUNT`` metric carries ``tolerance == 0.0`` because a tally
    either matches exactly or the figure is wrong.
    """

    id: Annotated[str, Field(pattern=r"^M\d+$")]
    label: str = Field(min_length=1, max_length=120)
    source: MetricSource
    unit: MetricUnit
    recount_key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    declared_value: float = Field(ge=0.0)
    tolerance: float = Field(default=0.0, ge=0.0)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _ratio_in_unit_interval(self) -> EvalMetric:
        """Enforce a ``RATIO`` metric's declared value lies in ``[0, 1]``.

        A coverage ratio outside the unit interval is an authoring bug the
        recount can never reproduce, so it fails at the ingestion boundary
        rather than recounting to a guaranteed mismatch later.

        Raises:
            ValueError: when ``unit`` is ``RATIO`` and ``declared_value`` is
                greater than ``1.0``.
        """
        if self.unit is MetricUnit.RATIO and self.declared_value > 1.0:
            raise ValueError(
                f"ratio metric declared_value out of [0, 1]: id={self.id!r} "
                f"declared_value={self.declared_value!r}"
            )
        return self

    @model_validator(mode="after")
    def _count_is_whole(self) -> EvalMetric:
        """Enforce a ``COUNT`` metric's declared value is a whole number.

        A tally with a fractional declared value is an authoring bug — a
        count of waves or criteria is never 12.5 — so it fails at the
        boundary rather than recounting to a near-miss.

        Raises:
            ValueError: when ``unit`` is ``COUNT`` and ``declared_value`` is
                not an integer.
        """
        if self.unit is MetricUnit.COUNT and self.declared_value != int(self.declared_value):
            raise ValueError(
                f"count metric declared_value is not whole: id={self.id!r} "
                f"declared_value={self.declared_value!r}"
            )
        return self


class EvalReport(_StrictModel):
    """Pre-registered build-report metric set.

    The report is promotable iff every metric recounts: the companion
    recount tool re-derives each :class:`EvalMetric` from repo data and the
    report is honest only when no figure drifts. The model enforces the
    structural invariants (non-empty metric list, unique ids + recount keys);
    the recount tool enforces the reproducibility invariant.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["EvalReport"] = "EvalReport"

    report_id: Annotated[str, Field(pattern=r"^EVAL-[A-Z0-9-]+$")]
    title: str = Field(min_length=1, max_length=120)
    metrics: list[EvalMetric] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> EvalReport:
        """Enforce metric ``id`` and ``recount_key`` are unique in the report.

        A duplicate id would make a recount mismatch ambiguous (which row
        failed?) and a duplicate recount_key would double-count one figure,
        so both collisions fail at load time.

        Raises:
            ValueError: when two metrics share an ``id`` or a ``recount_key``.
        """
        ids = [m.id for m in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate metric id in report: report_id={self.report_id!r}")
        keys = [m.recount_key for m in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate recount_key in report: report_id={self.report_id!r}")
        return self


__all__ = ["EvalMetric", "EvalReport", "MetricSource", "MetricUnit"]
