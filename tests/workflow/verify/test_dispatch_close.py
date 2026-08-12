"""Unit tests for the post-execution dispatch verify gate.

Exercises :func:`eawf.workflow.verify.dispatch_close.verify_close_readiness`
against hand-built typed report bodies. The gate is pure — no I/O, no
state mutation — so each test builds a body in memory and inspects the
returned :class:`~eawf.workflow.verify.dispatch_close.VerifyResult`.

The companion runner-side integration that wires the gate into
:func:`eawf.runtime.daemon.dispatch_runner.emit_agent_end_report` is
covered by :mod:`tests.runtime.daemon.test_dispatch_close_gate`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.store.kinds.agent_report import (
    AuditorReportBody,
    ExecutorReportBody,
    ReviewerReportBody,
)
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    FloorFailureClass,
    VerifyResult,
    classify_floor_failure,
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


# ---- W49: rung 4 — evidence_refs required on criteria-bearing waves ---------


def test_rung4_zero_criteria_wave_passes_with_empty_refs() -> None:
    """Boundary: nothing to evidence — rung 4 stays dormant."""
    result = verify_close_readiness(
        "P28-I03-W57",
        _executor_body(),
        typed_criteria_count=0,
        require_evidence_refs=True,
    )
    assert result.passed is True


def test_rung4_criteria_bearing_wave_with_refs_passes() -> None:
    from eawf.kernel.store.kinds.agent_report import AgentReportEvidenceRef

    body = _executor_body().model_copy(
        update={
            "evidence_refs": [
                AgentReportEvidenceRef(
                    kind="artifact",
                    ref="uv run pytest tests/x -q -> exit 0",
                    note="CR-01",
                ),
                AgentReportEvidenceRef(
                    kind="artifact",
                    ref="src/eawf/x.py:42 wires the behaviour",
                    note="CR-02",
                ),
            ]
        }
    )
    result = verify_close_readiness(
        "P28-I03-W57",
        body,
        typed_criteria_count=2,
        require_evidence_refs=True,
    )
    assert result.passed is True


def test_rung4_undercounted_refs_refuse() -> None:
    """W35 tightening: one ref for two typed criteria refuses (per-criterion
    contract, matching the executor DoD)."""
    from eawf.kernel.store.kinds.agent_report import AgentReportEvidenceRef

    body = _executor_body().model_copy(
        update={
            "evidence_refs": [AgentReportEvidenceRef(kind="artifact", ref="only one", note="CR-01")]
        }
    )
    result = verify_close_readiness(
        "P28-I03-W57",
        body,
        typed_criteria_count=2,
        require_evidence_refs=True,
    )
    assert result.passed is False
    assert any("one per criterion" in reason for reason in result.reasons)


def test_rung4_criteria_bearing_wave_without_refs_refuses() -> None:
    result = verify_close_readiness(
        "P28-I03-W57",
        _executor_body(),
        typed_criteria_count=2,
        require_evidence_refs=True,
    )
    assert result.passed is False
    assert any("evidence_refs" in reason for reason in result.reasons)


def test_rung4_off_without_teeth_bit() -> None:
    """Advisory repos and legacy callers are unchanged."""
    result = verify_close_readiness(
        "P28-I03-W57",
        _executor_body(),
        typed_criteria_count=2,
        require_evidence_refs=False,
    )
    assert result.passed is True


# ---- Floor-failure classifier (R2b, P30-I25-W04) ----------------------------
#
# The classifier decides whether a refused deterministic close-gate is an
# ENVIRONMENTAL gap the executor cannot fix in-scope (a bare smoke repo missing
# scaffolding) or an EXECUTOR_FIXABLE refusal (a real lint / test / assertion
# failure the repair ladder should re-dispatch on).

_PRECOMMIT_DETAIL = "argv=['uv', 'run', 'pre-commit', 'run', '--all-files'] returncode=1"
_MYPY_DETAIL = "argv=['uv', 'run', 'mypy', 'src/'] returncode=1"
_PYTEST_DETAIL = "argv=['uv', 'run', 'pytest', '-q'] returncode=1"


def _scaffold_full_repo(repo_root: Path) -> None:
    """Write the floor scaffolding a real python repo carries.

    A repo with a pre-commit config, a package dir, and a pytest dependency
    declared has no MISSING scaffolding, so a floor-gate refusal against it is a
    genuine executor-fixable failure (lint / type / test), not an environmental
    gap.
    """
    (repo_root / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )


def test_classify_missing_precommit_config_is_environmental(tmp_path: Path) -> None:
    """A pre-commit floor refusal in a repo with no config is environmental."""
    # Bare smoke repo: no .pre-commit-config.yaml the executor could scaffold.
    result = classify_floor_failure(failing_detail=_PRECOMMIT_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.ENVIRONMENTAL


def test_classify_missing_package_dir_is_environmental(tmp_path: Path) -> None:
    """A mypy floor refusal against an absent package dir is environmental."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    # No src/ dir for `mypy src/` to type-check.
    result = classify_floor_failure(failing_detail=_MYPY_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.ENVIRONMENTAL


def test_classify_missing_pytest_dependency_is_environmental(tmp_path: Path) -> None:
    """A pytest floor refusal with no declared pytest dependency is environmental."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    # pyproject.toml is absent, so pytest is not a declared test dependency.
    result = classify_floor_failure(failing_detail=_PYTEST_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.ENVIRONMENTAL


def test_classify_pytest_declared_but_test_fails_is_executor_fixable(tmp_path: Path) -> None:
    """A pytest refusal in a scaffolded repo is a real test failure the agent can fix."""
    _scaffold_full_repo(tmp_path)
    result = classify_floor_failure(failing_detail=_PYTEST_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.EXECUTOR_FIXABLE


def test_classify_lint_error_in_changed_code_is_executor_fixable(tmp_path: Path) -> None:
    """A pre-commit refusal with the config present is a real lint error the agent can fix."""
    _scaffold_full_repo(tmp_path)
    result = classify_floor_failure(failing_detail=_PRECOMMIT_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.EXECUTOR_FIXABLE


def test_classify_mypy_error_with_package_dir_is_executor_fixable(tmp_path: Path) -> None:
    """A mypy refusal against an EXISTING package dir is a real type error the agent can fix."""
    _scaffold_full_repo(tmp_path)
    result = classify_floor_failure(failing_detail=_MYPY_DETAIL, repo_root=tmp_path)
    assert result is FloorFailureClass.EXECUTOR_FIXABLE


def test_classify_wave_specific_command_gate_is_executor_fixable(tmp_path: Path) -> None:
    """A non-floor command gate refusal is executor-fixable regardless of scaffolding.

    A wave's own ``command_exit_zero`` gate (not one of the pre-commit / mypy /
    pytest floor commands) checks code the agent wrote, so its refusal never
    classifies environmental even in a bare repo.
    """
    detail = "argv=['python', 'scripts/check_invariant.py'] returncode=1"
    result = classify_floor_failure(failing_detail=detail, repo_root=tmp_path)
    assert result is FloorFailureClass.EXECUTOR_FIXABLE


def test_classify_unrecognized_detail_is_executor_fixable(tmp_path: Path) -> None:
    """A detail with no parseable argv (a timeout) keeps the repair ladder -- boundary.

    An unrecognized falsifier must not silently close-with-followups: the safe
    default is executor-fixable so the repair ladder still fires.
    """
    result = classify_floor_failure(
        failing_detail="timeout after 300s (class='standard')", repo_root=tmp_path
    )
    assert result is FloorFailureClass.EXECUTOR_FIXABLE


def test_classify_empty_detail_is_executor_fixable(tmp_path: Path) -> None:
    """An empty falsifier is the min-length boundary -- executor-fixable default."""
    result = classify_floor_failure(failing_detail="", repo_root=tmp_path)
    assert result is FloorFailureClass.EXECUTOR_FIXABLE
