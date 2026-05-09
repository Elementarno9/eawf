"""Unit tests for :class:`HookRunner` (Phase 4 W04).

The runner contract per design spec §3.3 / acceptance §3:

- Hooks fire in registration order.
- ``block=True`` propagates back to the caller (CLI exit 9 mapping is
  tested in ``test_cli_hook_run.py``).
- An exception inside a hook is captured into a :class:`HookResult`
  with ``raised=True`` and ``output=repr(exc)``; the runner never
  propagates it so other hooks still execute.
- Empty bucket → empty result list (the no-block / no-hook path).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.hooks.event import HookEvent, HookEventType
from eawf.hooks.runner import HookResult, HookRunner, _coerce_result


def _event(event_type: HookEventType = HookEventType.PRE_COMMIT) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="P04-I01-W04",
        command="eawf wave close",
        args={},
        runtime="generic",
        occurred_at=datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
        payloads={},
    )


def test_runner_no_hooks_returns_empty_list() -> None:
    runner = HookRunner()
    assert runner.run_event(_event()) == []


def test_runner_dispatches_in_registration_order() -> None:
    runner = HookRunner()
    fired: list[str] = []

    def hook_a(event: HookEvent) -> tuple[bool, str]:
        fired.append("a")
        return False, "ok-a"

    def hook_b(event: HookEvent) -> tuple[bool, str]:
        fired.append("b")
        return False, "ok-b"

    runner.register(HookEventType.PRE_COMMIT, hook_a, name="a")
    runner.register(HookEventType.PRE_COMMIT, hook_b, name="b")

    results = runner.run_event(_event())
    assert fired == ["a", "b"]
    assert [r.name for r in results] == ["a", "b"]
    assert all(not r.block for r in results)
    assert results[0].output == "ok-a"
    assert all(r.duration_ms >= 0.0 for r in results)


def test_runner_only_dispatches_to_matching_event_type() -> None:
    runner = HookRunner()

    def commit_hook(event: HookEvent) -> tuple[bool, str]:
        return False, "ran"

    runner.register(HookEventType.PRE_COMMIT, commit_hook, name="commit")
    # Wrong event type → bucket is empty.
    assert runner.run_event(_event(HookEventType.SESSION_START)) == []


def test_runner_block_signal_propagates_to_result() -> None:
    runner = HookRunner()

    def blocker(event: HookEvent) -> tuple[bool, str]:
        return True, "fail-closed: lint dirty"

    runner.register(HookEventType.PRE_COMMIT, blocker, name="lint")
    results = runner.run_event(_event())
    assert len(results) == 1
    assert results[0].block is True
    assert results[0].output == "fail-closed: lint dirty"
    assert results[0].raised is False


def test_runner_exception_captured_does_not_crash_loop() -> None:
    runner = HookRunner()

    def boom(event: HookEvent) -> tuple[bool, str]:
        raise RuntimeError("instrument missing")

    def normal(event: HookEvent) -> tuple[bool, str]:
        return False, "ok"

    runner.register(HookEventType.PRE_COMMIT, boom, name="boom")
    runner.register(HookEventType.PRE_COMMIT, normal, name="normal")

    results = runner.run_event(_event())
    assert len(results) == 2
    assert results[0].name == "boom"
    assert results[0].raised is True
    assert results[0].block is False
    assert "instrument missing" in results[0].output
    # The follow-up hook still fires.
    assert results[1].name == "normal"
    assert results[1].raised is False
    assert results[1].output == "ok"


def test_runner_hook_returning_none_yields_default_result() -> None:
    runner = HookRunner()

    def silent(event: HookEvent) -> None:
        return None

    runner.register(HookEventType.PRE_COMMIT, silent, name="silent")
    results = runner.run_event(_event())
    assert len(results) == 1
    assert results[0].name == "silent"
    assert results[0].block is False
    assert results[0].output == ""


def test_runner_hook_returning_hook_result_overrides_duration() -> None:
    runner = HookRunner()

    def returns_result(event: HookEvent) -> HookResult:
        return HookResult(
            name="ignored-when-coerced",
            block=False,
            output="explicit",
            duration_ms=999.0,
            raised=False,
        )

    runner.register(HookEventType.PRE_COMMIT, returns_result, name="explicit")
    results = runner.run_event(_event())
    assert results[0].output == "explicit"
    # Runner-recorded duration overrides the hook-supplied value.
    assert results[0].duration_ms != 999.0
    # Runner-recorded name overrides the model name.
    assert results[0].name == "explicit"


def test_runner_register_rejects_duplicate_name() -> None:
    runner = HookRunner()

    def lint(event: HookEvent) -> tuple[bool, str]:
        return False, "ok"

    runner.register(HookEventType.PRE_COMMIT, lint, name="lint")
    with pytest.raises(ValueError, match="already registered"):
        runner.register(HookEventType.PRE_COMMIT, lint, name="lint")


def test_runner_hooks_for_yields_registered_pairs() -> None:
    runner = HookRunner()

    def lint(event: HookEvent) -> tuple[bool, str]:
        return False, "ok"

    runner.register(HookEventType.PRE_COMMIT, lint, name="lint")
    pairs = list(runner.hooks_for(HookEventType.PRE_COMMIT))
    assert pairs == [("lint", lint)]


def test_runner_coerce_result_rejects_bad_tuple_shapes() -> None:
    """Direct ``_coerce_result`` rejects anything other than the documented shapes."""
    with pytest.raises(TypeError, match="non-bool"):
        _coerce_result("hook", ("yes", "ok"), 0.0)
    with pytest.raises(TypeError, match="non-str"):
        _coerce_result("hook", (False, 42), 0.0)
    with pytest.raises(TypeError, match="unsupported type"):
        _coerce_result("hook", 123, 0.0)
