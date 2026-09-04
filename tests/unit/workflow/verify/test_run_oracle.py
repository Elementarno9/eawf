"""Tests for :func:`run_oracle` (FS05 ordered-escalation runner).

Covers the typed criteria CR-1..CR-3 of the FS05 spec plus the boundary
and error paths the runner must honour:

* CR-1 (returns, T4_CONTRACT): a criterion with a passing T1 gate returns
  ``tier=T1_STATIC`` and NEVER consults the jury -- a mocked
  ``convene_cross_vendor_jury`` that fails the test if called proves the
  deterministic-before-jury invariant.
* CR-2 (returns, T4_CONTRACT): a criterion with no deterministic gate and a
  ``verdict_requirement`` other than ``"always"`` falls through to the sync
  single-auditor :func:`verify_wave_verdict_gate`, not the async jury.
* CR-3 (emits, T2_STRUCTURAL): the runner tries gates in ascending tier
  order -- a spy over ``compile_gate`` records the gate ids it saw and the
  order is asserted to be tier-ascending.

Boundary: a zero-gate criterion routes straight to the verdict / jury
tier; a single passing-T1-gate criterion short-circuits at T1; a required
blocking deterministic gate that returns ``fail`` blocks before jury
fallthrough. Error-path: a gate that raises is caught and skipped (it never
aborts the escalation), and when only a raising gate exists the runner falls
through to the verdict tier rather than propagating.

Every external seam (``compile_gate``, ``run_checks``,
``verify_wave_verdict_gate``, ``convene_cross_vendor_jury``,
``verdict_requirement``) is monkeypatched on the :mod:`oracle` module so
each branch is driven deterministically with no live jury, no subprocess,
and no network.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, OracleTier
from eawf.kernel.state.models import Wave
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.verify import oracle
from eawf.workflow.verify.oracle import OracleResult, run_oracle

_T0 = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures: minimal criterion / gate / wave builders + a forbidden-jury spy.
# --------------------------------------------------------------------------- #


def _criterion(
    *, evidence_kind: str = "deterministic", gate_ids: list[str] | None = None
) -> CriterionSpec:
    """Build a minimal valid :class:`CriterionSpec` for runner tests."""
    return CriterionSpec(
        id="CR-01",
        text="the runner scores this criterion",
        kind="contract",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        gate_ids=gate_ids or [],
        quality_dimension="functional_suitability",  # type: ignore[arg-type]
        measurable_signal="a measurable signal of at least twenty characters",
    )


def _gate(gate_id: str, kind: str) -> GateSpec:
    """Build a minimal valid :class:`GateSpec` of the given kind.

    Argv-bearing kinds (``command_exit_zero``) carry an allowlisted
    ``argv`` so the GateSpec L0-policy validator accepts the row.
    """
    args: dict[str, Any] = {}
    if kind == "command_exit_zero":
        args = {"argv": ["pytest", "-q"]}
    return GateSpec(
        id=gate_id,
        criterion_id="CR-01",
        kind=kind,
        args=args,
        policy="block",
        cadence="every-wave",
    )


def _wave() -> Wave:
    """Build a minimal valid :class:`Wave` (empty success_criteria)."""
    return Wave(
        id="P29-I11-W01",
        iter_id="P29-I11",
        title="add run_oracle ordered-escalation runner",
        status="claimed",  # type: ignore[arg-type]
        opened_at=_T0,
    )


def _check_result(*, status: str, name: str = "gate") -> CheckResult:
    """Build a :class:`CheckResult` with the given status."""
    return CheckResult(
        name=name,
        kind="file_exists",
        passed=status == "pass",
        status=status,  # type: ignore[arg-type]
        details=f"check {status}",
    )


def _forbidden_jury(*_args: Any, **_kwargs: Any) -> Any:
    """A jury stand-in that fails the test the moment it is awaited."""

    async def _boom() -> Any:
        raise AssertionError(
            "convene_cross_vendor_jury must not be called when a deterministic gate passes"
        )

    return _boom()


def _spawn_factory_stub(_runtime: str) -> Any:
    """A spawn factory placeholder; never invoked in these mocked tests."""
    raise AssertionError("spawn factory must not be invoked in mocked runner tests")


def _run(criterion: CriterionSpec, gates: list[GateSpec], *, repo_root: Path) -> OracleResult:
    """Drive :func:`run_oracle` over fake on-disk paths and return the result."""
    return asyncio.run(
        run_oracle(
            criterion,
            gates,
            wave=_wave(),
            state=object(),  # type: ignore[arg-type] - jury seam is mocked; state is unused
            state_path=repo_root / "state.json",
            events_path=repo_root / "event.jsonl",
            repo_root=repo_root,
            spawn_factory=_spawn_factory_stub,
        )
    )


# --------------------------------------------------------------------------- #
# CR-1: a passing T1 gate returns at T1 and never consults the jury.
# --------------------------------------------------------------------------- #


def test_run_oracle_passing_t1_gate_returns_t1_without_jury(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A passing T1 gate short-circuits at T1_STATIC; the jury is never called."""
    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda gate, *, criterion: CheckSpec(kind="file_exists", name=gate.id),
    )
    monkeypatch.setattr(
        oracle, "run_checks", lambda specs, *, cwd=None: [_check_result(status="pass")]
    )
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _forbidden_jury)
    monkeypatch.setattr(
        oracle,
        "verify_wave_verdict_gate",
        lambda *a, **k: pytest.fail("single-auditor gate must not run when a T1 gate passes"),
    )

    result = _run(_criterion(), [_gate("G-1", "file_exists")], repo_root=tmp_path)

    assert result.tier is OracleTier.T1_STATIC
    assert result.status == "pass"
    assert result.gate_id == "G-1"
    assert result.criterion_id == "CR-01"


def test_run_oracle_failing_required_gate_returns_fail_without_jury(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A required/blocking deterministic gate that fails returns FAIL before jury."""
    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda gate, *, criterion: CheckSpec(kind="file_exists", name=gate.id),
    )
    monkeypatch.setattr(
        oracle, "run_checks", lambda specs, *, cwd=None: [_check_result(status="fail")]
    )
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _forbidden_jury)
    monkeypatch.setattr(
        oracle,
        "verify_wave_verdict_gate",
        lambda *a, **k: pytest.fail("single-auditor gate must not run after a T1 fail"),
    )

    result = _run(_criterion(), [_gate("G-1", "file_exists")], repo_root=tmp_path)

    assert result.tier is OracleTier.T1_STATIC
    assert result.status == "fail"
    assert result.gate_id == "G-1"
    assert result.detail == "check fail"


# --------------------------------------------------------------------------- #
# W05 (CR-01): the deterministic tier runs off the daemon event loop. A slow
# deterministic gate is offloaded via asyncio.to_thread, so a concurrent task
# on the same loop makes progress WHILE the gate runs -- the runner never
# starves the loop with an inline subprocess call.
# --------------------------------------------------------------------------- #


def test_run_oracle_slow_deterministic_gate_does_not_starve_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-01: run_checks runs via asyncio.to_thread, keeping the loop responsive.

    ``run_checks`` is monkeypatched to a synchronous blocker that parks its
    caller until a concurrent driver releases it. If ``run_oracle`` called it
    inline on the event loop, the blocking call would freeze the loop and the
    concurrent driver could not advance until the gate returned. Because the
    call is offloaded to a worker thread, the driver records its own progress
    (``ticker_run``) BEFORE the gate returns (``gate_return``). The ordering
    assertion is the load-bearing off-loop signal; the bounded ``release`` wait
    keeps a regression (an inline call) from hanging CI -- it deadlocks the loop
    until the timeout, then the ordering assertion fails.
    """
    order: list[str] = []
    gate_entered = threading.Event()
    release = threading.Event()

    def _blocking_run(specs: list[CheckSpec], *, cwd: Path | None = None) -> list[CheckResult]:
        order.append("gate_enter")
        gate_entered.set()
        # Parks the CALLER: a worker thread when offloaded (loop stays free), the
        # loop thread itself when run inline (loop frozen). Bounded so a
        # regression fails the ordering assertion instead of hanging forever.
        release.wait(timeout=2.0)
        order.append("gate_return")
        return [_check_result(status="pass")]

    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda gate, *, criterion: CheckSpec(kind="file_exists", name=gate.id),
    )
    monkeypatch.setattr(oracle, "run_checks", _blocking_run)
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _forbidden_jury)

    async def _drive() -> OracleResult:
        task = asyncio.create_task(
            run_oracle(
                _criterion(),
                [_gate("G-1", "file_exists")],
                wave=_wave(),
                state=object(),  # type: ignore[arg-type] - jury seam is mocked; state is unused
                state_path=tmp_path / "state.json",
                events_path=tmp_path / "event.jsonl",
                repo_root=tmp_path,
                spawn_factory=_spawn_factory_stub,
            )
        )
        # Reaching this poll loop while the gate is running proves the event loop
        # is not frozen -- the gate blocks its worker thread, not the loop.
        while not gate_entered.is_set():
            await asyncio.sleep(0.001)
        order.append("ticker_run")
        release.set()
        return await task

    result = asyncio.run(_drive())

    assert order.index("ticker_run") < order.index("gate_return")
    assert result.tier is OracleTier.T1_STATIC
    assert result.status == "pass"
    assert result.gate_id == "G-1"


# --------------------------------------------------------------------------- #
# CR-2: no deterministic gate + requirement != "always" -> single-auditor.
# --------------------------------------------------------------------------- #


def test_run_oracle_no_gate_non_always_returns_single_auditor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero-gate criterion with requirement!='always' returns the sync gate result."""
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "sampled")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _forbidden_jury)

    class _Gate:
        passed = True
        requirement = "sampled"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"
    assert result.gate_id is None


def test_run_oracle_single_auditor_fail_maps_to_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing single-auditor gate maps to status='fail'."""
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "skip")

    class _Gate:
        passed = False
        requirement = "skip"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "fail"


# --------------------------------------------------------------------------- #
# CR-3: gates are tried in ascending tier order.
# --------------------------------------------------------------------------- #


def test_run_oracle_tries_gates_in_ascending_tier_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runner compiles gates lowest-tier-first regardless of input order."""
    seen: list[str] = []

    def _spy_compile(gate: GateSpec, *, criterion: CriterionSpec) -> CheckSpec:
        seen.append(gate.id)
        return CheckSpec(kind="file_exists", name=gate.id)

    def _raising_run(specs: list[CheckSpec], *, cwd: Path | None = None) -> list[CheckResult]:
        raise RuntimeError("gate unavailable")

    # Raised gates are skipped, so the runner exhausts every gate before falling through.
    monkeypatch.setattr(oracle, "compile_gate", _spy_compile)
    monkeypatch.setattr(oracle, "run_checks", _raising_run)
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "skip")

    class _Gate:
        passed = True
        requirement = "skip"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    gates = [
        _gate("G-T5", "svg_pixel_diff"),  # T5_GOLDEN
        _gate("G-T1", "file_exists"),  # T1_STATIC
        _gate("G-T4", "command_exit_zero"),  # T4_CONTRACT
        _gate("G-T2", "state_field_equals"),  # T2_STRUCTURAL
    ]
    _run(_criterion(), gates, repo_root=tmp_path)

    assert seen == ["G-T1", "G-T2", "G-T4", "G-T5"]


def test_run_oracle_unknown_kind_gate_sorts_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate whose kind is unknown sorts after every known-tier gate."""
    seen: list[str] = []

    def _spy_compile(gate: GateSpec, *, criterion: CriterionSpec) -> CheckSpec:
        seen.append(gate.id)
        return CheckSpec(kind="file_exists", name=gate.id)

    def _raising_run(specs: list[CheckSpec], *, cwd: Path | None = None) -> list[CheckResult]:
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(oracle, "compile_gate", _spy_compile)
    monkeypatch.setattr(oracle, "run_checks", _raising_run)
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "skip")

    class _Gate:
        passed = True
        requirement = "skip"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    gates = [
        _gate("G-UNKNOWN", "totally-made-up-kind"),
        _gate("G-T1", "file_exists"),
    ]
    _run(_criterion(), gates, repo_root=tmp_path)

    assert seen == ["G-T1", "G-UNKNOWN"]


# --------------------------------------------------------------------------- #
# Boundary + error paths.
# --------------------------------------------------------------------------- #


def test_run_oracle_always_requirement_consults_jury(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero-gate criterion with requirement=='always' convenes the async jury."""
    called: list[bool] = []

    async def _jury(**_kwargs: Any) -> Any:
        called.append(True)

        class _Result:
            outcome = JuryAggregateOutcome.PASS

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)
    monkeypatch.setattr(
        oracle,
        "verify_wave_verdict_gate",
        lambda *a, **k: pytest.fail("single-auditor gate must not run when requirement=='always'"),
    )

    result = _run(_criterion(), [], repo_root=tmp_path)

    assert called == [True]
    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"


def test_run_oracle_jury_needs_user_held_advisory_close_proceeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A NEEDS_USER jury outcome is held advisory: maps to status='pass'.

    An uncalibrated jury holds only advisory authority, so ANY non-pass
    outcome -- a FAIL veto or a NEEDS_USER split / sub-quorum -- is logged at
    WARNING and the close proceeds as ``"pass"`` rather than blocking. TRUST-4
    restores blocking on a calibrated jury.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.NEEDS_USER

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    with caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"):
        result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.status == "pass"
    assert any("jury_veto_advisory" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# W10: a jury veto is held advisory until TRUST-4 -- it is logged at WARNING
# and the close proceeds rather than blocking on an uncalibrated jury.
# --------------------------------------------------------------------------- #


def test_run_oracle_jury_veto_held_advisory_close_proceeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """An always-band FAIL veto returns status='pass' with a WARNING, no block.

    The live cross-vendor jury is uncalibrated on eawf's own distribution, so
    its veto is advisory until TRUST-4: the binding point
    :func:`oracle.jury_block_authority` returns ``"advisory"``, the veto is
    logged at WARNING, and the oracle returns ``status="pass"`` so the close
    proceeds. The negative space -- that the veto does NOT map to ``"fail"`` --
    is the load-bearing safety property: a correct close drawing an
    uncalibrated veto is never blocked.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.FAIL

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    with caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"):
        result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "jury_veto_advisory" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "authority=advisory" in warnings[0].getMessage()
    assert "close_proceeds=True" in warnings[0].getMessage()


def test_run_oracle_correct_close_with_uncalibrated_veto_not_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A correct close that draws an uncalibrated jury veto is not blocked.

    The oracle returns ``status="pass"`` (never ``"fail"``) so the
    per-criterion close-gate frontier proceeds past this criterion rather than
    raising. This is the negative-path mirror of the advisory hold: the absence
    of a block, not the presence of one, is what is asserted.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.FAIL

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)
    assert oracle.jury_block_authority(_wave()) == "advisory"

    result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.status != "fail"
    assert result.status == "pass"


# --------------------------------------------------------------------------- #
# W04 (TRUST-4): the staged advisory-to-block gate. A jury FAIL veto raises
# LifecycleError ONLY when the caller passes BLOCKING authority; the same FAIL
# under ADVISORY authority logs a warning and the close proceeds without raising.
# --------------------------------------------------------------------------- #


def _run_with_authority(
    criterion: CriterionSpec,
    gates: list[GateSpec],
    *,
    repo_root: Path,
    block_authority: BlockAuthority,
) -> OracleResult:
    """Drive :func:`run_oracle` with an explicit block authority."""
    return asyncio.run(
        run_oracle(
            criterion,
            gates,
            wave=_wave(),
            state=object(),  # type: ignore[arg-type] - jury seam is mocked; state is unused
            state_path=repo_root / "state.json",
            events_path=repo_root / "event.jsonl",
            repo_root=repo_root,
            spawn_factory=_spawn_factory_stub,
            block_authority=block_authority,
        )
    )


def test_run_oracle_jury_fail_under_blocking_authority_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """C2: a jury FAIL under BLOCKING authority raises LifecycleError (close blocked).

    The calibrated jury has earned blocking authority, so its veto is no longer
    advisory: the close-gate jury branch raises :class:`LifecycleError` rather
    than returning ``status="pass"``. This is the half of C2 where the jury
    actually blocks.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.FAIL

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    with (
        caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"),
        pytest.raises(LifecycleError, match="cross-vendor jury vetoed close"),
    ):
        _run_with_authority(
            _criterion(),
            [],
            repo_root=tmp_path,
            block_authority=BlockAuthority.BLOCKING,
        )

    assert any("jury_veto_blocking" in r.getMessage() for r in caplog.records)
    assert any("authority=blocking" in r.getMessage() for r in caplog.records)


def test_run_oracle_jury_fail_under_advisory_authority_proceeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """C2: the SAME jury FAIL under ADVISORY authority logs a warning, no raise.

    The negative-space mirror of the blocking path: an uncalibrated jury holds
    only advisory authority, so the identical FAIL veto returns ``status="pass"``
    with a WARNING and the close proceeds -- never a LifecycleError.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.FAIL

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    with caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"):
        result = _run_with_authority(
            _criterion(),
            [],
            repo_root=tmp_path,
            block_authority=BlockAuthority.ADVISORY,
        )

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"
    warnings = [r for r in caplog.records if "jury_veto_advisory" in r.getMessage()]
    assert len(warnings) == 1
    assert "close_proceeds=True" in warnings[0].getMessage()


def test_run_oracle_default_authority_is_advisory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2: omitting block_authority defaults to ADVISORY (back-compat, no raise).

    A caller that does not pass ``block_authority`` -- every pre-W04 call site --
    keeps the advisory hold: a FAIL veto returns ``status="pass"`` rather than
    raising, so the default is the safe back-compat behaviour.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.FAIL

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    result = _run(_criterion(), [], repo_root=tmp_path)

    assert result.status == "pass"


def test_run_oracle_jury_pass_under_blocking_authority_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2: a jury PASS under BLOCKING authority is a clean pass, never a raise.

    Blocking authority only ever blocks a NON-pass outcome; a clean jury PASS
    returns ``status="pass"`` regardless of authority -- the gate raises on a
    veto, not on a clean close.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.PASS

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    result = _run_with_authority(
        _criterion(),
        [],
        repo_root=tmp_path,
        block_authority=BlockAuthority.BLOCKING,
    )

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"


def test_run_oracle_jury_needs_user_under_blocking_authority_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2: a NEEDS_USER jury outcome under BLOCKING authority also raises.

    Blocking authority blocks ANY non-pass outcome, not only a substantive FAIL:
    an unresolved split / sub-quorum (NEEDS_USER) under a calibrated jury raises
    :class:`LifecycleError` rather than silently proceeding.
    """

    async def _jury(**_kwargs: Any) -> Any:
        class _Result:
            outcome = JuryAggregateOutcome.NEEDS_USER

        return _Result()

    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "always")
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _jury)

    with pytest.raises(LifecycleError, match="cross-vendor jury vetoed close"):
        _run_with_authority(
            _criterion(),
            [],
            repo_root=tmp_path,
            block_authority=BlockAuthority.BLOCKING,
        )


def test_run_oracle_non_deterministic_criterion_skips_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-deterministic criterion never compiles a gate; it goes to the verdict tier."""
    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda *a, **k: pytest.fail("compile_gate must not run for a non-deterministic criterion"),
    )
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "skip")

    class _Gate:
        passed = True
        requirement = "skip"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    result = _run(
        _criterion(evidence_kind="jury"),
        [_gate("G-1", "file_exists")],
        repo_root=tmp_path,
    )

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "pass"


def test_run_oracle_raising_gate_is_caught_and_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate whose run raises is caught (not propagated) and escalation continues."""

    def _raising_run(specs: list[CheckSpec], *, cwd: Path | None = None) -> list[CheckResult]:
        raise RuntimeError("gate blew up")

    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda gate, *, criterion: CheckSpec(kind="file_exists", name=gate.id),
    )
    monkeypatch.setattr(oracle, "run_checks", _raising_run)
    monkeypatch.setattr(oracle, "verdict_requirement", lambda wave: "skip")

    class _Gate:
        passed = False
        requirement = "skip"

    monkeypatch.setattr(oracle, "verify_wave_verdict_gate", lambda wave, *, state_path: _Gate())

    # The only gate raises; the runner must fall through to the verdict tier
    # and surface its result rather than propagating the RuntimeError.
    result = _run(_criterion(), [_gate("G-1", "file_exists")], repo_root=tmp_path)

    assert result.tier is OracleTier.T7_JURY
    assert result.status == "fail"


def test_run_oracle_compile_returns_none_skips_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate that compiles to None is skipped; the runner moves to the next tier."""

    def _compile(gate: GateSpec, *, criterion: CriterionSpec) -> CheckSpec | None:
        if gate.id == "G-T1":
            return None
        return CheckSpec(kind="file_exists", name=gate.id)

    monkeypatch.setattr(oracle, "compile_gate", _compile)
    monkeypatch.setattr(
        oracle, "run_checks", lambda specs, *, cwd=None: [_check_result(status="pass")]
    )
    monkeypatch.setattr(oracle, "convene_cross_vendor_jury", _forbidden_jury)

    gates = [_gate("G-T1", "file_exists"), _gate("G-T2", "state_field_equals")]
    result = _run(_criterion(), gates, repo_root=tmp_path)

    # G-T1 compiled to None (skipped); G-T2 passed at T2_STRUCTURAL.
    assert result.tier is OracleTier.T2_STRUCTURAL
    assert result.gate_id == "G-T2"
    assert result.status == "pass"


# --------------------------------------------------------------------------- #
# OracleResult.failing_detail -- the grounding payload a repair re-dispatch is fed.
# --------------------------------------------------------------------------- #


def test_failing_detail_returns_recorded_detail() -> None:
    """A refused result surfaces its recorded detail as the repair grounding payload."""
    result = OracleResult(
        tier=OracleTier.T1_STATIC,
        status="fail",
        criterion_id="CR-01",
        gate_id="G-T1",
        detail="command_exit_zero gate exit=1: import budget exceeded",
    )

    assert result.failing_detail() == "command_exit_zero gate exit=1: import budget exceeded"


def test_failing_detail_falls_back_when_detail_empty() -> None:
    """A refused result with no detail still yields a non-empty grounding payload."""
    result = OracleResult(
        tier=OracleTier.T7_JURY,
        status="fail",
        criterion_id="CR-01",
    )

    detail = result.failing_detail()
    # Non-empty so the repair builder's guard always has something to ground on.
    assert detail
    assert "CR-01" in detail
    assert "fail" in detail


def test_failing_detail_raises_on_passing_result() -> None:
    """A passing result has no failing check, so failing_detail raises."""
    result = OracleResult(
        tier=OracleTier.T2_STRUCTURAL,
        status="pass",
        criterion_id="CR-01",
        gate_id="G-T2",
        detail="all checks green",
    )

    with pytest.raises(ValueError, match="passed: no failing detail"):
        result.failing_detail()
