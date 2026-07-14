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
    # Claude's own measure of the turn (502_968 ms), not the 593_177 ms span across
    # the fixture's rows. The span is the wall clock: it would include any wait for
    # the operator to approve a tool.
    assert counters.api_duration_ms == 502_968
    assert counters.total_duration_ms == 502_968
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
    # Timestamped, because a real transcript's rows are: the duration is bounded by
    # the lifetime the rows demonstrate, and rows that demonstrate none bound it to
    # zero (see test_a_transcript_that_shows_no_lifetime_justifies_no_duration).
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-07-13T09:00:00.000Z",
            "message": {
                "id": "msg_01",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-13T09:00:02.000Z",
            "message": {
                "id": "msg_02",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-07-13T09:00:03.000Z",
            "durationMs": 1_000,
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-07-13T09:00:10.000Z",
            "durationMs": 2_500,
        },
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


def test_duration_only_grows_as_turn_rows_land(tmp_path: Path) -> None:
    """A later capture never reports a smaller duration than an earlier one.

    A backwards counter re-origins the wave (dropping its measured runtime), so the
    measure must only ever grow. Summing turn rows is monotone by construction:
    rows are appended, never rewritten. The old measure was NOT -- a span-based
    figure fell the moment Claude's smaller one landed.
    """
    path = tmp_path / "t.jsonl"
    early_rows = [
        _prompt_row(at="2026-07-13T00:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T00:10:00.000Z"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in early_rows) + "\n", encoding="utf-8")
    early = aggregate_transcript_counters(path)

    late_rows = [
        *early_rows,
        _turn_duration_row(at="2026-07-13T00:10:05.000Z", ms=120_000),
    ]
    path.write_text("\n".join(json.dumps(r) for r in late_rows) + "\n", encoding="utf-8")
    late = aggregate_transcript_counters(path)

    assert early is not None
    assert late is not None
    assert early.api_duration_ms == 0
    assert late.api_duration_ms == 120_000
    assert late.api_duration_ms >= early.api_duration_ms


def test_a_transcript_that_shows_no_lifetime_justifies_no_duration(tmp_path: Path) -> None:
    """Rows carrying no timestamps demonstrate no lifetime, so they bound no runtime.

    This test used to assert the OPPOSITE -- that a timestamp-free transcript still
    reports its raw turn sum. That is the door the clamp was skipped through: with
    fewer than two timestamps there is no span, and the first cut of the ceiling
    (W49) responded by not clamping at all. The tokens still count; the duration
    does not.
    """
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
    assert counters.api_duration_ms == 0
    assert counters.output_tokens == 5  # the tokens are still real


def test_the_clamp_has_no_door_for_a_resumed_file_with_one_timestamp(tmp_path: Path) -> None:
    """The exact shape that walked through W49's ceiling.

    A resumed session's new file: Claude kills the abandoned agents and closes out
    the interrupted turn with its full wall clock (366,298,957 ms = 101.75 hours,
    the real value from a real transcript on this machine). If the only row bearing
    a parseable timestamp is that turn row, W49 computed no span and applied no
    clamp -- so the ceiling it shipped let through the very figure it was written to
    stop: 203.5 EU on one wave.

    Delete the ``< 2 stamps -> 0`` rule in ``_transcript_span_ms`` and this test
    reports 101.75 hours again.
    """
    rows = [
        {"type": "system", "subtype": "agents_killed"},
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-07-12T23:52:21.810Z",
            "durationMs": 366_298_957,
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_0001",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 0


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


# --- P30-I25-W43: Claude measures its own turns; stop re-deriving wall clock --


def _prompt_row(*, at: str) -> dict[str, object]:
    """An operator prompt -- a user row carrying TEXT. This starts a turn."""
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": "do the thing"}}


def _tool_result_row(*, at: str) -> dict[str, object]:
    """A tool handing its result back MID-TURN -- also a user row, but not a prompt."""
    return {
        "type": "user",
        "timestamp": at,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    }


def _turn_duration_row(*, at: str, ms: int) -> dict[str, object]:
    """Claude's own measure of a completed turn. It excludes approval stalls."""
    return {"type": "system", "subtype": "turn_duration", "timestamp": at, "durationMs": ms}


def test_a_twelve_hour_wait_for_tool_approval_is_not_agent_runtime(tmp_path: Path) -> None:
    """The operator sleeping at a permission prompt is not the agent working.

    When the agent asks to run a tool, Claude Code can put the request to the
    operator -- and the stall sits INSIDE the turn, between the assistant's
    `tool_use` row and the `tool_result` that follows. It has the identical shape
    to a long-running tool, so no span-based measure can tell them apart. In the
    session that produced this iter, one such stall was 12.9 HOURS of sleep, and
    the previous measure booked every minute of it as agent runtime.

    Claude measures the turn itself and excludes the wait. Read its number.
    """
    rows = [
        _prompt_row(at="2026-07-13T05:00:00.000Z"),
        {
            "type": "assistant",
            "timestamp": "2026-07-13T05:23:11.000Z",
            "message": {
                "id": "msg_0001",
                "model": "claude-opus-4-8",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 10},
            },
        },
        # ... the operator is asleep. 12.9 hours pass. Then they approve.
        _tool_result_row(at="2026-07-13T18:15:20.000Z"),
        _turn_duration_row(at="2026-07-13T18:16:00.000Z", ms=115 * 60_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    # Claude's figure: 115 minutes of work. NOT the 13-hour span of the turn.
    assert counters.api_duration_ms == 115 * 60_000


def test_an_interrupted_turn_contributes_no_runtime(tmp_path: Path) -> None:
    """The shape a CLAMP cannot catch, because the fabrication sits under the ceiling.

    This is the real 343dbe7f transcript's shape, reduced: the operator walks away
    while the agent works, comes back hours later and hits Esc. Claude kills the
    background agents and closes the turn out with its full WALL CLOCK -- 76.26 hours
    in the real file, 152.5 EU. That figure lies INSIDE the transcript's own span
    (the turn began after the file did), so the W49/W50 ceiling was a no-op on it:
    `min(sum, span)` returns the sum. Six real transcripts carry such a row; the
    clamp helped exactly one of them.

    An interrupted turn is not mis-measured, it is UNMEASURABLE -- Claude excludes
    the operator's waiting only for a turn it COMPLETES. So it contributes nothing,
    and the completed turn beside it still counts in full.

    Delete the interrupt check in `_scan_rows` and this test books the 4 hours.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:05:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:05:30.000Z", ms=300_000),  # a real 5-min turn
        _prompt_row(at="2026-07-13T09:10:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:11:00.000Z"),
        # ... the operator leaves. Four hours later they return and press Esc.
        {"type": "system", "subtype": "agents_killed", "timestamp": "2026-07-13T13:10:00.000Z"},
        _turn_duration_row(at="2026-07-13T13:10:01.000Z", ms=4 * 3_600_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    # The 4 hours are the operator's absence, and they sit comfortably inside the
    # transcript's own 4h10m span -- which is exactly why a ceiling cannot see them.
    assert counters.api_duration_ms == 300_000


def test_an_interrupt_without_background_agents_is_still_an_interrupt(tmp_path: Path) -> None:
    """Esc with no background agents running leaves no ``agents_killed`` row.

    Claude then marks the interruption in the message itself. Both markers sit in the
    row immediately BEFORE the closeout, on all 16 such rows across the 330 real
    transcripts on this machine -- so the rule keys on the interrupt, not on the
    agents. Keying on ``agents_killed`` alone would have missed 10 of the 11
    interrupts observed here.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:01:00.000Z"),
        {
            "type": "user",
            "timestamp": "2026-07-13T11:00:00.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        },
        _turn_duration_row(at="2026-07-13T11:00:01.000Z", ms=2 * 3_600_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 0


def test_the_span_is_never_substituted_for_a_measurement(tmp_path: Path) -> None:
    """When the ceiling breaks, report nothing -- not the operator's clock.

    Substituting the transcript's span ships MEASURE_VERSION 1 ("the operator's
    clock, idle included") under a later version number, which does not make it a
    measurement -- it makes it an undetectable one. Three real transcripts landed
    43.6, 38.1 and 18.8 EU that way through the W49 clamp.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:01:00.000Z"),
        # A completed turn claiming more time than the transcript has existed: data
        # this code does not understand.
        _turn_duration_row(at="2026-07-13T09:01:30.000Z", ms=10 * 3_600_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 0  # not the 90-second span, and not the 10 hours


def test_an_ordinary_session_is_not_clamped(tmp_path: Path) -> None:
    """Boundary: the ceiling must not shave a session that is behaving.

    Turns sum to 20 minutes inside a 40.5-minute transcript, so the clamp is
    inactive and the measure passes through unchanged. A ceiling that quietly
    rewrote honest data would be its own defect.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:10:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:10:30.000Z", ms=600_000),
        _prompt_row(at="2026-07-13T09:30:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:40:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:40:30.000Z", ms=600_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 1_200_000  # both turns, in full


def test_duration_is_read_only_off_the_turn_duration_rows(tmp_path: Path) -> None:
    """A ``durationMs`` on any other row type is not the measure.

    The sum used to add ``durationMs`` from EVERY row, which is correct today only
    by luck of the schema -- no other row carries one. The day a Claude Code release
    puts a duration on some other row (a ``stop_hook_summary`` row already exists),
    the measure inflates, and per the ceiling test above nothing downstream catches
    an inflation.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:10:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:10:30.000Z", ms=600_000),
        # A future row type that happens to carry the field. It is not a turn.
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-07-13T09:10:40.000Z",
            "durationMs": 300_000,
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 600_000  # the turn only, not the hook row


def test_summed_turn_durations_are_the_measure(tmp_path: Path) -> None:
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:02:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:02:30.000Z", ms=120_000),
        _prompt_row(at="2026-07-13T09:30:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:33:00.000Z"),
        _turn_duration_row(at="2026-07-13T09:33:30.000Z", ms=180_000),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    # The two turns Claude measured -- and not the 30 minutes the operator spent
    # reading between them.
    assert counters.api_duration_ms == 300_000


def test_an_unmeasured_turn_reports_zero_rather_than_a_guess(tmp_path: Path) -> None:
    """No turn row yet -- so no duration. There is deliberately NO span fallback.

    Spanning the in-flight turn would buy one capture of coverage and cost both
    defects back: it charges that turn's approval stalls, and it makes the duration
    DROP when Claude's smaller figure lands, which reads as a counter reset and
    re-origins the wave. Nothing is lost by waiting -- the row arrives before the
    next capture, so the turn is counted then. The tokens are still captured here.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:05:00.000Z"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)

    assert counters is not None
    assert counters.api_duration_ms == 0
    assert counters.output_tokens == 10


def test_out_of_order_rows_cannot_make_turns_overlap(tmp_path: Path) -> None:
    """Transcript rows are NOT written chronologically; grouping in file order lies.

    Live corpus: one transcript reported 1633 minutes of turn spans against a 1308
    minute wall clock -- 125% of the time the session had existed -- because rows
    interleave out of order and two turns' spans overlapped.
    """
    rows = [
        _prompt_row(at="2026-07-13T09:00:00.000Z"),
        _assistant_row("msg_0001", at="2026-07-13T09:10:00.000Z"),
        _prompt_row(at="2026-07-13T09:20:00.000Z"),
        # An out-of-order row: written here, but timestamped back in turn one.
        _tool_result_row(at="2026-07-13T09:05:00.000Z"),
        _assistant_row("msg_0002", at="2026-07-13T09:25:00.000Z"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counters = aggregate_transcript_counters(path)
    wall_clock_ms = 25 * 60_000

    assert counters is not None
    assert counters.api_duration_ms <= wall_clock_ms
