"""Tests for the Claude session-transcript counter aggregator (P30-I25-W25).

The Stop hook payload carries no counters -- only a ``transcript_path``. These
tests pin what the aggregator makes of that file: deduplicated token tallies,
summed turn durations, the billed model id, a token-derived cost, and a
fail-open ``None`` whenever the transcript is missing, unreadable, or carries
nothing measurable.

The fixture transcript (``tests/runtime/hooks/fixtures/claude_session_transcript.jsonl``)
is a scrubbed extract of a REAL Claude Code session JSONL -- the shape the
hand-written statusline fixtures never matched.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.transcript_counters import (
    aggregate_transcript_counters,
    projects_root,
    transcript_path_for_session,
)

_TRANSCRIPT_FIXTURE = (
    Path(__file__).parents[1] / "runtime" / "hooks" / "fixtures" / "claude_session_transcript.jsonl"
)


def test_aggregate_real_transcript_fixture() -> None:
    counters = aggregate_transcript_counters(_TRANSCRIPT_FIXTURE)

    assert counters is not None
    # Both duration fields carry the same figure -- the convention the headless
    # spawn snapshot uses. Every gap between this fixture's rows is inside a turn
    # (none exceeds the idle ceiling), so the summed working time is the full
    # 593_177 ms and it exceeds the single turn-duration row (502_968 ms); the
    # aggregator reports the larger of the two monotonic measures.
    assert counters.api_duration_ms == 593_177
    assert counters.total_duration_ms == 593_177
    assert counters.input_tokens == 4
    assert counters.output_tokens == 1_013
    assert counters.cache_creation_input_tokens == 67_527
    assert counters.cache_read_input_tokens == 61_868
    assert counters.harness == "claude-code"
    assert counters.model == "claude-opus-4-8"
    assert counters.cost_usd is not None
    assert counters.cost_usd > Decimal("0")


def test_aggregate_dedupes_repeated_usage_rows(tmp_path: Path) -> None:
    """One billed message repeated across content-block rows counts once."""
    row = {
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(row) for _ in range(4)) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.input_tokens == 10
    assert counters.output_tokens == 20


def test_aggregate_sums_distinct_messages_and_durations(tmp_path: Path) -> None:
    rows = [
        {
            "type": "assistant",
            "message": {
                "id": "msg_01",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_02",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        },
        {"type": "system", "subtype": "turn_duration", "durationMs": 1_000},
        {"type": "system", "subtype": "turn_duration", "durationMs": 2_500},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.input_tokens == 15
    assert counters.output_tokens == 27
    assert counters.api_duration_ms == 3_500


def _assistant_row(message_id: str, *, at: str, output_tokens: int = 10) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {
            "id": message_id,
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1, "output_tokens": output_tokens},
        },
    }


def test_duration_falls_back_to_the_timestamp_span(tmp_path: Path) -> None:
    """A live session has no turn_duration row yet -- the row lands after the Stop hook.

    This is the defect the first live run exposed: capture read the transcript
    mid-turn, found no ``durationMs``, and reported a zero duration, so the
    API_DURATION EU basis still produced elapsed_eu=0.
    """
    rows = [
        _assistant_row("msg_0001", at="2026-07-13T00:00:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T00:05:00.000Z"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 300_000
    assert counters.total_duration_ms == 300_000


def test_duration_is_monotonic_as_the_transcript_grows(tmp_path: Path) -> None:
    """A later capture never reports a smaller duration than an earlier one.

    A backwards counter would raise ``LifecycleError`` at close, so the reported
    duration must be the max of the two monotonic measures (turn-duration sum and
    timestamp span) rather than whichever one happens to be present.
    """
    path = tmp_path / "t.jsonl"
    early_rows = [
        _assistant_row("msg_0001", at="2026-07-13T00:00:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T00:10:00.000Z"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in early_rows) + "\n", encoding="utf-8")
    early = aggregate_transcript_counters(path)

    # The turn ends: the turn_duration row finally lands (a smaller figure than
    # the span, because the span also covers the operator's think time).
    late_rows = [
        *early_rows,
        {
            "type": "system",
            "subtype": "turn_duration",
            "durationMs": 120_000,
            "timestamp": "2026-07-13T00:10:05.000Z",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in late_rows) + "\n", encoding="utf-8")
    late = aggregate_transcript_counters(path)

    assert early is not None
    assert late is not None
    assert early.api_duration_ms == 600_000
    assert late.api_duration_ms >= early.api_duration_ms


def test_duration_uses_turn_rows_when_rows_carry_no_timestamp(tmp_path: Path) -> None:
    rows = [
        {"type": "system", "subtype": "turn_duration", "durationMs": 7_000},
        {
            "type": "assistant",
            "message": {
                "id": "msg_0001",
                "model": "claude-opus-4-8",
                "usage": {"output_tokens": 5},
            },
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 7_000


def test_aggregate_prices_cache_write_ttl_split(tmp_path: Path) -> None:
    """The 1-hour cache-write tier prices above the 5-minute tier for the same tokens."""

    def _cost(ttl_key: str) -> Decimal:
        row = {
            "type": "assistant",
            "message": {
                "id": "msg_01",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 1_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {ttl_key: 1_000},
                },
            },
        }
        path = tmp_path / f"{ttl_key}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        counters = aggregate_transcript_counters(path)
        assert counters is not None
        assert counters.cost_usd is not None
        return counters.cost_usd

    assert _cost("ephemeral_1h_input_tokens") > _cost("ephemeral_5m_input_tokens")


def test_aggregate_prices_unsplit_cache_creation(tmp_path: Path) -> None:
    """A row with no TTL split still bills its cache-write tokens (at the 5m rate)."""
    row = {
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "model": "claude-opus-4-8",
            "usage": {"cache_creation_input_tokens": 1_000},
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.cache_creation_input_tokens == 1_000
    assert counters.cost_usd is not None
    assert counters.cost_usd > Decimal("0")


def test_aggregate_unpriced_model_keeps_counters_with_null_cost(tmp_path: Path) -> None:
    row = {
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "model": "some-unknown-vendor-model",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.cost_usd is None
    assert counters.input_tokens == 10
    assert counters.model == "some-unknown-vendor-model"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json\n",
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n',
    ],
    ids=["empty", "malformed", "no-usable-counter"],
)
def test_aggregate_fails_open_on_useless_transcript(tmp_path: Path, content: str) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(content, encoding="utf-8")

    assert aggregate_transcript_counters(path) is None


def test_aggregate_missing_transcript_returns_none(tmp_path: Path) -> None:
    assert aggregate_transcript_counters(tmp_path / "absent.jsonl") is None
    assert aggregate_transcript_counters(None) is None


def test_aggregate_unreadable_transcript_returns_none(tmp_path: Path) -> None:
    # A directory at the transcript path raises OSError on open -- fail open.
    (tmp_path / "dir.jsonl").mkdir()

    assert aggregate_transcript_counters(tmp_path / "dir.jsonl") is None


def test_transcript_path_for_session_resolves_via_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "-workspace-proj"
    project.mkdir()
    transcript = project / "sess-01.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    assert projects_root() == tmp_path
    assert transcript_path_for_session("sess-01", cwd=Path("/workspace/proj")) == transcript


def test_transcript_path_for_session_globs_when_cwd_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "-workspace-other"
    project.mkdir()
    transcript = project / "sess-01.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    # The session ran from a different working directory (a worktree, say); the
    # glob still finds it.
    assert transcript_path_for_session("sess-01", cwd=Path("/workspace/proj")) == transcript


def test_transcript_path_for_session_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path))

    assert transcript_path_for_session("sess-missing", cwd=Path("/workspace/proj")) is None
    assert transcript_path_for_session("") is None


# --- P30-I25-W34: EU is agent work time, not the operator's wall clock ------


def test_idle_gap_between_turns_is_not_counted_as_work(tmp_path: Path) -> None:
    """A wave left claimed overnight must not bank EU for the hours nobody worked.

    The shipped duration was the first-to-last row SPAN, which is the operator's
    wall clock: it counted reading, thinking, and sleeping. `compute_runtime_delta`
    forbids exactly that ("elapsed_eu is the AGENT-RUNTIME basis ... NOT the
    claim->close wall-clock"). Two short turns either side of an overnight gap
    must report only the two turns.
    """
    rows = [
        # Turn one: two minutes of work.
        _assistant_row("msg_0001", at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:02:00.000Z"),
        # ... the operator goes home. Fourteen hours pass.
        _assistant_row("msg_0003", at="2026-07-13T23:00:00.000Z"),
        _assistant_row("msg_0004", at="2026-07-13T23:03:00.000Z"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    # Two minutes plus three minutes of work -- not the fourteen-hour span.
    assert counters.api_duration_ms == 300_000


def test_long_in_turn_tool_run_still_counts_as_work(tmp_path: Path) -> None:
    """A ten-minute test run inside a turn is the agent working, not idling."""
    rows = [
        _assistant_row("msg_0001", at="2026-07-13T09:00:00.000Z"),
        # The agent kicks off the suite and waits ten minutes for it.
        _assistant_row("msg_0002", at="2026-07-13T09:10:00.000Z"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 600_000


def test_work_time_stays_monotonic_across_an_idle_gap(tmp_path: Path) -> None:
    """Resuming after an idle gap only ever ADDS duration -- never subtracts.

    A backwards counter raises at close, so the measure must not shrink when the
    transcript grows across a turn boundary.
    """
    path = tmp_path / "t.jsonl"
    first_turn = [
        _assistant_row("msg_0001", at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:05:00.000Z"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in first_turn) + "\n", encoding="utf-8")
    before = aggregate_transcript_counters(path)

    resumed = [*first_turn, _assistant_row("msg_0003", at="2026-07-14T09:00:00.000Z")]
    path.write_text("\n".join(json.dumps(r) for r in resumed) + "\n", encoding="utf-8")
    after = aggregate_transcript_counters(path)

    assert before is not None
    assert after is not None
    assert before.api_duration_ms == 300_000
    # The day-long gap adds nothing, but nothing is lost either.
    assert after.api_duration_ms >= before.api_duration_ms
    assert after.api_duration_ms == 300_000
