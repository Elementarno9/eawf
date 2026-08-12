"""Min-N-gated self-eval surface tests.

The self-eval surface is an *honest-negative* dashboard: below a hard
minimum cohort size it refuses to score rather than emit a Goodhartable
``0%`` / ``100%`` / ``NaN`` pass rate. These tests pin both layers:

- the pure :func:`summarize_self_eval` reducer across the cohort-size
  boundaries (empty, below N, exactly N, above N) plus its error path; and
- the store-reading :func:`compute_self_eval` entry, which must surface the
  same honest-negative refusal when the on-disk verdict store is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.eval import (
    MIN_SELF_EVAL_N,
    SelfEvalStatus,
    compute_self_eval,
    summarize_self_eval,
)


def _verdicts(*, passing: int, failing: int) -> tuple[AgentReportVerdict, ...]:
    """Build a verdict cohort with *passing* passes and *failing* fails."""
    return (
        *(AgentReportVerdict.PASS for _ in range(passing)),
        *(AgentReportVerdict.FAIL for _ in range(failing)),
    )


def _write_executor_report(state_path: Path, *, index: int, verdict: AgentReportVerdict) -> None:
    """Append one executor-report envelope to the on-disk store."""
    role = AgentSessionRole.EXECUTOR
    base_id = f"P00-W{index:02d}"
    report_id = report_record_id(role=role, base_id=base_id, attempt=1)
    moment = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    body = ExecutorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="recorded executor verdict",
        wave_id=base_id,
        outcome="implementation outcome",
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{index:02d}",
        scope_id=base_id,
        base_id=base_id,
        attempt=1,
        runtime="claude",
        generated_at=moment,
        summary="recorded executor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded executor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


# --- summarize_self_eval: the refuse-to-score gate ------------------------


def test_summarize_self_eval_empty_cohort_refuses_to_score() -> None:
    """An empty cohort yields INSUFFICIENT_DATA with pass_rate=None, not 0%/NaN."""
    surface = summarize_self_eval(())

    assert surface.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert surface.cohort_size == 0
    assert surface.pass_rate is None
    assert surface.verdict_breakdown == {}
    assert "insufficient data" in surface.note
    assert f"N={MIN_SELF_EVAL_N}" in surface.note


def test_summarize_self_eval_below_min_n_refuses_to_score() -> None:
    """A cohort one short of the gate still refuses — no fake number."""
    cohort = _verdicts(passing=MIN_SELF_EVAL_N - 1, failing=0)
    surface = summarize_self_eval(cohort)

    assert surface.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert surface.cohort_size == MIN_SELF_EVAL_N - 1
    assert surface.pass_rate is None
    assert surface.verdict_breakdown == {"pass": MIN_SELF_EVAL_N - 1}


def test_summarize_self_eval_exactly_min_n_scores() -> None:
    """A cohort exactly at the gate clears it and reports a real pass rate."""
    cohort = _verdicts(passing=MIN_SELF_EVAL_N, failing=0)
    surface = summarize_self_eval(cohort)

    assert surface.status is SelfEvalStatus.SCORED
    assert surface.cohort_size == MIN_SELF_EVAL_N
    assert surface.pass_rate == pytest.approx(1.0)
    assert surface.verdict_breakdown == {"pass": MIN_SELF_EVAL_N}


def test_summarize_self_eval_above_min_n_scores_mixed_cohort() -> None:
    """Above the gate, the pass rate counts pass + pass-with-followups."""
    cohort = (
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS_WITH_FOLLOWUPS,
        AgentReportVerdict.FAIL,
        AgentReportVerdict.FAIL,
        AgentReportVerdict.BLOCKED,
    )
    surface = summarize_self_eval(cohort)

    assert surface.status is SelfEvalStatus.SCORED
    assert surface.cohort_size == 6
    # 3 passing (pass + pass + pass-with-followups) over 6.
    assert surface.pass_rate == pytest.approx(0.5)
    assert surface.verdict_breakdown == {
        "pass": 2,
        "pass-with-followups": 1,
        "fail": 2,
        "blocked": 1,
    }


def test_summarize_self_eval_all_failing_above_min_n_scores_zero() -> None:
    """An all-fail cohort above the gate scores a real 0.0 — not a refusal.

    The honest-negative refusal is reserved for *insufficient data*; a fully
    populated all-fail cohort is a real, defensible ``0%``.
    """
    cohort = _verdicts(passing=0, failing=MIN_SELF_EVAL_N + 1)
    surface = summarize_self_eval(cohort)

    assert surface.status is SelfEvalStatus.SCORED
    assert surface.pass_rate == pytest.approx(0.0)


def test_summarize_self_eval_respects_custom_min_n() -> None:
    """The gate is parameterised; a higher min_n re-refuses a small cohort."""
    cohort = _verdicts(passing=MIN_SELF_EVAL_N, failing=0)
    surface = summarize_self_eval(cohort, min_n=MIN_SELF_EVAL_N + 1)

    assert surface.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert surface.min_n == MIN_SELF_EVAL_N + 1
    assert surface.pass_rate is None


def test_summarize_self_eval_rejects_non_positive_min_n() -> None:
    """A zero/negative gate would defeat the refuse-to-score guarantee."""
    with pytest.raises(ValueError, match="min_n must be >= 1"):
        summarize_self_eval((), min_n=0)


# --- compute_self_eval: store-reading entry -------------------------------


def test_compute_self_eval_empty_store_refuses_to_score(tmp_path: Path) -> None:
    """An empty verdict store yields the honest-negative surface, not 0%/NaN.

    This is the load-bearing criterion: the store starts empty, so the
    dashboard must refuse rather than fabricate a number.
    """
    state_path = tmp_path / "state.json"
    surface = compute_self_eval(state_path)

    assert surface.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert surface.cohort_size == 0
    assert surface.pass_rate is None


def test_compute_self_eval_below_min_n_store_refuses(tmp_path: Path) -> None:
    """A store with fewer than N reports still refuses to score."""
    state_path = tmp_path / "state.json"
    for index in range(MIN_SELF_EVAL_N - 1):
        _write_executor_report(state_path, index=index, verdict=AgentReportVerdict.PASS)

    surface = compute_self_eval(state_path)

    assert surface.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert surface.cohort_size == MIN_SELF_EVAL_N - 1
    assert surface.pass_rate is None


def test_compute_self_eval_exactly_min_n_store_scores(tmp_path: Path) -> None:
    """A store with exactly N reports clears the gate and scores."""
    state_path = tmp_path / "state.json"
    for index in range(MIN_SELF_EVAL_N):
        _write_executor_report(state_path, index=index, verdict=AgentReportVerdict.PASS)

    surface = compute_self_eval(state_path)

    assert surface.status is SelfEvalStatus.SCORED
    assert surface.cohort_size == MIN_SELF_EVAL_N
    assert surface.pass_rate == pytest.approx(1.0)


def test_compute_self_eval_above_min_n_store_scores_mixed(tmp_path: Path) -> None:
    """A store above N scores a real pass rate over the persisted verdicts."""
    state_path = tmp_path / "state.json"
    verdicts = (
        *(AgentReportVerdict.PASS for _ in range(MIN_SELF_EVAL_N)),
        AgentReportVerdict.FAIL,
    )
    for index, verdict in enumerate(verdicts):
        _write_executor_report(state_path, index=index, verdict=verdict)

    surface = compute_self_eval(state_path)

    assert surface.status is SelfEvalStatus.SCORED
    assert surface.cohort_size == MIN_SELF_EVAL_N + 1
    assert surface.pass_rate == pytest.approx(MIN_SELF_EVAL_N / (MIN_SELF_EVAL_N + 1))
    assert surface.verdict_breakdown == {"pass": MIN_SELF_EVAL_N, "fail": 1}
