"""Unit tests for the bounded spawn-retry loop + tiered failure notice.

Pins the retry / V5-reactive-switch contract over the CLI failure taxonomy for a
live spawn: :func:`~eawf.workflow.dispatch.retry.spawn_with_retry` classifies a
:class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` into the V5 ladder
action (retry-same / switch-runtime / halt), respawns or switches accordingly,
and on a terminal failure raises a typed
:class:`~eawf.workflow.dispatch.retry.RetryExhaustedError` carrying every attempt
plus a tiered :class:`~eawf.workflow.dispatch.retry.FailureNotice`.

The spawn is ALWAYS a recording stub -- these tests never fork a real subprocess
(no network, no auth, no cost). The stub raises a ``RuntimeSpawnError`` with a
canned stderr keyword per ``ErrorClass`` (or yields a clean
:class:`~eawf.runtime.runtimes.adapter.SpawnResult`), and an injected classifier
maps the canned stderr to the canonical error class -- mirroring the real
adapter ``parse_error`` seam without importing a concrete adapter.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.adapter import ErrorClass, RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.fallback import FallbackAction
from eawf.workflow.dispatch.retry import (
    DEFAULT_MAX_ATTEMPTS,
    FailureNotice,
    FailureTier,
    RetryExhaustedError,
    SpawnAttemptFailure,
    failure_tier_for_action,
    spawn_with_retry,
)

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)

# Map a canned stderr keyword the stub spawn raises -> the canonical error class
# the injected classifier returns. Mirrors the per-runtime adapter ``parse_error``
# pattern matching without importing a concrete adapter.
_KEYWORD_TO_CLASS: dict[str, ErrorClass] = {
    "rate-limit": "RUNTIME_RATE_LIMIT",
    "server-error": "RUNTIME_SERVER_ERROR",
    "timeout": "RUNTIME_TIMEOUT",
    "api-error": "RUNTIME_API_ERROR",
    "auth-error": "RUNTIME_AUTH_ERROR",
}


def _spawn_result(runtime: str, *, pid: int = 4321) -> SpawnResult:
    """Build an otherwise-valid :class:`SpawnResult` for a clean spawn on *runtime*."""
    return SpawnResult(
        session_id=f"sess-{runtime}",
        runtime=runtime,
        model="opus",
        subprocess_pid=pid,
        exit_status=0,
        text="done",
        started_at=_T0,
        ended_at=_T1,
    )


def _classify(exc: RuntimeSpawnError, runtime: str) -> ErrorClass:
    """Stub classifier: map the canned stderr keyword in *exc* to an ``ErrorClass``.

    Stands in for a resolved adapter's
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.parse_error` -- the
    keyword the stub spawn embedded in the error message keys the canonical class.
    """
    message = str(exc)
    for keyword, error_class in _KEYWORD_TO_CLASS.items():
        if keyword in message:
            return error_class
    raise AssertionError(f"stub classifier saw an unkeyed error message: {message!r}")


class _ScriptedSpawn:
    """Recording stand-in for a live ``spawn_session`` (NEVER a real process).

    Replays a queue of scripted outcomes -- one per call. An outcome is either a
    stderr keyword string (the call raises ``RuntimeSpawnError`` carrying it) or
    ``None`` (the call returns a clean :class:`SpawnResult` on the runtime it was
    handed). Records the runtime each call was handed so a test can assert the V5
    switch handed the next runtime to the spawn. Raises if called more times than
    outcomes queued so an unbounded loop surfaces as a test failure rather than an
    ``IndexError`` deep in the loop.
    """

    def __init__(self, outcomes: list[str | None]) -> None:
        self._outcomes = list(outcomes)
        self.runtimes: list[str] = []
        self.calls = 0

    async def __call__(self, runtime: str) -> SpawnResult:
        self.runtimes.append(runtime)
        if self.calls >= len(self._outcomes):
            raise AssertionError(
                f"spawn called {self.calls + 1} times but only "
                f"{len(self._outcomes)} outcome(s) queued (unbounded loop?)"
            )
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if outcome is None:
            return _spawn_result(runtime)
        raise RuntimeSpawnError(f"runtime spawn failed: {outcome}")


# ---------------------------------------------------------------------------
# Clean first spawn -> no retry
# ---------------------------------------------------------------------------


def test_spawn_with_retry_clean_first_spawn_no_retry() -> None:
    """A clean first spawn returns immediately -- no classification, no retry."""
    spawn = _ScriptedSpawn([None])
    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex"],
            spawn=spawn,
            classify=_classify,
        )
    )
    assert result.runtime == "claude-code"
    assert spawn.calls == 1
    assert spawn.runtimes == ["claude-code"]


# ---------------------------------------------------------------------------
# RATE_LIMIT -> retry the SAME runtime
# ---------------------------------------------------------------------------


def test_spawn_with_retry_rate_limit_retries_same_runtime_then_succeeds() -> None:
    """A rate-limit failure retries the SAME runtime; the second spawn succeeds."""
    spawn = _ScriptedSpawn(["rate-limit", None])
    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex"],
            spawn=spawn,
            classify=_classify,
        )
    )
    assert result.runtime == "claude-code"
    assert spawn.calls == 2
    # Both spawns ran against the SAME runtime -- a rate limit never switches.
    assert spawn.runtimes == ["claude-code", "claude-code"]


def test_spawn_with_retry_rate_limit_exhausts_same_runtime() -> None:
    """Persistent rate-limit exhausts the ceiling on the same runtime -> transient tier."""
    spawn = _ScriptedSpawn(["rate-limit", "rate-limit", "rate-limit"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code", "codex"],
                spawn=spawn,
                classify=_classify,
                max_attempts=3,
            )
        )
    err = excinfo.value
    assert spawn.calls == 3
    assert spawn.runtimes == ["claude-code", "claude-code", "claude-code"]
    assert err.notice.tier is FailureTier.TRANSIENT_RETRYABLE
    assert err.notice.error_class == "RUNTIME_RATE_LIMIT"


# ---------------------------------------------------------------------------
# SERVER / TIMEOUT / API -> switch to next runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "error_class"),
    [
        ("server-error", "RUNTIME_SERVER_ERROR"),
        ("timeout", "RUNTIME_TIMEOUT"),
        ("api-error", "RUNTIME_API_ERROR"),
    ],
)
def test_spawn_with_retry_availability_error_switches_to_next_runtime(
    keyword: str, error_class: str
) -> None:
    """A server / timeout / api failure switches to the next preference runtime."""
    spawn = _ScriptedSpawn([keyword, None])
    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex", "opencode"],
            spawn=spawn,
            classify=_classify,
        )
    )
    # The retry ran against the NEXT runtime in the preference ladder.
    assert result.runtime == "codex"
    assert spawn.runtimes == ["claude-code", "codex"]


def test_spawn_with_retry_switch_walks_full_ladder() -> None:
    """Consecutive availability failures walk the ladder runtime by runtime."""
    spawn = _ScriptedSpawn(["server-error", "timeout", None])
    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex", "opencode"],
            spawn=spawn,
            classify=_classify,
            max_attempts=3,
        )
    )
    assert result.runtime == "opencode"
    assert spawn.runtimes == ["claude-code", "codex", "opencode"]


def test_spawn_with_retry_switch_exhausts_ladder_raises_switched_tier() -> None:
    """When the preference ladder runs out of runtimes the switch tier is terminal."""
    spawn = _ScriptedSpawn(["server-error", "server-error"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code", "codex"],
                spawn=spawn,
                classify=_classify,
                max_attempts=5,
            )
        )
    err = excinfo.value
    # Two spawns: claude-code fails -> switch to codex -> codex fails -> ladder
    # exhausted (no runtime past codex), so the loop stops before max_attempts.
    assert spawn.runtimes == ["claude-code", "codex"]
    assert err.notice.tier is FailureTier.SWITCHED
    assert err.notice.runtime == "codex"


def test_spawn_with_retry_empty_preference_no_switch_target() -> None:
    """An availability failure with no preference ladder exhausts immediately."""
    spawn = _ScriptedSpawn(["server-error"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=[],
                spawn=spawn,
                classify=_classify,
                max_attempts=3,
            )
        )
    err = excinfo.value
    # No runtime to switch to -> a single spawn, then terminal switched tier.
    assert spawn.calls == 1
    assert err.notice.tier is FailureTier.SWITCHED


# ---------------------------------------------------------------------------
# AUTH -> HALT immediately (no retry)
# ---------------------------------------------------------------------------


def test_spawn_with_retry_auth_error_halts_immediately_no_retry() -> None:
    """An auth failure HALTs on the first spawn -- no retry, no switch -> fatal tier."""
    spawn = _ScriptedSpawn(["auth-error", None, None])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code", "codex"],
                spawn=spawn,
                classify=_classify,
                max_attempts=3,
            )
        )
    err = excinfo.value
    # Exactly one spawn -- auth never auto-retries and never switches.
    assert spawn.calls == 1
    assert spawn.runtimes == ["claude-code"]
    assert err.notice.tier is FailureTier.FATAL_HALT
    assert err.notice.error_class == "RUNTIME_AUTH_ERROR"


# ---------------------------------------------------------------------------
# Bounded exhaustion -> typed error with the full attempt trail
# ---------------------------------------------------------------------------


def test_spawn_with_retry_exhaustion_raises_typed_error_with_full_trail() -> None:
    """Exhausting retries raises RetryExhaustedError carrying every attempt."""
    spawn = _ScriptedSpawn(["rate-limit", "rate-limit", "rate-limit"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code"],
                spawn=spawn,
                classify=_classify,
                max_attempts=3,
            )
        )
    err = excinfo.value
    assert err.attempts == 3
    assert len(err.failures) == 3
    assert all(isinstance(f, SpawnAttemptFailure) for f in err.failures)
    assert [f.attempt for f in err.failures] == [1, 2, 3]
    assert all(f.error_class == "RUNTIME_RATE_LIMIT" for f in err.failures)
    assert all(f.action is FallbackAction.RETRY_SAME for f in err.failures)
    assert "exhausted after 3 attempt(s)" in str(err)


def test_spawn_with_retry_is_bounded_stops_after_max_attempts() -> None:
    """The loop spawns at most max_attempts times, never an infinite loop."""
    # Queue one MORE failing outcome than the ceiling; the loop must not consume it.
    spawn = _ScriptedSpawn(["rate-limit", "rate-limit", "rate-limit", "rate-limit"])
    with pytest.raises(RetryExhaustedError):
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code"],
                spawn=spawn,
                classify=_classify,
                max_attempts=3,
            )
        )
    # Exactly the ceiling -- the 4th outcome was never reached.
    assert spawn.calls == 3


def test_spawn_with_retry_default_ceiling_is_three() -> None:
    """The default attempt ceiling is DEFAULT_MAX_ATTEMPTS (3) spawns."""
    spawn = _ScriptedSpawn(["rate-limit"] * DEFAULT_MAX_ATTEMPTS)
    with pytest.raises(RetryExhaustedError):
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code"],
                spawn=spawn,
                classify=_classify,
            )
        )
    assert spawn.calls == DEFAULT_MAX_ATTEMPTS == 3


# ---------------------------------------------------------------------------
# Boundary: max_attempts=1 (single spawn, no retry)
# ---------------------------------------------------------------------------


def test_spawn_with_retry_single_attempt_succeeds() -> None:
    """max_attempts=1 with a clean spawn returns the result -- one spawn, no retry."""
    spawn = _ScriptedSpawn([None])
    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex"],
            spawn=spawn,
            classify=_classify,
            max_attempts=1,
        )
    )
    assert result.runtime == "claude-code"
    assert spawn.calls == 1


def test_spawn_with_retry_single_attempt_fails_typed_no_retry() -> None:
    """max_attempts=1 with a rate-limit fails typed after one spawn -- no retry."""
    spawn = _ScriptedSpawn(["rate-limit"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code"],
                spawn=spawn,
                classify=_classify,
                max_attempts=1,
            )
        )
    assert spawn.calls == 1
    assert excinfo.value.notice.tier is FailureTier.TRANSIENT_RETRYABLE


# ---------------------------------------------------------------------------
# Error path: invalid max_attempts argument
# ---------------------------------------------------------------------------


def test_spawn_with_retry_rejects_zero_max_attempts() -> None:
    """max_attempts < 1 is a ValueError before any spawn is attempted."""
    spawn = _ScriptedSpawn([None])
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code"],
                spawn=spawn,
                classify=_classify,
                max_attempts=0,
            )
        )
    # No spawn was attempted -- the guard fires first.
    assert spawn.calls == 0


# ---------------------------------------------------------------------------
# Tiered failure notice -- tier per terminal outcome + model invariants
# ---------------------------------------------------------------------------


def test_failure_tier_for_action_maps_every_ladder_action() -> None:
    """Each V5 ladder action maps to exactly one terminal failure tier."""
    assert failure_tier_for_action(FallbackAction.RETRY_SAME) is FailureTier.TRANSIENT_RETRYABLE
    assert failure_tier_for_action(FallbackAction.SWITCH_RUNTIME) is FailureTier.SWITCHED
    assert failure_tier_for_action(FallbackAction.HALT) is FailureTier.FATAL_HALT


def test_failure_notice_is_frozen_and_forbids_extra() -> None:
    """The tiered notice is frozen and rejects unexpected keys (schema mismatch)."""
    notice = FailureNotice(
        tier=FailureTier.FATAL_HALT,
        runtime="claude-code",
        error_class="RUNTIME_AUTH_ERROR",
        attempts_used=1,
        message="auth failed",
    )
    with pytest.raises(ValidationError):
        notice.tier = FailureTier.SWITCHED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FailureNotice(
            tier=FailureTier.FATAL_HALT,
            runtime="claude-code",
            error_class="RUNTIME_AUTH_ERROR",
            attempts_used=1,
            message="auth failed",
            unexpected="x",  # type: ignore[call-arg]
        )


def test_spawn_attempt_failure_requires_positive_attempt() -> None:
    """SpawnAttemptFailure.attempt is ge=1 -- a zero attempt is out of range."""
    with pytest.raises(ValidationError):
        SpawnAttemptFailure(
            attempt=0,
            runtime="claude-code",
            error_class="RUNTIME_RATE_LIMIT",
            action=FallbackAction.RETRY_SAME,
            detail="x",
        )


def test_runtime_spawn_error_carries_exit_status_and_stderr() -> None:
    """RuntimeSpawnError carries the optional (exit_status, stderr) classify context."""
    enriched = RuntimeSpawnError("boom", exit_status=2, stderr=b"401 unauthorized")
    assert enriched.exit_status == 2
    assert enriched.stderr == b"401 unauthorized"
    # The parse-level default: no exit context, empty stderr.
    bare = RuntimeSpawnError("parse failed")
    assert bare.exit_status is None
    assert bare.stderr == b""


def test_spawn_with_retry_drives_a_real_adapter_parse_error() -> None:
    """The loop classifies via a real adapter parse_error bound as the classifier.

    Proves the production integration shape (the daemon binds the resolved
    adapter's ``parse_error``): a ``RuntimeSpawnError`` carrying a rate-limit
    stderr classifies to RUNTIME_RATE_LIMIT and retries the same runtime.
    """
    from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter

    adapter = ClaudeAdapter()

    def _classify_via_adapter(exc: RuntimeSpawnError, runtime: str) -> ErrorClass:
        exit_status = exc.exit_status if exc.exit_status is not None else -1
        return adapter.parse_error(exit_status, exc.stderr)

    calls: list[str] = []

    async def _spawn(runtime: str) -> SpawnResult:
        calls.append(runtime)
        if len(calls) == 1:
            raise RuntimeSpawnError(
                "claude spawn exited nonzero", exit_status=2, stderr=b"429 rate limit exceeded"
            )
        return _spawn_result(runtime)

    result = asyncio.run(
        spawn_with_retry(
            runtime="claude-code",
            preference=["claude-code", "codex"],
            spawn=_spawn,
            classify=_classify_via_adapter,
        )
    )
    assert result.runtime == "claude-code"
    # The rate-limit classification retried the SAME runtime.
    assert calls == ["claude-code", "claude-code"]


def test_retry_exhausted_error_carries_notice_and_trail_together() -> None:
    """The terminal error bundles the full attempt trail AND the tiered notice."""
    spawn = _ScriptedSpawn(["server-error", "timeout"])
    with pytest.raises(RetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code", "codex"],
                spawn=spawn,
                classify=_classify,
                max_attempts=5,
            )
        )
    err = excinfo.value
    # The trail records each runtime + class + action in order.
    assert [f.runtime for f in err.failures] == ["claude-code", "codex"]
    assert [f.error_class for f in err.failures] == ["RUNTIME_SERVER_ERROR", "RUNTIME_TIMEOUT"]
    # The notice classifies the TERMINAL failure (the codex timeout switch).
    assert err.notice.tier is FailureTier.SWITCHED
    assert err.notice.runtime == "codex"
    assert err.notice.error_class == "RUNTIME_TIMEOUT"
    assert err.notice.attempts_used == 2
