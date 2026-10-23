"""The usage fold counts each Claude message at its latest reading.

The Claude SDK repeats one assistant message once per content block, and
the first copy carries a streaming snapshot whose output count is near
zero. The fold must count the message's final usage, not its first copy.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from eawf.runtime.runtimes.claude.sdk_launcher import _UsageFold
from eawf.runtime.runtimes.metering import UsageSample


def _assistant(usage: Any, message_id: str | None = "m1") -> AssistantMessage:
    """Return one assistant message copy reading *usage*."""
    return AssistantMessage(
        content=[TextBlock(text="x")], model="fixture-model", usage=usage, message_id=message_id
    )


def _result(usage: Any) -> ResultMessage:
    """Return a turn result reporting *usage* as the turn total."""
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        usage=usage,
    )


def _usage(input_tokens: int, output_tokens: int, cache_read: int) -> dict[str, int]:
    """Return a usage block with the three folded counts."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
    }


def test_fold_counts_the_latest_reading_of_one_message() -> None:
    fold = _UsageFold()

    readings = [fold.observe(_assistant(_usage(3, out, 50_000))) for out in (1, 1, 412)]

    assert readings[0] == UsageSample(
        input_tokens=3, output_tokens=1, cache_read_input_tokens=50_000
    )
    assert readings[1] is None
    assert readings[2] == UsageSample(
        input_tokens=3, output_tokens=412, cache_read_input_tokens=50_000
    )


def test_fold_edges_repeat_with_no_increase_returns_none() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 412, 50_000)))

    assert fold.observe(_assistant(_usage(3, 412, 50_000))) is None


def test_fold_edges_distinct_messages_sum() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 1, 100), "m1"))
    fold.observe(_assistant(_usage(3, 40, 100), "m1"))

    total = fold.observe(_assistant(_usage(5, 7, 200), "m2"))

    assert total == UsageSample(input_tokens=8, output_tokens=47, cache_read_input_tokens=300)


def test_fold_edges_lower_reading_never_lowers_the_total() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 412, 50_000)))

    assert fold.observe(_assistant(_usage(3, 1, 50_000))) is None
    later = fold.observe(_assistant(_usage(4, 413, 50_000)))

    assert later == UsageSample(input_tokens=4, output_tokens=413, cache_read_input_tokens=50_000)


def test_fold_edges_mixed_reading_folds_only_the_increase() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 412, 50_000)))

    total = fold.observe(_assistant(_usage(3, 1, 50_010)))

    assert total == UsageSample(input_tokens=3, output_tokens=412, cache_read_input_tokens=50_010)


def test_fold_edges_malformed_usage_returns_none() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 1, 0)))

    assert fold.observe(_assistant({"output_tokens": -5})) is None
    assert fold.observe(_assistant({"output_tokens": "many"})) is None
    assert fold.observe(_assistant(["not", "a", "dict"])) is None


def test_fold_edges_result_adopted_only_above_the_sum() -> None:
    fold = _UsageFold()
    fold.observe(_assistant(_usage(3, 1, 0), "m1"))
    fold.observe(_assistant(_usage(3, 412, 0), "m1"))

    assert fold.observe(_result(_usage(3, 400, 0))) is None
    assert fold.observe(_result(_usage(3, 500, 0))) == UsageSample(
        input_tokens=3, output_tokens=500, cache_read_input_tokens=0
    )
