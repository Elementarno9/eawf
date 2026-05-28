"""Unit tests for the post-execution dispatch verify gate (P28-I03-W57).

Exercises :func:`eawf.workflow.verify.dispatch_close.verify_close_readiness`
against hand-built typed report bodies. The gate is pure — no I/O, no
state mutation — so each test builds a body in memory and inspects the
returned :class:`~eawf.workflow.verify.dispatch_close.VerifyResult`.

The companion runner-side integration that wires the gate into
:func:`eawf.runtime.daemon.dispatch_runner.emit_agent_end_report` is
covered by :mod:`tests.runtime.daemon.test_dispatch_close_gate`.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.store.kinds.agent_report import (
    AuditorReportBody,
    ExecutorReportBody,
    ReviewerReportBody,
)
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    VerifyResult,
    verify_close_readiness,
)


def _executor_body(
    *,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    summary: str = "implemented W57 verify gate",
    wave_id: str = "P28-I03-W57",
) -> ExecutorReportBody:
    """Return a minimal :class:`ExecutorReportBody` with optional overrides."""
    return ExecutorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary=summary,
        wave_id=wave_id,
        files_changed=["src/eawf/workflow/verify/dispatch_close.py"],
        tests_run=["uv run pytest tests/workflow/verify -q"],
        commit_sha="abcdef1",
        outcome=summary,
    )


# ---- Pass cases -------------------------------------------------------------


def test_pass_verdict_on_executor_body_passes() -> None:
    """A clean PASS verdict on a matching wave id passes the gate."""
    body = _executor_body()
    result = verify_close_readiness("P28-I03-W57", body)
    assert isinstance(result, VerifyResult)
    assert result.passed is True
    assert result.verdict is AgentReportVerdict.PASS
    assert result.reasons == ()


def test_pass_with_followups_verdict_passes() -> None:
    """A ``pass-with-followups`` verdict is still close-ready."""
    body = _executor_body(verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is True
    assert result.verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS


def test_pass_for_auditor_body_does_not_check_wave_id() -> None:
    """Non-executor bodies skip the wave-id mismatch check (no field to compare)."""
    body = AuditorReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="audit passed",
        target_id="P28-I03-W57",
    )
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is True


def test_pass_for_reviewer_body_passes_on_clean_verdict() -> None:
    """Reviewer bodies follow the same gate rules — verdict + summary only."""
    body = ReviewerReportBody(
        verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
        confidence=Confidence.HIGH,
        summary="reviewed clean",
        target_id="P28-I03-W57",
    )
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is True


# ---- Fail cases -------------------------------------------------------------


def test_fail_verdict_blocks_close() -> None:
    """A FAIL verdict refuses close with a precise reason."""
    body = _executor_body(verdict=AgentReportVerdict.FAIL)
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is False
    assert result.verdict is AgentReportVerdict.FAIL
    assert any("not in close-ready set" in reason for reason in result.reasons)


def test_blocked_verdict_blocks_close() -> None:
    """A BLOCKED verdict refuses close."""
    body = _executor_body(verdict=AgentReportVerdict.BLOCKED)
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is False
    assert result.verdict is AgentReportVerdict.BLOCKED


def test_executor_body_wave_id_mismatch_blocks() -> None:
    """An executor body whose wave id disagrees with dispatched wave blocks."""
    body = _executor_body(wave_id="P28-I03-W56")
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is False
    assert any("disagrees" in reason for reason in result.reasons)


def test_blank_summary_blocks_close() -> None:
    """A whitespace-only summary refuses close even on a PASS verdict."""
    body = _executor_body(summary="   ")
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is False
    assert any("summary is blank" in reason for reason in result.reasons)


def test_multiple_failures_surface_in_one_result() -> None:
    """Every failing check appears in :attr:`VerifyResult.reasons`."""
    body = _executor_body(
        verdict=AgentReportVerdict.FAIL,
        summary=" ",
        wave_id="P28-I03-W56",
    )
    result = verify_close_readiness("P28-I03-W57", body)
    assert result.passed is False
    assert len(result.reasons) == 3


# ---- DispatchCloseBlockedError ----------------------------------------------


def test_dispatch_close_blocked_error_carries_structured_result() -> None:
    """The error carries the :class:`VerifyResult` for downstream inspection."""
    result = VerifyResult(
        passed=False,
        verdict=AgentReportVerdict.FAIL,
        reasons=("verdict=fail not in close-ready set",),
    )
    error = DispatchCloseBlockedError(wave_id="P28-I03-W57", result=result)
    assert error.wave_id == "P28-I03-W57"
    assert error.result is result
    message = str(error)
    assert "P28-I03-W57" in message
    assert "verdict=fail" in message


def test_dispatch_close_blocked_error_message_handles_empty_reasons() -> None:
    """A result with no reasons still produces a readable message."""
    result = VerifyResult(
        passed=False,
        verdict=AgentReportVerdict.BLOCKED,
        reasons=(),
    )
    error = DispatchCloseBlockedError(wave_id="P28-I03-W57", result=result)
    assert "no reasons recorded" in str(error)


# ---- Purity ----------------------------------------------------------------


def test_verify_close_readiness_is_pure_no_body_mutation() -> None:
    """Calling the gate twice yields equal results — the body is untouched."""
    body = _executor_body()
    first = verify_close_readiness("P28-I03-W57", body)
    second = verify_close_readiness("P28-I03-W57", body)
    assert first == second
    # Body remains validatable + equal after the gate calls.
    assert body.verdict is AgentReportVerdict.PASS
    assert body.wave_id == "P28-I03-W57"


def test_verify_result_is_immutable() -> None:
    """:class:`VerifyResult` is a frozen dataclass — attribute writes raise."""
    result = VerifyResult(
        passed=True,
        verdict=AgentReportVerdict.PASS,
        reasons=(),
    )
    with pytest.raises(AttributeError):
        result.passed = False  # type: ignore[misc]
