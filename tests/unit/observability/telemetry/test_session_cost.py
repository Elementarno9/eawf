"""A session's cost is the sum of its requests' costs, never its totals priced as one request.

Summed tokens priced as one request cross a tier no request reached, bill
every request at one date's rates and guess one write class for every write.
These tests pin the per-request sum on each path that prices a session: the
telemetry adapters, the transcript counters and the spawn meter.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.telemetry.aggregator import roll_session
from eawf.observability.telemetry.exporter import _cost_family
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.pricing import PriceBook, TokenUsage, load_price_book
from eawf.observability.telemetry.sources import claude_session
from eawf.observability.telemetry.sources.claude_session import ClaudeSessionSource
from eawf.observability.telemetry.sources.codex_session import CodexSessionSource
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.runtimes.claude.transcript_counters import read_transcript
from eawf.runtime.runtimes.metering import price_spawn_result

_ON = date(2026, 11, 10)
_BOOK = load_price_book(repo_root=None, overrides={})


def _assistant(index: int, model: str, ts: str, **usage: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": "sess-cost",
        "uuid": f"u-{index}",
        "timestamp": ts,
        "message": {"id": f"msg_{index}", "model": model, "usage": usage},
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _haiku_session(tmp_path: Path) -> Path:
    """Twenty Haiku 5.5 requests of 50k cache read, 1k input and 500 output."""
    records = [
        _assistant(
            index,
            "claude-haiku-5-5",
            f"2026-11-10T10:{index:02d}:00Z",
            input_tokens=1_000,
            cache_read_input_tokens=50_000,
            output_tokens=500,
        )
        for index in range(20)
    ]
    return _write_jsonl(tmp_path / "sess-cost.jsonl", records)


def _rolled(path: Path) -> TelemetrySession:
    (session,) = ClaudeSessionSource().iter_rows(path)
    return roll_session(session, project_id="proj-1")


# ---- the telemetry session row -------------------------------------------------------


def test_session_cost_repro_totals_cross_the_haiku_tier(tmp_path: Path) -> None:
    """Each request stays under 100k prompt tokens; their 1.02M sum must not bill at tier rates."""
    rolled = _rolled(_haiku_session(tmp_path))

    assert rolled.total_input_tokens + rolled.total_cache_read == 1_020_000
    assert rolled.total_cost_usd == Decimal("0.017")


def test_session_cost_repro_openai_writes_zero_the_session(tmp_path: Path) -> None:
    """A Codex write names no priced class: that request is unpriced, the rest still bill."""
    records = [
        {"timestamp": "2026-11-10T09:00:00Z", "type": "session_meta", "payload": {"id": "cx"}},
        {
            "timestamp": "2026-11-10T09:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-6.1-sol"},
        },
        _token_count("2026-11-10T09:00:10Z", (10_000, 0, 2_000, 100), (10_000, 0, 2_000, 100)),
        _token_count("2026-11-10T09:01:10Z", (20_000, 8_000, 2_000, 200), (10_000, 8_000, 0, 100)),
    ]
    path = _write_jsonl(tmp_path / "rollout-cx.jsonl", records)
    (session,) = CodexSessionSource().iter_rows(path)
    rolled = roll_session(session, project_id="proj-1")

    # 2000 input x 2 + 8000 cached x 0.10 + 100 output x 10, per million.
    assert rolled.total_cost_usd == Decimal("0.0058")


def _token_count(
    ts: str, total: tuple[int, int, int, int], last: tuple[int, int, int, int]
) -> dict[str, Any]:
    def usage(values: tuple[int, int, int, int]) -> dict[str, int]:
        gross, cached, writes, output = values
        return {
            "input_tokens": gross,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": writes,
            "output_tokens": output,
            "reasoning_output_tokens": 0,
        }

    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage(total), "last_token_usage": usage(last)},
        },
    }


def _dated_book(tmp_path: Path) -> PriceBook:
    """A book whose claude-test-1 input rate doubles on 2026-11-10."""
    directory = tmp_path / "prices"
    directory.mkdir()
    rows: list[tuple[str | None, str | None, int]] = [
        (None, "2026-11-10", 1),
        ("2026-11-10", None, 2),
    ]
    models: list[dict[str, object]] = [
        {
            "id": "claude-test-1",
            "match": [],
            "effective_from": start,
            "effective_to": end,
            "source_url": "https://example.com/pricing",
            "verified_on": "2026-11-10",
            "verified_by": "eye",
            "rates": {
                "input": rate,
                "output": rate,
                "cache_read": rate,
                "cache_write": {"5m": rate, "1h": rate},
            },
            "tiers": [],
            "modifiers": {"fast": None, "geo_us": None},
        }
        for start, end, rate in rows
    ]
    (directory / "anthropic.yaml").write_text(
        json.dumps({"vendor": "anthropic", "aliases": [], "models": models}), encoding="utf-8"
    )
    return load_price_book(repo_root=None, directory=directory, overrides={})


def test_session_cost_repro_prices_each_request_at_its_own_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _dated_book(tmp_path)
    monkeypatch.setattr(claude_session, "active_price_book", lambda: book, raising=False)
    path = _write_jsonl(
        tmp_path / "sess-dated.jsonl",
        [
            _assistant(1, "claude-test-1", "2026-11-09T12:00:00Z", input_tokens=1_000),
            _assistant(2, "claude-test-1", "2026-11-11T12:00:00Z", input_tokens=1_000),
        ],
    )

    assert _rolled(path).total_cost_usd == Decimal("0.003")


def test_session_with_no_priced_request_has_a_null_cost(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "sess-unpriced.jsonl",
        [_assistant(1, "claude-unknown-9", "2026-11-10T12:00:00Z", input_tokens=1_000)],
    )

    assert _rolled(path).total_cost_usd is None


def test_session_without_requests_has_a_null_cost(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "sess-empty.jsonl", [{"type": "user", "sessionId": "s"}])

    assert _rolled(path).total_cost_usd is None


def test_cost_counter_skips_unpriced_sessions() -> None:
    def session(session_id: str, runtime: str, cost: Decimal | None) -> TelemetrySession:
        return TelemetrySession(
            session_id=session_id,
            project_id="p",
            runtime=runtime,  # type: ignore[arg-type]
            wave_id=None,
            attempt_id=None,
            session_log_path="opaque://s",
            started_at=None,
            ended_at=None,
            duration_ms=None,
            model_primary=None,
            total_cost_usd=cost,
            end_marker="other",
        )

    family = _cost_family(
        [
            session("a", "claude", Decimal("0.5")),
            session("b", "claude", None),
            session("c", "codex", None),
        ],
        scope="s",
    )

    assert [dict(sample.labels)["runtime"] for sample in family.samples] == ["claude"]
    assert family.samples[0].value == pytest.approx(0.5)


# ---- the transcript counters ---------------------------------------------------------


def test_session_cost_repro_counters_price_each_message(tmp_path: Path) -> None:
    """Two 60k-prompt Haiku 5.5 messages bill at base rates although their sum crosses 100k."""
    path = _write_jsonl(
        tmp_path / "counters.jsonl",
        [
            _assistant(1, "claude-haiku-5-5", "2026-11-10T10:00:00Z", input_tokens=60_000),
            _assistant(2, "claude-haiku-5-5", "2026-11-10T10:01:00Z", input_tokens=60_000),
        ],
    )
    reading = read_transcript(path)

    assert reading is not None and reading.counters is not None
    assert reading.counters.cost_usd == Decimal("0.012")


def test_counters_sum_priced_messages_and_skip_an_unpriced_one(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "counters-mixed.jsonl",
        [
            _assistant(1, "claude-haiku-5-5", "2026-11-10T10:00:00Z", input_tokens=10_000),
            _assistant(2, "claude-unknown-9", "2026-11-10T10:01:00Z", input_tokens=10_000),
        ],
    )
    reading = read_transcript(path)

    assert reading is not None and reading.counters is not None
    assert reading.counters.cost_usd == Decimal("0.001")


def test_counters_with_only_unpriced_messages_have_a_null_cost(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "counters-unpriced.jsonl",
        [_assistant(1, "claude-unknown-9", "2026-11-10T10:00:00Z", input_tokens=10_000)],
    )
    reading = read_transcript(path)

    assert reading is not None and reading.counters is not None
    assert reading.counters.cost_usd is None


# ---- summed totals priced only where exact -------------------------------------------


def test_quote_totals_below_the_tier_equals_the_per_request_sum() -> None:
    usage = TokenUsage(input=40_000, cache_read=60_000, output=1_000)
    one = _BOOK.quote("claude-haiku-5-5", usage, on=_ON)
    total = _BOOK.quote_totals("claude-haiku-5-5", usage, first=_ON, last=_ON)

    # Exactly at the threshold the tier does not apply.
    assert usage.prompt_tokens == 100_000
    assert total.priced and total.cost_usd == one.cost_usd


def test_quote_totals_above_the_tier_is_unpriced() -> None:
    usage = TokenUsage(input=40_001, cache_read=60_000, output=1_000)
    quote = _BOOK.quote_totals("claude-haiku-5-5", usage, first=_ON, last=_ON)

    assert quote.cost_usd is None and not quote.priced
    assert quote.unpriced_tokens == 101_001
    assert quote.reason is not None and "100000-token tier" in quote.reason


def test_quote_totals_across_a_rate_change_is_unpriced(tmp_path: Path) -> None:
    book = _dated_book(tmp_path)
    usage = TokenUsage(input=1_000)

    spanning = book.quote_totals("claude-test-1", usage, first=date(2026, 11, 9), last=_ON)
    within = book.quote_totals("claude-test-1", usage, first=_ON, last=date(2026, 11, 12))

    assert spanning.cost_usd is None and spanning.reason is not None
    assert "change between" in spanning.reason
    assert within.cost_usd == Decimal("0.002")


def test_quote_totals_unknown_model_is_unpriced() -> None:
    quote = _BOOK.quote_totals("claude-unknown-9", TokenUsage(input=1), first=_ON, last=_ON)

    assert quote.source is None and quote.cost_usd is None and quote.unpriced_tokens == 1


def test_quote_totals_refuses_a_window_that_ends_before_it_starts() -> None:
    with pytest.raises(ValueError, match="precedes"):
        _BOOK.quote_totals("claude-haiku-5-5", TokenUsage(), first=_ON, last=date(2026, 11, 9))


def _spawn(**tokens: int) -> SpawnResult:
    moment = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    return SpawnResult(
        session_id="spawn-1",
        runtime="claude-code",
        model="claude-haiku-5-5",
        subprocess_pid=1,
        exit_status=0,
        text="",
        started_at=moment,
        ended_at=moment,
        **tokens,  # type: ignore[arg-type]
    )


def test_session_cost_repro_spawn_totals_above_a_tier_are_unpriced() -> None:
    metered = price_spawn_result(_spawn(input_tokens=1_000, cache_read_input_tokens=150_000))

    assert metered.cost_usd is None and not metered.priced


def test_spawn_totals_below_every_tier_price_exactly() -> None:
    metered = price_spawn_result(_spawn(input_tokens=1_000, output_tokens=1_000))

    assert metered.priced and metered.cost_usd == Decimal("0.0006")
