"""DEL-036: minted gates carry declared conventions rather than defaults.

One case per convention -- scope, timeout class, one wave-close argv per
criterion, a non-blocking ship gate -- and one stated exception, each
driven through :func:`validate_gate_conventions`, the check promotion to
``READY`` runs.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.wave_body import WaveSpecBody
from eawf.workflow.verify.gate_conventions import (
    GateConventionError,
    validate_gate_conventions,
)

EXCEPTION = "the migration rehearsal replays the whole corpus, measured at 140 s"


def _gate(
    gate_id: str = "G-01",
    *,
    criterion_id: str = "CR-01",
    kind: str = "command_exit_zero",
    cadence: str = "every-wave",
    required: bool = True,
    **args: Any,
) -> GateSpec:
    body: dict[str, Any] = {"argv": ["uv", "run", "pytest", "tests/unit", "-q"]}
    body.update({"scope": "all", "timeout_class": "quick"})
    body.update(args)
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind=kind,
        args={key: value for key, value in body.items() if value is not None},
        policy="block",
        cadence=cadence,  # type: ignore[arg-type]
        required=required,
    )


def _check(*gates: GateSpec, has_commit: bool = False, exception: str | None = None) -> None:
    validate_gate_conventions(gates, wave_has_commit=has_commit, timeout_exception=exception)


def test_del_036_a_conventional_gate_passes() -> None:
    _check(_gate())


def test_del_036_no_gates_pass_trivially() -> None:
    _check()


def test_del_036_a_gate_without_argv_is_not_subject_to_the_conventions() -> None:
    gate = GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="schema_validate",
        args={"model": "CloseReadiness"},
        policy="block",
        cadence="every-wave",
    )
    _check(gate)


# ---- scope ---------------------------------------------------------------


def test_del_036_a_defaulted_scope_is_refused() -> None:
    with pytest.raises(GateConventionError, match="declares no scope"):
        _check(_gate(scope=None))


@pytest.mark.parametrize("scope", ["changed", "touched"])
def test_del_036_a_narrowed_scope_is_refused_before_the_wave_has_a_commit(scope: str) -> None:
    with pytest.raises(GateConventionError, match="does not have yet"):
        _check(_gate(scope=scope))


@pytest.mark.parametrize("scope", ["changed", "touched", "all"])
def test_del_036_a_wave_with_a_commit_may_narrow_its_scope(scope: str) -> None:
    _check(_gate(scope=scope), has_commit=True)


# ---- timeout class -------------------------------------------------------


def test_del_036_a_defaulted_timeout_class_is_refused() -> None:
    with pytest.raises(GateConventionError, match="declares no timeout_class"):
        _check(_gate(timeout_class=None))


@pytest.mark.parametrize("timeout_class", ["standard", "slow", "very_slow"])
def test_del_036_a_class_past_quick_is_refused_without_an_exception(timeout_class: str) -> None:
    with pytest.raises(GateConventionError, match="timeout_exception"):
        _check(_gate(timeout_class=timeout_class))


def test_del_036_a_stated_exception_admits_a_longer_class() -> None:
    _check(_gate(timeout_class="slow"), exception=EXCEPTION)


def test_del_036_the_exception_is_stated_on_the_wave_body() -> None:
    body = WaveSpecBody.model_validate({"timeout_exception": EXCEPTION})
    assert body.timeout_exception == EXCEPTION
    with pytest.raises(ValidationError):
        WaveSpecBody.model_validate({"timeout_exception": "too slow"})


# ---- one argv per criterion at wave close ---------------------------------


def test_del_036_one_gate_per_repetition_is_refused() -> None:
    first, second = _gate("G-01"), _gate("G-02")
    with pytest.raises(GateConventionError, match="2 wave-close argv gates"):
        _check(first, second)


def test_del_036_a_second_argv_for_an_unobservable_half_is_refused() -> None:
    other = _gate("G-02", argv=["uv", "run", "ruff", "check", "src"])
    with pytest.raises(GateConventionError, match="assertion in that test"):
        _check(_gate("G-01"), other)


def test_del_036_two_criteria_each_keep_one_close_gate() -> None:
    _check(_gate("G-01"), _gate("G-02", criterion_id="CR-02"))


# ---- a CI result is a non-blocking ship gate -------------------------------


def test_del_036_a_ci_half_is_a_second_gate_at_ship_cadence() -> None:
    ship = _gate("G-02", cadence="ship", required=False)
    _check(_gate("G-01"), ship)


def test_del_036_a_ship_gate_that_would_block_the_wave_close_is_refused() -> None:
    ship = _gate("G-02", cadence="ship", required=True)
    with pytest.raises(GateConventionError, match="required: false"):
        _check(_gate("G-01"), ship)
