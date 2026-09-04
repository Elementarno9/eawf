"""Tests for :mod:`eawf.kernel.spec.round_loop`.

Pins the bounded round loop + checkpoint-policy tiers:

1. The loop halts on saturation (the campaign goes dry before the budget)
   and reports :attr:`RoundHaltReason.SATURATED`.
2. The loop halts on the round budget (the campaign never saturates) and
   reports :attr:`RoundHaltReason.ROUND_BUDGET`.
3. Saturation wins the halt precedence: a round that both saturates and
   exhausts the budget reports SATURATED, not ROUND_BUDGET.
4. Each of the four :class:`CheckpointTier` cadences gates the recorded
   operator-review checkpoints as specified (every round / every n / on
   halt / never).
5. Boundary cases: a one-round budget, a budget-1 saturation, and an
   ``EVERY_N`` interval that never divides a completed round.
6. Error paths: a zero / negative round budget raises ``ValueError``; a
   ``CheckpointPolicy`` interval below 1 fails validation; an
   ``extra='forbid'`` typo is rejected (AGENTS rule 2).
7. The driver is pure: it spawns no subprocess and imports no adapter.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.round_loop import (
    DEFAULT_CHECKPOINT_INTERVAL,
    DEFAULT_ROUND_BUDGET,
    Checkpoint,
    CheckpointPolicy,
    CheckpointTier,
    RoundHaltReason,
    RoundLoopResult,
    RoundOutcome,
    run_round_loop,
)
from eawf.kernel.spec.saturation import SaturationGateResult, SaturationReport


def _report(*, saturated: bool) -> SaturationReport:
    """Return a minimal SaturationReport with the given ``saturated`` bit.

    The round loop only reads ``saturated``; the gate detail is filler so
    the report is well-formed without standing up real ledgers.
    """
    gate = SaturationGateResult(
        name="novelty_decay",
        passed=saturated,
        detail="test fixture",
    )
    return SaturationReport(
        saturated=saturated,
        gates=(gate,),
        live_claim_count=1,
        empty_ledger=False,
    )


def _runner(saturate_on: int | None) -> Callable[[int], RoundOutcome]:
    """Build a round_runner that reports saturation on round ``saturate_on``.

    Args:
        saturate_on: The 1-based round at which the runner returns a
            saturated report. ``None`` means the runner never saturates
            (every round reports ``saturated=False``).
    """

    def _run(round_number: int) -> RoundOutcome:
        is_sat = saturate_on is not None and round_number >= saturate_on
        return RoundOutcome(saturation=_report(saturated=is_sat))

    return _run


# --- Halt on saturation -------------------------------------------------


def test_loop_halts_on_saturation_before_budget() -> None:
    """A campaign that goes dry mid-budget stops and reports SATURATED."""
    result = run_round_loop(_runner(saturate_on=3), round_budget=10)
    assert isinstance(result, RoundLoopResult)
    assert result.halt_reason is RoundHaltReason.SATURATED
    assert result.rounds_run == 3
    assert result.saturated is True
    assert result.final_saturation.saturated is True


def test_loop_halts_on_first_round_saturation() -> None:
    """Saturation on the very first round stops the loop after one round."""
    result = run_round_loop(_runner(saturate_on=1), round_budget=10)
    assert result.halt_reason is RoundHaltReason.SATURATED
    assert result.rounds_run == 1


# --- Halt on round budget -----------------------------------------------


def test_loop_halts_on_round_budget_when_never_saturates() -> None:
    """A campaign that never converges stops at the budget ceiling."""
    result = run_round_loop(_runner(saturate_on=None), round_budget=5)
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET
    assert result.rounds_run == 5
    assert result.saturated is False
    assert result.final_saturation.saturated is False


def test_loop_default_budget_is_the_ceiling() -> None:
    """An omitted budget falls back to DEFAULT_ROUND_BUDGET as the ceiling."""
    result = run_round_loop(_runner(saturate_on=None))
    assert result.rounds_run == DEFAULT_ROUND_BUDGET
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET


# --- Halt precedence: saturation wins -----------------------------------


def test_saturation_wins_when_budget_also_spent_same_round() -> None:
    """A round that both saturates and exhausts the budget reports SATURATED."""
    # round_budget == saturate_on: the final round trips both conditions.
    result = run_round_loop(_runner(saturate_on=4), round_budget=4)
    assert result.rounds_run == 4
    assert result.halt_reason is RoundHaltReason.SATURATED
    assert result.saturated is True


# --- Boundary: one-round budget -----------------------------------------


def test_loop_one_round_budget_no_saturation() -> None:
    """A budget of one runs exactly one round then halts on budget."""
    result = run_round_loop(_runner(saturate_on=None), round_budget=1)
    assert result.rounds_run == 1
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET


def test_loop_one_round_budget_with_saturation() -> None:
    """A one-round budget that saturates on round one reports SATURATED."""
    result = run_round_loop(_runner(saturate_on=1), round_budget=1)
    assert result.rounds_run == 1
    assert result.halt_reason is RoundHaltReason.SATURATED


# --- Checkpoint tier: EVERY_ROUND ---------------------------------------


def test_checkpoint_every_round_records_each_round() -> None:
    """The EVERY_ROUND tier records one checkpoint per completed round."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_ROUND)
    result = run_round_loop(_runner(saturate_on=None), round_budget=4, checkpoint_policy=policy)
    assert [c.round_number for c in result.checkpoints] == [1, 2, 3, 4]
    # Only the terminal round is flagged terminal.
    assert [c.terminal for c in result.checkpoints] == [False, False, False, True]


# --- Checkpoint tier: EVERY_N -------------------------------------------


def test_checkpoint_every_n_records_on_interval() -> None:
    """The EVERY_N tier records a checkpoint every ``interval`` rounds."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_N, interval=2)
    result = run_round_loop(_runner(saturate_on=None), round_budget=5, checkpoint_policy=policy)
    # Rounds 2 and 4 divide the interval; round 5 (terminal) does not, so
    # the EVERY_N tier does NOT add a terminal checkpoint of its own.
    assert [c.round_number for c in result.checkpoints] == [2, 4]


def test_checkpoint_every_n_interval_never_divides() -> None:
    """An EVERY_N interval larger than the rounds run records nothing."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_N, interval=10)
    result = run_round_loop(_runner(saturate_on=None), round_budget=4, checkpoint_policy=policy)
    assert result.checkpoints == []


def test_checkpoint_every_n_default_interval() -> None:
    """An EVERY_N policy with no interval uses the default cadence."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_N)
    assert policy.interval == DEFAULT_CHECKPOINT_INTERVAL


# --- Checkpoint tier: ON_HALT (the default) -----------------------------


def test_checkpoint_on_halt_records_only_terminal_round() -> None:
    """The ON_HALT tier records a single checkpoint on the terminal round."""
    policy = CheckpointPolicy(tier=CheckpointTier.ON_HALT)
    result = run_round_loop(_runner(saturate_on=None), round_budget=4, checkpoint_policy=policy)
    assert len(result.checkpoints) == 1
    assert result.checkpoints[0].round_number == 4
    assert result.checkpoints[0].terminal is True


def test_checkpoint_policy_defaults_to_on_halt() -> None:
    """A bare CheckpointPolicy (and an omitted policy) defaults to ON_HALT."""
    assert CheckpointPolicy().tier is CheckpointTier.ON_HALT
    # Omitting the policy entirely behaves like the ON_HALT default.
    result = run_round_loop(_runner(saturate_on=None), round_budget=3)
    assert len(result.checkpoints) == 1
    assert result.checkpoints[0].round_number == 3


def test_checkpoint_on_halt_marks_saturation_state() -> None:
    """The ON_HALT terminal checkpoint carries the round's saturation bit."""
    result = run_round_loop(_runner(saturate_on=2), round_budget=10)
    assert len(result.checkpoints) == 1
    assert result.checkpoints[0].saturated is True


# --- Checkpoint tier: NEVER ---------------------------------------------


def test_checkpoint_never_records_nothing() -> None:
    """The NEVER tier records no checkpoint even on the terminal halt."""
    policy = CheckpointPolicy(tier=CheckpointTier.NEVER)
    result = run_round_loop(_runner(saturate_on=None), round_budget=4, checkpoint_policy=policy)
    assert result.checkpoints == []
    # The terminal result is still returned in full.
    assert result.rounds_run == 4
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET


# --- CheckpointPolicy.wants_checkpoint unit behaviour --------------------


def test_wants_checkpoint_every_n_uses_modulo() -> None:
    """``wants_checkpoint`` fires for the EVERY_N tier on interval multiples."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_N, interval=3)
    assert policy.wants_checkpoint(3, halted=False) is True
    assert policy.wants_checkpoint(6, halted=False) is True
    assert policy.wants_checkpoint(4, halted=False) is False


def test_wants_checkpoint_on_halt_ignores_non_terminal_rounds() -> None:
    """The ON_HALT tier fires only when ``halted`` is True."""
    policy = CheckpointPolicy(tier=CheckpointTier.ON_HALT)
    assert policy.wants_checkpoint(1, halted=False) is False
    assert policy.wants_checkpoint(7, halted=True) is True


def test_wants_checkpoint_rejects_zero_round_number() -> None:
    """A non-positive round number is an error — the loop is 1-based."""
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_ROUND)
    with pytest.raises(ValueError, match="round_number must be >= 1"):
        policy.wants_checkpoint(0, halted=False)


# --- Error path: zero / negative round budget ---------------------------


def test_run_round_loop_rejects_zero_budget() -> None:
    """A zero round budget raises ValueError — a loop must run a round."""
    with pytest.raises(ValueError, match="round_budget must be >= 1"):
        run_round_loop(_runner(saturate_on=None), round_budget=0)


def test_run_round_loop_rejects_negative_budget() -> None:
    """A negative round budget raises ValueError at the driver boundary."""
    with pytest.raises(ValueError, match="round_budget must be >= 1"):
        run_round_loop(_runner(saturate_on=None), round_budget=-3)


# --- Error path: model validation ---------------------------------------


def test_checkpoint_policy_rejects_zero_interval() -> None:
    """A CheckpointPolicy interval below 1 fails validation (ge=1)."""
    with pytest.raises(ValidationError):
        CheckpointPolicy(tier=CheckpointTier.EVERY_N, interval=0)


def test_checkpoint_policy_rejects_unknown_field() -> None:
    """``extra='forbid'`` rejects a typo'd policy key (AGENTS rule 2)."""
    with pytest.raises(ValidationError):
        CheckpointPolicy.model_validate({"teir": "every_round"})


def test_checkpoint_policy_rejects_bad_tier_token() -> None:
    """An out-of-ladder tier token fails validation, not silently coerces."""
    with pytest.raises(ValidationError):
        CheckpointPolicy.model_validate({"tier": "hourly"})


def test_checkpoint_rejects_zero_round_number() -> None:
    """A Checkpoint cannot record a non-positive round number (ge=1)."""
    with pytest.raises(ValidationError):
        Checkpoint(round_number=0, saturated=False)


# --- Purity: the driver spawns nothing ----------------------------------


def test_run_round_loop_does_not_spawn_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver is pure: it never reaches a subprocess or an adapter.

    We sabotage the subprocess seams and assert the loop still runs,
    proving the round sequencing reaches neither the shell nor a runtime
    adapter — the survey work lives entirely in the injected runner.
    """
    import subprocess

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("run_round_loop must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    result = run_round_loop(_runner(saturate_on=2), round_budget=5)
    assert result.halt_reason is RoundHaltReason.SATURATED

    import eawf.kernel.spec.round_loop as round_loop_mod

    assert not hasattr(round_loop_mod, "subprocess")
    assert not hasattr(round_loop_mod, "RuntimeAdapter")


# --- Package re-export ---------------------------------------------------


def test_round_loop_symbols_reexported_from_package() -> None:
    """Every public round-loop symbol resolves through the lazy package init."""
    from eawf.kernel import spec

    for name in (
        "Checkpoint",
        "CheckpointPolicy",
        "CheckpointTier",
        "RoundHaltReason",
        "RoundLoopResult",
        "RoundOutcome",
        "run_round_loop",
        "DEFAULT_ROUND_BUDGET",
        "DEFAULT_CHECKPOINT_INTERVAL",
    ):
        assert hasattr(spec, name)
    # The lazily resolved class is the same object as the direct import.
    assert spec.CheckpointTier is CheckpointTier
