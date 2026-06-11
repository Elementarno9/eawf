"""Unit tests for the grounded repair re-dispatch loop (P30-I06-W07 / FLEET-7).

A wave close refused on a specific criterion must yield a GROUNDED repair
re-dispatch: the prompt carries the refused criterion's text PLUS the concrete
failing check's output, and the loop is bounded so a cap raises a typed
exhaustion rather than re-dispatching forever. A content-free "drifted, redo"
repair is structurally impossible -- the repair builder
(:func:`~eawf.workflow.dispatch.retry.build_repair_prompt`) raises before it can
assemble a prompt when the failing payload is missing.

The repair spawn is ALWAYS a recording stub -- these tests never fork a real
subprocess (no network, no auth, no cost). The verifier is a scripted stub
standing in for the close-gate oracle re-run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.retry import (
    RepairExhaustedError,
    RepairWithoutFailureError,
    RetryExhaustedError,
    build_repair_prompt,
    repair_until_resolved,
)

_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 10, 12, 0, 5, tzinfo=UTC)

_CRITERION_TEXT = "the cold-path import budget stays under the FORBIDDEN_MODULES ceiling"
_FAILING_DETAIL = "command_exit_zero gate exit=1: registry imported on the CLI cold path"


def _criterion(cid: str = "CR-07") -> CriterionSpec:
    """Build a typed success criterion whose text grounds the repair prompt."""
    return CriterionSpec(
        id=cid,
        text=_CRITERION_TEXT,
        kind="behavior",
        acceptance_style="binary",
        evidence_kind="deterministic",
        required=True,
        quality_dimension=QualityDimension.PERFORMANCE_EFFICIENCY,
        measurable_signal="the cold-path import gate scores this criterion deterministically",
    )


def _spawn_result(runtime: str = "claude-code", *, text: str = "repaired") -> SpawnResult:
    """Build an otherwise-valid :class:`SpawnResult` for one repair re-dispatch."""
    return SpawnResult(
        session_id=f"sess-{runtime}",
        runtime=runtime,
        model="opus",
        subprocess_pid=4321,
        exit_status=0,
        text=text,
        started_at=_T0,
        ended_at=_T1,
    )


class _RecordingSpawn:
    """Records the grounded prompt each repair re-dispatch was handed.

    Returns a clean :class:`SpawnResult` per call. Raises if called more times
    than the bound allows so an unbounded loop surfaces as a test failure rather
    than hanging.
    """

    def __init__(self, *, limit: int) -> None:
        self.prompts: list[str] = []
        self._limit = limit

    async def __call__(self, prompt: str) -> SpawnResult:
        if len(self.prompts) >= self._limit:
            raise AssertionError(
                f"repair spawn called {len(self.prompts) + 1} times but "
                f"only {self._limit} call(s) allowed (unbounded loop?)"
            )
        self.prompts.append(prompt)
        return _spawn_result()


# ---------------------------------------------------------------------------
# Criterion 1a: the built repair prompt is grounded (criterion text + detail)
# ---------------------------------------------------------------------------


def test_build_repair_prompt_carries_criterion_text_and_failing_detail() -> None:
    """A repair prompt for a refused criterion carries its text AND the failing detail."""
    criterion = _criterion()

    prompt = build_repair_prompt(
        criterion,
        _FAILING_DETAIL,
        base_prompt="ORIGINAL DISPATCH PROMPT",
        attempt=1,
    )

    assert _CRITERION_TEXT in prompt
    assert _FAILING_DETAIL in prompt
    # The original contract is preserved verbatim under the repair notice.
    assert "ORIGINAL DISPATCH PROMPT" in prompt
    assert criterion.id in prompt


def test_repair_loop_first_redispatch_is_grounded() -> None:
    """The first repair re-dispatch carries the criterion text + failing detail."""
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=1)

    result = asyncio.run(
        repair_until_resolved(
            criterion,
            _FAILING_DETAIL,
            base_prompt="ORIGINAL DISPATCH PROMPT",
            spawn=spawn,
            verify=lambda _result: None,  # first re-dispatch resolves the refusal
        )
    )

    assert result.runtime == "claude-code"
    assert len(spawn.prompts) == 1
    grounded = spawn.prompts[0]
    assert _CRITERION_TEXT in grounded
    assert _FAILING_DETAIL in grounded


# ---------------------------------------------------------------------------
# Criterion 1b: the repair loop is bounded -- the cap raises a typed exhaustion
# ---------------------------------------------------------------------------


def test_repair_loop_exhausts_when_every_attempt_still_fails() -> None:
    """When every re-dispatch still fails the criterion, the cap raises -- no infinite loop."""
    criterion = _criterion()
    max_attempts = 3
    spawn = _RecordingSpawn(limit=max_attempts)

    # The verifier always reports the criterion is still failing, so the loop
    # exhausts at the cap rather than looping forever.
    def _always_failing(_result: SpawnResult) -> str:
        return "criterion still failing after repair attempt"

    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            repair_until_resolved(
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=_always_failing,
                max_attempts=max_attempts,
            )
        )

    # Bounded: exactly the cap of re-dispatches, every one recorded as a failure.
    assert len(spawn.prompts) == max_attempts
    assert len(excinfo.value.failures) == max_attempts
    assert excinfo.value.attempts == max_attempts


def test_repair_loop_exhaustion_raises_repair_exhausted_carrying_last_check() -> None:
    """The spent loop raises the DL-7 RepairExhaustedError naming the criterion + last check."""
    criterion = _criterion()
    max_attempts = 2
    spawn = _RecordingSpawn(limit=max_attempts)
    # The verifier re-grounds on a fresh detail each round; the LAST detail is the
    # one the escalation fork must carry.
    last_detail = "freshest falsifier output for the final repair attempt"
    outcomes: list[str] = ["stale detail for retry 2", last_detail]
    calls = iter(outcomes)

    with pytest.raises(RepairExhaustedError) as excinfo:
        asyncio.run(
            repair_until_resolved(
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=lambda _result: next(calls),
                max_attempts=max_attempts,
            )
        )
    exc = excinfo.value
    # The specialised error is still a RetryExhaustedError (base-type catchers keep working).
    assert isinstance(exc, RetryExhaustedError)
    assert exc.criterion_id == criterion.id
    # C1: it carries the LAST failing check (the freshest falsifier), not the first.
    assert exc.last_failing_detail == last_detail


def test_repair_loop_regrounds_each_retry_on_freshest_detail() -> None:
    """Each retry re-grounds the prompt on the verifier's freshest failing payload."""
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=2)
    # First attempt still fails with a fresh detail; second attempt resolves.
    outcomes: list[str | None] = ["fresh falsifier output for retry 2", None]
    calls = iter(outcomes)

    result = asyncio.run(
        repair_until_resolved(
            criterion,
            _FAILING_DETAIL,
            base_prompt="ORIGINAL DISPATCH PROMPT",
            spawn=spawn,
            verify=lambda _result: next(calls),
        )
    )

    assert result.runtime == "claude-code"
    assert len(spawn.prompts) == 2
    # First re-dispatch grounded on the original failing detail.
    assert _FAILING_DETAIL in spawn.prompts[0]
    # Second re-dispatch re-grounded on the verifier's freshest payload.
    assert "fresh falsifier output for retry 2" in spawn.prompts[1]


def test_repair_loop_rejects_zero_max_attempts() -> None:
    """A non-positive ceiling fails fast -- the loop never spawns."""
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=0)

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        asyncio.run(
            repair_until_resolved(
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=lambda _result: None,
                max_attempts=0,
            )
        )
    assert spawn.prompts == []


# ---------------------------------------------------------------------------
# Criterion 2: a content-free repair cannot be built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, "", "   ", "\n\t "])
def test_build_repair_prompt_raises_without_failing_payload(payload: str | None) -> None:
    """The repair builder raises on a None / empty / whitespace-only payload."""
    criterion = _criterion()

    with pytest.raises(RepairWithoutFailureError, match="without a resolved failing-check payload"):
        build_repair_prompt(
            criterion,
            payload,
            base_prompt="ORIGINAL DISPATCH PROMPT",
            attempt=1,
        )
