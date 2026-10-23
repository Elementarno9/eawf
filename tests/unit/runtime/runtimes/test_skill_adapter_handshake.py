"""Tests for the runtime fallback ladder.

Covers the F-d02 fall-through, where a failed session-handle resolution
builds a fresh-session annotation with
:attr:`DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH` on the same runtime
(no switch), and the V5 reactive-switchover ladder (error class -> action,
next-runtime selection).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import DispatchNote
from eawf.runtime.runtimes.adapter import ALL_ERROR_CLASSES
from eawf.runtime.runtimes.fallback import (
    FallbackAction,
    fall_back_to_fresh,
    fallback_action,
    next_runtime_on_error,
    switch_runtime_annotation,
)

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (c) F-d02 resolution failure -> fresh per V8
# ---------------------------------------------------------------------------


def test_fall_back_to_fresh_builds_continue_failed_annotation() -> None:
    """C04d F-d02: the V8 fall-through annotation degrades to fresh."""
    annotation = fall_back_to_fresh(
        attempt=2,
        runtime="claude-code",
        occurred_at=_now(),
        reason="session log expired",
    )
    assert annotation.note is DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH
    assert annotation.attempt == 2
    # No runtime switch on a V8 fall-through — same runtime, fresh session.
    assert annotation.runtime_from is None
    assert annotation.runtime_to == "claude-code"
    assert annotation.reason == "session log expired"


def test_fall_back_to_fresh_reason_optional() -> None:
    """The fall-through reason is optional (None when the caller omits it)."""
    annotation = fall_back_to_fresh(attempt=1, runtime="codex", occurred_at=_now())
    assert annotation.reason is None
    assert annotation.runtime_to == "codex"


# ---------------------------------------------------------------------------
# V5 reactive-switchover ladder
# ---------------------------------------------------------------------------


def test_fallback_action_rate_limit_retries_same() -> None:
    """Rate-limit retries the same runtime once before switching (C07a §5.5)."""
    assert fallback_action("RUNTIME_RATE_LIMIT") is FallbackAction.RETRY_SAME


@pytest.mark.parametrize(
    "error_class",
    ["RUNTIME_SERVER_ERROR", "RUNTIME_TIMEOUT", "RUNTIME_API_ERROR"],
)
def test_fallback_action_availability_errors_switch(error_class: str) -> None:
    """Availability errors fall through to the next runtime immediately."""
    assert fallback_action(error_class) is FallbackAction.SWITCH_RUNTIME  # type: ignore[arg-type]


def test_fallback_action_auth_halts() -> None:
    """Auth failure halts with BLOCKED — never falls through."""
    assert fallback_action("RUNTIME_AUTH_ERROR") is FallbackAction.HALT


def test_fallback_policy_total_over_all_error_classes() -> None:
    """Every canonical error class has exactly one fallback action."""
    actions = {fallback_action(ec) for ec in ALL_ERROR_CLASSES}
    assert actions <= set(FallbackAction)
    # Totality: no error class raises KeyError.
    for ec in ALL_ERROR_CLASSES:
        assert fallback_action(ec) in FallbackAction


def test_next_runtime_on_error_advances_preference_on_switch() -> None:
    """A switch picks the next distinct runtime in the preference ladder."""
    nxt = next_runtime_on_error(
        failed_runtime="claude-code",
        preference=["claude-code", "codex", "opencode"],
        error_class="RUNTIME_SERVER_ERROR",
    )
    assert nxt == "codex"


def test_next_runtime_on_error_none_on_halt() -> None:
    """Auth failure never switches — no next runtime."""
    nxt = next_runtime_on_error(
        failed_runtime="claude-code",
        preference=["claude-code", "codex"],
        error_class="RUNTIME_AUTH_ERROR",
    )
    assert nxt is None


def test_next_runtime_on_error_none_on_retry_same() -> None:
    """Rate-limit retries the same runtime first, so no switch yet."""
    nxt = next_runtime_on_error(
        failed_runtime="claude-code",
        preference=["claude-code", "codex"],
        error_class="RUNTIME_RATE_LIMIT",
    )
    assert nxt is None


def test_next_runtime_on_error_none_when_ladder_exhausted() -> None:
    """A switch off the last runtime in the ladder yields None."""
    nxt = next_runtime_on_error(
        failed_runtime="opencode",
        preference=["claude-code", "codex", "opencode"],
        error_class="RUNTIME_TIMEOUT",
    )
    assert nxt is None


def test_switch_runtime_annotation_records_switch_on_error() -> None:
    """A V5 switch annotation carries both runtimes + SWITCH_ON_ERROR."""
    annotation = switch_runtime_annotation(
        attempt=2,
        runtime_from="claude-code",
        runtime_to="codex",
        occurred_at=_now(),
        reason="RUNTIME_SERVER_ERROR",
    )
    assert annotation.note is DispatchNote.SWITCH_ON_ERROR
    assert annotation.runtime_from == "claude-code"
    assert annotation.runtime_to == "codex"
    assert annotation.attempt == 2
