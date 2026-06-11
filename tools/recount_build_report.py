"""Recount a pre-registered build-report metric set from repo data alone.

A build-report (GitHub issue #1) cites figures about what the build
produced — closed-wave count, EU delivered, evidence-coverage ratio. The
risk is a narrated number drifting from the repo. This tool reads a typed
:class:`~eawf.kernel.spec.eval_report.EvalReport` (the PRE-REGISTERED metric
declarations) and, for each metric, RE-DERIVES the figure from
``.ea/state.json`` — the committed source of truth — then compares the
recount to the declared value.

The tool exits ``0`` only when EVERY metric reproduces within its declared
tolerance. It exits ``1`` and names every figure it could not reproduce:
either the recount disagreed with the declared value, or the metric's
``recount_key`` has no registered recomputer (an unrecountable figure).

Each recompute function is a pure function of the parsed ``state.json``
dict, so the recount is reproducible by anyone with the repo and nothing
else — no daemon, no network, no telemetry side channel.

Invocation::

    python3 tools/recount_build_report.py <report.json> [--state .ea/state.json]

Exit codes:

- ``0`` — every metric recounted within tolerance.
- ``1`` — at least one metric did not reproduce (named on stderr), the
  report failed schema validation, or a path argument was unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.spec.eval_report import EvalMetric, EvalReport

#: Code-canonical bucket -> EU table, mirrored from
#: :data:`eawf.workflow.estimation.buckets.BUCKET_EU`. Inlined (rather than
#: imported) so the recount stays a pure function of ``state.json`` with no
#: dependency on the estimation runtime; the canonical table is pinned by its
#: own test, so a drift between the two reds a test rather than skewing a
#: recount silently.
_BUCKET_EU: dict[str, float] = {
    "XS": 0.25,
    "S": 0.5,
    "M": 1.0,
    "L": 2.0,
    "XL": 3.5,
}

#: Evidence-kind values + criterion fields that count a success criterion as
#: carrying evidence for the coverage recount. A criterion is "covered" when it
#: cites a gate, attests an evidence kind, or records a waiver reason.
_EVIDENCED_KINDS: frozenset[str] = frozenset({"attested", "gate", "claim", "decision"})


def _closed_waves(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the closed-wave rows of *state*.

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        Every wave whose ``status`` is ``"closed"``, in mapping iteration
        order.
    """
    waves = state.get("waves", {})
    return [w for w in waves.values() if w.get("status") == "closed"]


def recount_closed_wave_count(state: Mapping[str, Any]) -> float:
    """Recount the number of closed waves.

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        The count of waves with ``status == "closed"``.
    """
    return float(len(_closed_waves(state)))


def recount_eu_delivered(state: Mapping[str, Any]) -> float:
    """Recount the EU delivered across closed waves.

    Each closed wave contributes its effort-bucket centroid EU
    (:data:`_BUCKET_EU`); a wave with no bucket contributes ``0``.

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        The summed bucket EU over every closed wave.
    """
    return float(sum(_BUCKET_EU.get(w.get("effort_bucket", ""), 0.0) for w in _closed_waves(state)))


def recount_phases_closed(state: Mapping[str, Any]) -> float:
    """Recount the number of closed phases.

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        The count of phases with ``status == "closed"``.
    """
    phases = state.get("phases", {})
    return float(sum(1 for p in phases.values() if p.get("status") == "closed"))


def _criterion_has_evidence(criterion: Mapping[str, Any]) -> bool:
    """Return whether a success criterion carries evidence.

    A criterion is evidenced when it cites at least one gate, attests an
    evidence kind in :data:`_EVIDENCED_KINDS`, or records a waiver reason.

    Args:
        criterion: One ``success_criteria`` row from a closed wave.

    Returns:
        ``True`` when the criterion carries evidence, else ``False``.
    """
    if criterion.get("gate_ids"):
        return True
    if criterion.get("evidence_kind") in _EVIDENCED_KINDS:
        return True
    return bool(criterion.get("waiver_reason"))


def recount_evidence_coverage(state: Mapping[str, Any]) -> float:
    """Recount the evidence-coverage ratio over closed-wave criteria.

    The ratio is (criteria carrying evidence) / (total criteria) across every
    closed wave's ``success_criteria``. Returns ``0`` when no closed wave has
    any criterion (an empty corpus has no coverage to claim).

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        The evidence-coverage ratio in ``[0, 1]``.
    """
    total = 0
    evidenced = 0
    for wave in _closed_waves(state):
        for criterion in wave.get("success_criteria") or []:
            total += 1
            if _criterion_has_evidence(criterion):
                evidenced += 1
    if total == 0:
        return 0.0
    return evidenced / total


def recount_commit_pinned_waves(state: Mapping[str, Any]) -> float:
    """Recount closed waves that pin a commit SHA.

    Args:
        state: The parsed ``state.json`` mapping.

    Returns:
        The count of closed waves whose ``commit`` field is set.
    """
    return float(sum(1 for w in _closed_waves(state) if w.get("commit")))


#: Registry mapping each metric ``recount_key`` to its pure recompute function
#: over the parsed ``state.json`` dict. A metric whose key is absent here is
#: unrecountable by construction — :func:`recount_report` reports it as such and
#: the tool exits nonzero, so an undeclared figure can never silently pass.
RECOUNTERS: dict[str, Callable[[Mapping[str, Any]], float]] = {
    "closed_wave_count": recount_closed_wave_count,
    "eu_delivered": recount_eu_delivered,
    "phases_closed": recount_phases_closed,
    "evidence_coverage": recount_evidence_coverage,
    "commit_pinned_waves": recount_commit_pinned_waves,
}


@dataclass(frozen=True)
class MetricRecount:
    """The recount outcome for one metric.

    ``reproduced`` is ``True`` only when a recomputer ran AND its result
    matched the declared value within tolerance. ``recounted_value`` is
    ``None`` when no recomputer was registered for the metric's key (an
    unrecountable figure).
    """

    metric_id: str
    recount_key: str
    declared_value: float
    recounted_value: float | None
    reproduced: bool
    reason: str


def recount_metric(metric: EvalMetric, state: Mapping[str, Any]) -> MetricRecount:
    """Recount one metric against repo data.

    Dispatches on ``metric.recount_key`` into :data:`RECOUNTERS`. A metric
    whose key has no registered recomputer is reported as unreproducible
    (``recounted_value`` is ``None``). When a recomputer runs, the metric
    reproduces iff ``|recounted - declared| <= tolerance``.

    Args:
        metric: The pre-registered metric declaration.
        state: The parsed ``state.json`` mapping.

    Returns:
        A :class:`MetricRecount` describing the outcome.
    """
    recounter = RECOUNTERS.get(metric.recount_key)
    if recounter is None:
        return MetricRecount(
            metric_id=metric.id,
            recount_key=metric.recount_key,
            declared_value=metric.declared_value,
            recounted_value=None,
            reproduced=False,
            reason=f"no recomputer registered for recount_key={metric.recount_key!r}",
        )
    recounted = recounter(state)
    delta = abs(recounted - metric.declared_value)
    reproduced = delta <= metric.tolerance
    if reproduced:
        reason = "ok"
    else:
        reason = (
            f"declared={metric.declared_value} recounted={recounted} "
            f"delta={delta} tolerance={metric.tolerance}"
        )
    return MetricRecount(
        metric_id=metric.id,
        recount_key=metric.recount_key,
        declared_value=metric.declared_value,
        recounted_value=recounted,
        reproduced=reproduced,
        reason=reason,
    )


def recount_report(report: EvalReport, state: Mapping[str, Any]) -> list[MetricRecount]:
    """Recount every metric in *report* against *state*.

    Args:
        report: The pre-registered build-report metric set.
        state: The parsed ``state.json`` mapping.

    Returns:
        One :class:`MetricRecount` per metric, in report order.
    """
    return [recount_metric(metric, state) for metric in report.metrics]


def _load_report(path: Path) -> EvalReport:
    """Load and validate an EvalReport from a JSON file.

    Args:
        path: Filesystem path to the report JSON.

    Returns:
        The validated :class:`EvalReport`.

    Raises:
        ValidationError: when the JSON does not satisfy the EvalReport schema.
        json.JSONDecodeError: when the file is not valid JSON.
        OSError: when the file cannot be read.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return EvalReport.model_validate(raw)


def main(argv: list[str] | None = None) -> int:
    """Recount a build-report metric set and map the outcome onto an exit code.

    Args:
        argv: CLI arguments (excluding the program name). Defaults to
            ``sys.argv[1:]`` when ``None``.

    Returns:
        ``0`` when every metric reproduced; ``1`` when the report failed to
        load/validate, a path was unreadable, or at least one metric did not
        reproduce.
    """
    parser = argparse.ArgumentParser(
        prog="recount_build_report.py",
        description="Recount a pre-registered build-report metric set from repo data.",
    )
    parser.add_argument("report", type=Path, help="path to the EvalReport JSON")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(".ea/state.json"),
        help="path to state.json (default: .ea/state.json)",
    )
    args = parser.parse_args(argv)

    try:
        report = _load_report(args.report)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"recount_build_report: cannot read report: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"recount_build_report: invalid EvalReport: {exc}", file=sys.stderr)
        return 1

    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"recount_build_report: cannot read state: {exc}", file=sys.stderr)
        return 1

    results = recount_report(report, state)
    failures = [r for r in results if not r.reproduced]

    if not failures:
        print(f"recount_build_report: all {len(results)} metric(s) reproduced")
        return 0

    print(
        f"recount_build_report: {len(failures)} of {len(results)} metric(s) did not reproduce:",
        file=sys.stderr,
    )
    for result in failures:
        print(f"  {result.metric_id} ({result.recount_key}): {result.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
