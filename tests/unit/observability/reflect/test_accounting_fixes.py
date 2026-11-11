"""Compaction summaries, thinking and Codex cache writes read off real transcript shapes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.reflect.generations import session_context
from eawf.observability.reflect.html.session import _sankey, _timeline
from eawf.observability.telemetry.models import PriceSourceKind
from eawf.observability.telemetry.pricing import TokenUsage, load_price_book
from eawf.observability.telemetry.sources.codex_session import (
    CODEX_WRITE_CLASS,
    CodexFold,
    CodexSessionSource,
    fold_codex_record,
)
from eawf.observability.telemetry.sources.session_history import (
    HistoryFile,
    SessionDetail,
    read_session_detail,
    reprice_request,
)

_T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
_SESSION = "0b6f2d1e-5a2c-4c1e-9d7a-3f1e2a4b5c6d"


def _stamp(seconds: float) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _claude(seconds: float, kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "sessionId": _SESSION, "timestamp": _stamp(seconds), **fields}


def _reply(
    seconds: float,
    message_id: str,
    text: str,
    *,
    output: int = 40,
    usage: dict[str, Any] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return _claude(
        seconds,
        "assistant",
        message={
            "id": message_id,
            "model": "claude-opus-5-5",
            "content": blocks if blocks is not None else [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": output, **(usage or {})},
        },
        **fields,
    )


def _ask(seconds: float, text: str, **fields: Any) -> dict[str, Any]:
    return _claude(seconds, "user", message={"role": "user", "content": text}, **fields)


def _boundary(seconds: float) -> dict[str, Any]:
    return _claude(
        seconds,
        "system",
        subtype="compact_boundary",
        compactMetadata={"trigger": "auto", "preTokens": 90000},
    )


def _detail(tmp_path: Path, records: list[dict[str, Any]]) -> SessionDetail:
    detail = read_session_detail(
        HistoryFile(runtime="claude", path=_write(tmp_path / f"{_SESSION}.jsonl", records))
    )
    assert detail is not None
    return detail


def _compacting(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    records = [
        _ask(0, "Plan the release " + "x" * 2000),
        _reply(5, "msg_1", "Here is the plan " + "y" * 3000),
        _ask(30, "Go on " + "z" * 1500),
        _reply(40, "msg_2", "Continuing " + "w" * 2500),
        _boundary(60),
    ]
    if summary is not None:
        records.append(summary)
    records.append(_reply(70, "msg_3", "Resumed after compaction"))
    return records


def _carried(detail: SessionDetail) -> tuple[int, str]:
    generations = session_context(detail).generations
    assert len(generations) == 2
    before, after = generations[0].summary, generations[1].summary
    return after.tokens_by_kind.get("compaction_summary", 0), _sankey(before, after, None)


# ---- the compaction summary ----------------------------------------------------------


def test_compact_summary(tmp_path: Path) -> None:
    summary = _ask(
        61,
        "This session is being continued from a previous conversation. " + "s" * 4000,
        isCompactSummary=True,
        isVisibleInTranscriptOnly=True,
    )
    carried, half = _carried(_detail(tmp_path, _compacting(summary)))
    assert carried > 0
    assert 'class="flow k-compact"' in half
    assert "summary not found" not in half


def test_compact_summary_missing_collapses_right_half(tmp_path: Path) -> None:
    carried, half = _carried(_detail(tmp_path, _compacting(None)))
    assert carried == 0
    assert "summary not found" in half
    assert 'class="flow k-compact"' not in half
    # Collapsed, the chart ends 130px past the gate node, which sits 260px right of the gutter.
    node = re.search(r'<rect class="k-[a-z]+" x="([\d.]+)"', half)
    assert node
    assert f'viewBox="0 0 {float(node.group(1)) + 260 + 12 + 130:.0f} ' in half


def test_compact_summary_flag_false_is_a_user_message(tmp_path: Path) -> None:
    plain = _ask(61, "an ordinary message " + "p" * 400, isCompactSummary=False)
    detail = _detail(tmp_path, _compacting(plain))
    assert [b.kind for b in detail.blocks].count("compaction_summary") == 0
    assert _carried(detail)[0] == 0


def test_compact_summary_text_blocks_and_legacy_summary_row(tmp_path: Path) -> None:
    blocks = _claude(
        61,
        "user",
        isCompactSummary=True,
        message={"role": "user", "content": [{"type": "text", "text": "q" * 800}]},
    )
    legacy = _claude(62, "summary", summary="an older summary row")
    detail = _detail(tmp_path, [*_compacting(blocks)[:-1], legacy, _compacting(None)[-1]])
    kinds = [block.kind for block in detail.blocks]
    assert kinds.count("compaction_summary") == 2
    assert _carried(detail)[0] > 0


def test_compact_summary_empty_content_sizes_zero(tmp_path: Path) -> None:
    empty = _claude(61, "user", isCompactSummary=True, message={"role": "user", "content": ""})
    detail = _detail(tmp_path, _compacting(empty))
    sized = [b.size_chars for b in detail.blocks if b.kind == "compaction_summary"]
    assert sized == [0]


# ---- thinking tokens and duration ----------------------------------------------------


def _thinking_records(
    detail_usage: object = None, duration: object = 45000
) -> list[dict[str, Any]]:
    usage: dict[str, Any] = {} if detail_usage is None else {"output_tokens_details": detail_usage}
    return [
        _ask(0, "Why does the build fail?"),
        _reply(10, "msg_a", "Looking", output=30),
        # One message, one record per content block: the thinking block first,
        # carrying the duration, then the text with the final counters.
        _reply(
            60,
            "msg_b",
            "",
            output=2,
            blocks=[{"type": "thinking", "thinking": "t" * 900}],
            thinkingDurationMs=duration,
        ),
        _reply(61, "msg_b", "It is the lock file", output=400, usage=usage),
    ]


def _legend_thinking(detail: SessionDetail) -> str:
    html = _timeline(detail)
    marker = '<i class="ph-think"></i>Thinking '
    return html.split(marker, 1)[1].split("</span>", 1)[0]


def test_thinking(tmp_path: Path) -> None:
    detail = _detail(tmp_path, _thinking_records({"thinking_tokens": 300}))
    final = detail.steps[-1]
    assert final.reasoning_tokens == 300
    assert final.output_tokens == 400
    assert detail.thinking_spans[0].duration_ms == 45000
    assert _legend_thinking(detail) != "0s"


def test_thinking_counter_clamped_to_output(tmp_path: Path) -> None:
    detail = _detail(tmp_path, _thinking_records({"thinking_tokens": 10_000}))
    assert detail.steps[-1].reasoning_tokens == 400


def test_thinking_counter_zero_is_kept(tmp_path: Path) -> None:
    detail = _detail(tmp_path, _thinking_records({"thinking_tokens": 0}))
    assert detail.steps[-1].reasoning_tokens == 0


@pytest.mark.parametrize("raw", [None, {}, {"thinking_tokens": -1}, {"thinking_tokens": True}, []])
def test_thinking_counter_absent_or_malformed_stays_null(tmp_path: Path, raw: object) -> None:
    detail = _detail(tmp_path, _thinking_records(raw))
    assert all(step.reasoning_tokens is None for step in detail.steps)


@pytest.mark.parametrize("duration", [0, -5, True, "45000", None])
def test_thinking_duration_malformed_draws_no_span(tmp_path: Path, duration: object) -> None:
    detail = _detail(tmp_path, _thinking_records(None, duration))
    assert detail.thinking_spans == ()
    assert _legend_thinking(detail) == "0s"


def test_thinking_duration_repeated_on_one_message_counts_once(tmp_path: Path) -> None:
    records = _thinking_records(None)
    records.insert(3, {**records[2], "timestamp": _stamp(60.5)})
    detail = _detail(tmp_path, records)
    assert len(detail.thinking_spans) == 1


# ---- Codex cache writes --------------------------------------------------------------


def _codex_usage(gross: int, cached: int, writes: int, output: int) -> dict[str, int]:
    return {
        "input_tokens": gross,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": writes,
        "output_tokens": output,
        "reasoning_output_tokens": output // 4,
        "total_tokens": gross + output,
    }


def _rollout(model: str, readings: list[tuple[dict[str, int], dict[str, int]]]) -> list[dict]:
    records: list[dict[str, Any]] = [
        {
            "timestamp": _stamp(0),
            "type": "session_meta",
            "payload": {"id": _SESSION, "cwd": "/work/repo"},
        },
        {"timestamp": _stamp(1), "type": "turn_context", "payload": {"model": model}},
    ]
    for index, (total, last) in enumerate(readings, start=1):
        records.append(
            {
                "timestamp": _stamp(index * 30),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": total, "last_token_usage": last},
                },
            }
        )
    return records


def _codex_detail(tmp_path: Path, records: list[dict[str, Any]]) -> SessionDetail:
    path = _write(tmp_path / f"rollout-2026-11-09T10-00-00-{_SESSION}.jsonl", records)
    detail = read_session_detail(HistoryFile(runtime="codex", path=path))
    assert detail is not None
    return detail


def test_codex_cache_writes(tmp_path: Path) -> None:
    first = _codex_usage(10_000, 2_000, 3_000, 500)
    second = _codex_usage(4_000, 3_000, 0, 200)
    total_two = {key: first[key] + second[key] for key in first}
    records = _rollout("gpt-6.1-sol", [(first, first), (total_two, second)])
    steps = _codex_detail(tmp_path, records).steps
    written, plain = steps
    assert written.cache_write_tokens == 3_000
    assert written.input_tokens == 5_000
    assert written.cache_read_tokens == 2_000
    assert written.total_tokens == 10_500
    # The rollout names no write class, so the writes stay unpriced, never zero.
    assert written.cost_usd is None
    assert written.price_source is PriceSourceKind.UNPRICED
    assert plain.cache_write_tokens == 0
    assert plain.cost_usd is not None
    session = next(CodexSessionSource().iter_rows(next(tmp_path.glob("rollout-*.jsonl"))))
    assert session.total_cache_write == 3_000
    assert session.total_input_tokens == 14_000 - 5_000 - 3_000


def test_codex_cache_writes_priced_by_named_class() -> None:
    book = load_price_book(repo_root=None, overrides={})
    day = _T0.date()
    named = book.quote("gpt-6.1-sol", TokenUsage(input=1, cache_write={"30m": 1_000_000}), on=day)
    assert named.priced
    unnamed = book.quote(
        "gpt-6.1-sol", TokenUsage(input=1, cache_write={CODEX_WRITE_CLASS: 1_000}), on=day
    )
    assert not unnamed.priced
    assert unnamed.unpriced_tokens == 1_000


def test_codex_cache_writes_reprice_keeps_them_unpriced(tmp_path: Path) -> None:
    usage = _codex_usage(10_000, 2_000, 3_000, 500)
    written = _codex_detail(tmp_path, _rollout("gpt-6.1-sol", [(usage, usage)])).steps[0]
    again = reprice_request(written, load_price_book(repo_root=None, overrides={}))
    assert again.cost_usd is None


def test_codex_cache_writes_clamped_to_uncached_input() -> None:
    fold = CodexFold()
    usage = _codex_usage(1_000, 800, 5_000, 10)
    reading = fold_codex_record(
        fold,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": usage, "last_token_usage": usage},
            },
        },
    )
    assert reading is not None
    assert (reading.input_tokens, reading.cache_read_tokens, reading.cache_write_tokens) == (
        0,
        800,
        200,
    )
    assert fold.total_cache_write == 200
    assert fold.total_input_tokens == 0


@pytest.mark.parametrize("raw", [None, -3, "12", True])
def test_codex_cache_writes_malformed_read_as_none(raw: object) -> None:
    usage: dict[str, Any] = {**_codex_usage(1_000, 100, 0, 10), "cache_write_input_tokens": raw}
    reading = fold_codex_record(
        CodexFold(),
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
        },
    )
    assert reading is not None
    assert reading.cache_write_tokens == 0
    assert reading.input_tokens == 900


def test_codex_cache_writes_from_movement_without_last_usage() -> None:
    fold = CodexFold()
    for total in (_codex_usage(1_000, 0, 100, 10), _codex_usage(3_000, 500, 700, 30)):
        reading = fold_codex_record(
            fold,
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": total}},
            },
        )
    assert reading is not None
    assert (reading.input_tokens, reading.cache_read_tokens, reading.cache_write_tokens) == (
        900,
        500,
        600,
    )
