"""A turn straddling a wave claim is split at the claim instant.

The claim runs as a tool call inside a turn, and Claude writes that turn's
``turn_duration`` row only when the turn ends -- after the claim-time snapshot.
Differencing a later capture against that snapshot charged the wave for the whole
turn, the minutes before its claim included. These tests pin the as-of reading of
a transcript (tokens by row timestamp, the straddling turn pro-rated) and the
capture-time settlement that raises the claim baseline by the pre-claim share.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import RuntimeBaseline, RuntimeLatest, Wave
from eawf.runtime.daemon.methods.state_runtime import settle_straddling_claim
from eawf.runtime.runtimes.claude.transcript_counters import (
    MEASURE_VERSION,
    aggregate_transcript_counters,
)
from eawf.runtime.session.vendor_id import hash_vendor_session_id

_SESSION = hash_vendor_session_id("sess-placeholder-prorate")

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the wave records a captured runtime at close",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the wave records a captured runtime at close",
}


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-01-05T{clock}+00:00")


def _stamp(clock: str) -> str:
    return f"2026-01-05T{clock}.000Z"


def _prompt(clock: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": "go"}}
    if clock is not None:
        row["timestamp"] = _stamp(clock)
    return row


def _reply(clock: str | None, message_id: str, output_tokens: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1, "output_tokens": output_tokens},
        },
    }
    if clock is not None:
        row["timestamp"] = _stamp(clock)
    return row


def _turn(clock: str, duration_ms: int) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": duration_ms,
        "timestamp": _stamp(clock),
    }


def _earlier_turn() -> list[dict[str, Any]]:
    """A turn completed well before any claim: 100 s of work, 10 output tokens."""
    return [
        _prompt("09:50:00"),
        _reply("09:51:00", "msg_early", 10),
        _turn("09:52:00", 100_000),
    ]


def _straddling_turn() -> list[dict[str, Any]]:
    """10:00 to 10:04, 200 s of work, a 100-token reply at 10:01 and a 50-token one at 10:03."""
    return [
        _prompt("10:00:00"),
        _reply("10:01:00", "msg_a", 100),
        _reply("10:03:00", "msg_b", 50),
        _turn("10:04:00", 200_000),
    ]


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return _write(tmp_path, _earlier_turn() + _straddling_turn())


def test_straddling_turn_split_at_claim_by_event_timestamps(transcript: Path) -> None:
    """Claim at 10:02: the last pre-claim event is 10:01, a quarter into the 10:00-10:04 turn."""
    before = aggregate_transcript_counters(transcript, as_of=_at("10:02:00"))
    full = aggregate_transcript_counters(transcript)

    assert before is not None
    assert full is not None
    assert before.api_duration_ms == 100_000 + 50_000
    assert before.total_duration_ms == before.api_duration_ms
    assert full.api_duration_ms == 300_000
    # The wave is charged only the post-claim share of the straddling turn.
    assert full.api_duration_ms - before.api_duration_ms == 150_000


def test_straddling_turn_excludes_pre_claim_token_events(transcript: Path) -> None:
    before = aggregate_transcript_counters(transcript, as_of=_at("10:02:00"))
    full = aggregate_transcript_counters(transcript)

    assert before is not None
    assert full is not None
    assert before.output_tokens == 10 + 100
    assert full.output_tokens - before.output_tokens == 50


def test_straddling_turn_claim_before_turn_start(transcript: Path) -> None:
    before = aggregate_transcript_counters(transcript, as_of=_at("09:59:00"))

    assert before is not None
    assert before.api_duration_ms == 100_000
    assert before.output_tokens == 10


def test_straddling_turn_claim_before_any_row_reads_nothing(transcript: Path) -> None:
    assert aggregate_transcript_counters(transcript, as_of=_at("09:00:00")) is None


def test_straddling_turn_claim_after_turn_end(transcript: Path) -> None:
    before = aggregate_transcript_counters(transcript, as_of=_at("10:05:00"))
    full = aggregate_transcript_counters(transcript)

    assert before == full


def test_straddling_turn_claim_exactly_at_an_event(transcript: Path) -> None:
    """An event stamped at the claim instant counts as before it."""
    before = aggregate_transcript_counters(transcript, as_of=_at("10:01:00"))

    assert before is not None
    assert before.api_duration_ms == 100_000 + 50_000
    assert before.output_tokens == 10 + 100


def test_straddling_turn_claim_exactly_at_turn_end(transcript: Path) -> None:
    before = aggregate_transcript_counters(transcript, as_of=_at("10:04:00"))

    assert before is not None
    assert before.api_duration_ms == 300_000


def test_straddling_turn_claim_at_turn_start_charges_it_all_to_the_wave(transcript: Path) -> None:
    """Only the opening prompt precedes the claim, so the pre-claim share is zero."""
    before = aggregate_transcript_counters(transcript, as_of=_at("10:00:00"))

    assert before is not None
    assert before.api_duration_ms == 100_000


def test_straddling_turn_zero_length_turn_falls_on_one_side(tmp_path: Path) -> None:
    """A turn whose rows share one stamp cannot straddle: it is wholly before or after."""
    rows = [
        *_earlier_turn(),
        _prompt("10:00:00"),
        _reply("10:00:00", "msg_z", 5),
        _turn("10:00:00", 0),
    ]
    path = _write(tmp_path, rows)

    at_stamp = aggregate_transcript_counters(path, as_of=_at("10:00:00"))
    just_before = aggregate_transcript_counters(
        path, as_of=datetime(2026, 1, 5, 9, 59, 59, tzinfo=UTC)
    )

    assert at_stamp is not None
    assert just_before is not None
    assert at_stamp.output_tokens == 15
    assert just_before.output_tokens == 10
    assert at_stamp.api_duration_ms == just_before.api_duration_ms == 100_000


def test_straddling_turn_zero_duration_contributes_nothing(tmp_path: Path) -> None:
    rows = [
        *_earlier_turn(),
        _prompt("10:00:00"),
        _reply("10:03:00", "msg_c", 5),
        _turn("10:04:00", 0),
    ]
    before = aggregate_transcript_counters(_write(tmp_path, rows), as_of=_at("10:02:00"))

    assert before is not None
    assert before.api_duration_ms == 100_000


def test_straddling_turn_linear_split_without_pre_claim_stamps(tmp_path: Path) -> None:
    """No pre-claim row is stamped: the turn is its 120 s ending at 10:04, split linearly."""
    rows = [*_earlier_turn(), _prompt(None), _reply(None, "msg_u", 5), _turn("10:04:00", 120_000)]
    path = _write(tmp_path, rows)

    halfway = aggregate_transcript_counters(path, as_of=_at("10:03:00"))
    before_start = aggregate_transcript_counters(path, as_of=_at("10:01:00"))

    assert halfway is not None
    assert before_start is not None
    assert halfway.api_duration_ms == 100_000 + 60_000
    assert before_start.api_duration_ms == 100_000


def test_straddling_turn_interrupted_contributes_nothing(tmp_path: Path) -> None:
    rows = [
        *_earlier_turn(),
        _prompt("10:00:00"),
        _reply("10:01:00", "msg_i", 5),
        {
            "type": "user",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
            "timestamp": _stamp("10:03:00"),
        },
        _turn("10:04:00", 200_000),
    ]
    path = _write(tmp_path, rows)

    before = aggregate_transcript_counters(path, as_of=_at("10:02:00"))
    full = aggregate_transcript_counters(path)

    assert before is not None
    assert full is not None
    assert before.api_duration_ms == full.api_duration_ms == 100_000


def test_straddling_turn_naive_as_of_raises(transcript: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        aggregate_transcript_counters(transcript, as_of=datetime(2026, 1, 5, 10, 2))


# --- capture-time settlement of the claim baseline ---------------------------


def _wave(baseline: RuntimeBaseline) -> Wave:
    return Wave.model_validate(
        {
            "id": "P00-I01-W01",
            "iter_id": "P00-I01",
            "title": "Wave one",
            "status": WaveStatus.CLAIMED.value,
            "success_criteria": [_CRITERION],
            "opened_at": _at("09:00:00"),
            "runtime_baseline": baseline,
        }
    )


def _claim_baseline(**overrides: Any) -> RuntimeBaseline:
    """What the claim at 10:02 read: the straddling turn had no duration row yet."""
    fields: dict[str, Any] = {
        "api_duration_ms": 100_000,
        "total_duration_ms": 100_000,
        "input_tokens": 3,
        "output_tokens": 110,
        "session_id": _SESSION,
        "measure_version": MEASURE_VERSION,
        "captured_at": _at("10:02:00"),
    }
    fields.update(overrides)
    return RuntimeBaseline(**fields)


def _capture(api_duration_ms: int) -> RuntimeLatest:
    return RuntimeLatest(
        api_duration_ms=api_duration_ms,
        total_duration_ms=api_duration_ms,
        output_tokens=160,
        session_id=_SESSION,
        measure_version=MEASURE_VERSION,
        captured_at=_at("10:10:00"),
    )


def test_settle_straddling_claim_raises_baseline_by_pre_claim_share(transcript: Path) -> None:
    wave = _wave(_claim_baseline())

    settle_straddling_claim(wave, _capture(300_000), transcript=transcript)

    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 150_000
    assert wave.runtime_baseline.total_duration_ms == 150_000
    assert wave.runtime_baseline.output_tokens == 110
    assert wave.runtime_baseline.captured_at == _at("10:02:00")


def test_settle_straddling_claim_is_idempotent(transcript: Path) -> None:
    wave = _wave(_claim_baseline())

    settle_straddling_claim(wave, _capture(300_000), transcript=transcript)
    settle_straddling_claim(wave, _capture(300_000), transcript=transcript)

    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 150_000


def test_settle_straddling_claim_before_turn_ends_changes_nothing(tmp_path: Path) -> None:
    rows = _earlier_turn() + _straddling_turn()[:-1]
    wave = _wave(_claim_baseline())

    settle_straddling_claim(wave, _capture(100_000), transcript=_write(tmp_path, rows))

    assert wave.runtime_baseline == _claim_baseline()


def test_settle_straddling_claim_never_exceeds_the_capture(transcript: Path) -> None:
    wave = _wave(_claim_baseline())

    settle_straddling_claim(wave, _capture(120_000), transcript=transcript)

    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 120_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"measure_version": MEASURE_VERSION - 1},
        {"session_id": hash_vendor_session_id("sess-placeholder-other")},
    ],
)
def test_settle_straddling_claim_skips_an_incomparable_baseline(
    transcript: Path, overrides: dict[str, Any]
) -> None:
    baseline = _claim_baseline(**overrides)
    wave = _wave(baseline)

    settle_straddling_claim(wave, _capture(300_000), transcript=transcript)

    assert wave.runtime_baseline == baseline


def test_settle_straddling_claim_without_transcript_changes_nothing() -> None:
    wave = _wave(_claim_baseline())

    settle_straddling_claim(wave, _capture(300_000), transcript=None)

    assert wave.runtime_baseline == _claim_baseline()
