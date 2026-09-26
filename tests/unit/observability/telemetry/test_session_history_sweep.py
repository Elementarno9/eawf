"""Tests for the session-history sweep into the observed collection.

Every transcript here is a synthetic fixture written under ``tmp_path``; the
real home directory is never read (the history root is always injected).
"""

from __future__ import annotations

import json
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import eawf.observability.telemetry.projector as projector
from eawf.observability.telemetry.models import (
    DurationKind,
    ObservedDuration,
    ObservedSession,
    PriceSourceKind,
)
from eawf.observability.telemetry.projector import (
    SweepDrop,
    SweepDropReason,
    SweepFunnel,
    SweepStage,
    sweep_session_history,
)
from eawf.observability.telemetry.sources.session_history import (
    classify_label,
    claude_history_root,
    discover_history,
    parse_transcript,
)
from eawf.observability.telemetry.store import SqliteMetricsStore

_PRICED_MODEL = "claude-opus-4-7"
_SECRET_COMMAND = "tee notes.txt <<'EOF'\nprivate operator words\nEOF"


def _ts(second: int) -> str:
    return f"2026-09-01T10:00:{second:02d}Z"


def _assistant(
    second: int,
    *,
    message_id: str,
    content: list[dict[str, Any]] | None = None,
    model: str = _PRICED_MODEL,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": "raw-session-id",
        "timestamp": _ts(second),
        "message": {
            "id": message_id,
            "model": model,
            "content": content or [],
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    }


def _result(second: int, tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _ts(second),
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
    }


def _tool_use(block_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def _working_session(session_id: str, *, model: str = _PRICED_MODEL) -> list[dict[str, Any]]:
    return [
        {"type": "user", "sessionId": session_id, "timestamp": _ts(0), "message": {"content": "x"}},
        _assistant(
            1,
            message_id=f"{session_id}-m1",
            model=model,
            content=[
                _tool_use("t-bash", "Bash", {"command": "git status"}),
                _tool_use("t-task", "Task", {"subagent_type": "Explore"}),
                _tool_use("t-heredoc", "Bash", {"command": _SECRET_COMMAND}),
            ],
        ),
        _result(4, "t-bash"),
        _result(9, "t-task"),
        _result(2, "t-heredoc"),
    ]


def _write(root: Path, name: str, records: list[dict[str, Any]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> SqliteMetricsStore:
    s = SqliteMetricsStore(tmp_path / "telemetry.db")
    s.init_schema()
    return s


def _sessions(store: SqliteMetricsStore) -> list[ObservedSession]:
    rows = store.fetch_all("telemetry_observed_sessions", ObservedSession)
    return [r for r in rows if isinstance(r, ObservedSession)]


def _durations(store: SqliteMetricsStore) -> list[ObservedDuration]:
    rows = store.fetch_all("telemetry_observed_durations", ObservedDuration)
    return [r for r in rows if isinstance(r, ObservedDuration)]


def _assert_funnel_holds(funnel: SweepFunnel) -> None:
    stages = funnel.stages()
    assert all(a >= b for a, b in pairwise(stages)), stages
    assert funnel.seen - funnel.projected == len(funnel.drops)


def test_sweep_session_history_one_unparsable_file_drops_exactly_it(
    tmp_path: Path, store: SqliteMetricsStore
) -> None:
    root = tmp_path / "history"
    for idx in range(3):
        _write(root, f"good-{idx}.jsonl", _working_session(f"s{idx}"))
    (root / "broken.jsonl").write_text("{not json\n\x00\x01 garbage\n", encoding="utf-8")

    funnel = sweep_session_history(store, root, project_id="repo/demo")

    assert funnel.seen == 4
    assert funnel.parsed == funnel.seen - 1
    assert funnel.stages() == (4, 3, 3, 3)
    assert funnel.drops == [
        SweepDrop("broken.jsonl", SweepStage.PARSED, SweepDropReason.CORRUPT_RECORD)
    ]
    _assert_funnel_holds(funnel)

    sessions = _sessions(store)
    assert len(sessions) == 3
    for row in sessions:
        assert (row.input_tokens, row.output_tokens) == (10, 20)
        assert (row.cache_read_tokens, row.cache_write_tokens) == (30, 40)
        assert row.reasoning_tokens is None
        assert row.total_tokens == 100
        assert row.price_source is PriceSourceKind.LIST_RECONSTRUCTED
        assert row.rate_table_version
        assert row.cost_usd > 0
        assert row.duration_ms == 9000

    by_key = {(d.vendor_session_ref, d.kind, d.label): d for d in _durations(store)}
    ref = sessions[0].vendor_session_ref
    assert by_key[(ref, DurationKind.TOOL_CALL, "Bash")].count == 2
    assert by_key[(ref, DurationKind.TOOL_CALL, "Bash")].total_ms == 4000
    assert by_key[(ref, DurationKind.COMMAND, "git")].max_ms == 3000
    assert by_key[(ref, DurationKind.COMMAND, "other")].total_ms == 1000
    assert by_key[(ref, DurationKind.SUBAGENT, "Explore")].total_ms == 8000
    assert funnel.durations == len(by_key)


@pytest.mark.parametrize(
    "layout",
    [
        "missing",
        "empty",
        "single_good",
        "single_no_work",
        "mixed",
    ],
)
def test_sweep_session_history_funnel_never_increases(
    tmp_path: Path, store: SqliteMetricsStore, layout: str
) -> None:
    root = tmp_path / "history"
    if layout != "missing":
        root.mkdir()
    if layout in {"single_good", "mixed"}:
        _write(root, "a.jsonl", _working_session("a"))
    if layout in {"single_no_work", "mixed"}:
        _write(root, "b.jsonl", [{"type": "user", "timestamp": _ts(0), "message": {}}])
    if layout == "mixed":
        (root / "c.jsonl").write_bytes(b"\xff\xfe\x00bad")
        (root / "d.jsonl").write_text("\n\n", encoding="utf-8")

    funnel = sweep_session_history(store, root, project_id="repo/demo")

    _assert_funnel_holds(funnel)
    expected = {
        "missing": (0, 0, 0, 0),
        "empty": (0, 0, 0, 0),
        "single_good": (1, 1, 1, 1),
        "single_no_work": (1, 1, 0, 0),
        "mixed": (4, 2, 1, 1),
    }[layout]
    assert funnel.stages() == expected


def test_sweep_session_history_no_operator_work_drop_is_typed(
    tmp_path: Path, store: SqliteMetricsStore
) -> None:
    root = tmp_path / "history"
    _write(root, "idle.jsonl", [{"type": "summary", "summary": "x"}])

    funnel = sweep_session_history(store, root, project_id="repo/demo")

    assert funnel.drops == [
        SweepDrop("idle.jsonl", SweepStage.ATTRIBUTED, SweepDropReason.NO_OPERATOR_WORK)
    ]
    assert _sessions(store) == []


def test_sweep_session_history_projection_refused_is_typed(
    tmp_path: Path, store: SqliteMetricsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "history"
    _write(root, "a.jsonl", _working_session("a"))

    def _refuse(*_: object, **__: object) -> None:
        raise ValueError("refused")

    monkeypatch.setattr(projector, "observed_rows", _refuse)
    funnel = sweep_session_history(store, root, project_id="repo/demo")

    assert funnel.stages() == (1, 1, 1, 0)
    assert funnel.drops[0].reason is SweepDropReason.PROJECTION_REFUSED
    _assert_funnel_holds(funnel)


def test_sweep_session_history_rerun_is_idempotent(
    tmp_path: Path, store: SqliteMetricsStore
) -> None:
    root = tmp_path / "history"
    _write(root, "a.jsonl", _working_session("a"))
    sweep_session_history(store, root, project_id="repo/demo")
    first = (len(_sessions(store)), len(_durations(store)))
    sweep_session_history(store, root, project_id="repo/demo")
    assert (len(_sessions(store)), len(_durations(store))) == first


def test_sweep_session_history_persists_no_transcript_content(
    tmp_path: Path, store: SqliteMetricsStore
) -> None:
    root = tmp_path / "history"
    _write(root, "a.jsonl", _working_session("raw-session-id"))
    sweep_session_history(store, root, project_id="repo/demo")

    dumped = json.dumps(
        [r.model_dump(mode="json") for r in [*_sessions(store), *_durations(store)]]
    )
    assert "raw-session-id" not in dumped
    assert "private operator words" not in dumped
    assert str(tmp_path) not in dumped
    assert _sessions(store)[0].vendor_session_ref.startswith("vsid-")


def test_parse_transcript_counts_a_billed_message_once(tmp_path: Path) -> None:
    first = _assistant(1, message_id="m1")
    repeat = _assistant(2, message_id="m1")
    path = _write(tmp_path, "s.jsonl", [first, repeat])

    parsed = parse_transcript(path)

    assert parsed is not None
    assert parsed.turn_count == 1
    assert parsed.input_tokens == 10


def test_parse_transcript_splits_cache_write_tiers(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 50,
        "cache_creation": {"ephemeral_1h_input_tokens": 20},
    }
    path = _write(tmp_path, "s.jsonl", [_assistant(1, message_id="m1", usage=usage)])

    parsed = parse_transcript(path)

    assert parsed is not None
    assert (parsed.cache_write_5m_tokens, parsed.cache_write_1h_tokens) == (30, 20)
    assert parsed.cache_write_tokens == 50


def test_parse_transcript_skips_unpaired_and_backwards_blocks(tmp_path: Path) -> None:
    records = [
        _assistant(
            5,
            message_id="m1",
            content=[_tool_use("open", "Read", {}), _tool_use("back", "Grep", {})],
        ),
        _result(1, "back"),
        _result(6, "stranger"),
    ]
    parsed = parse_transcript(_write(tmp_path, "s.jsonl", records))

    assert parsed is not None
    assert parsed.durations == {}


def test_parse_transcript_skips_malformed_lines_but_keeps_good_ones(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(
        "{broken\n[1, 2]\n" + json.dumps(_assistant(1, message_id="m1")) + "\n",
        encoding="utf-8",
    )
    parsed = parse_transcript(path)

    assert parsed is not None
    assert parsed.skipped_lines == 2
    assert parsed.session_id == "raw-session-id"


def test_parse_transcript_falls_back_to_file_stem_for_session_id(tmp_path: Path) -> None:
    record = _assistant(1, message_id="m1")
    del record["sessionId"]
    parsed = parse_transcript(_write(tmp_path, "stem-id.jsonl", [record]))

    assert parsed is not None
    assert parsed.session_id == "stem-id"


@pytest.mark.parametrize(
    ("model", "source"),
    [
        (_PRICED_MODEL, PriceSourceKind.LIST_RECONSTRUCTED),
        ("claude-opus-5-5", PriceSourceKind.UNPRICED),
        ("vendor-unknown-1", PriceSourceKind.UNPRICED),
    ],
)
def test_sweep_session_history_price_source_per_model(
    tmp_path: Path, store: SqliteMetricsStore, model: str, source: PriceSourceKind
) -> None:
    root = tmp_path / "history"
    _write(root, "a.jsonl", _working_session("a", model=model))
    sweep_session_history(store, root, project_id="repo/demo")

    row = _sessions(store)[0]
    assert row.price_source is source
    if source is PriceSourceKind.UNPRICED:
        assert row.cost_usd == Decimal("0")
        assert row.rate_table_version is None


@pytest.mark.parametrize(
    ("kind", "name", "label"),
    [
        (DurationKind.TOOL_CALL, "Bash", "Bash"),
        (DurationKind.TOOL_CALL, "mcp__serena__find_symbol", "mcp"),
        (DurationKind.TOOL_CALL, "SomeNewTool", "other"),
        (DurationKind.TOOL_CALL, None, "other"),
        (DurationKind.COMMAND, "uv", "uv"),
        (DurationKind.COMMAND, "", "other"),
        (DurationKind.COMMAND, "EOF", "other"),
        (DurationKind.SUBAGENT, "Plan", "Plan"),
        (DurationKind.SUBAGENT, 42, "other"),
    ],
)
def test_classify_label_emits_only_vocabulary(kind: DurationKind, name: object, label: str) -> None:
    assert classify_label(kind, name) == label


def test_discover_history_ignores_nested_and_non_jsonl(tmp_path: Path) -> None:
    root = tmp_path / "history"
    _write(root, "b.jsonl", [])
    _write(root, "a.jsonl", [])
    (root / "notes.txt").write_text("x", encoding="utf-8")
    _write(root / "a" / "subagents", "agent-1.jsonl", [])

    assert [p.name for p in discover_history(root)] == ["a.jsonl", "b.jsonl"]
    assert discover_history(tmp_path / "missing") == []


def test_claude_history_root_honours_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    assert claude_history_root(Path("/work/my.repo")) == tmp_path / "projects" / "-work-my-repo"

    monkeypatch.delenv("EAWF_CLAUDE_PROJECTS_DIR")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude_history_root(Path("/w")) == tmp_path / "cfg" / "projects" / "-w"


def _session_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "vendor_session_ref": "vsid-" + "0" * 32,
        "runtime": "claude",
        "project_id": "repo/demo",
        "input_tokens": 1,
        "total_tokens": 1,
        "price_source": "unpriced",
    }
    row.update(overrides)
    return row


def test_observed_session_rejects_unreconciled_total() -> None:
    with pytest.raises(ValidationError, match="total_tokens"):
        ObservedSession.model_validate(_session_row(total_tokens=2))


def test_observed_session_rejects_raw_session_id() -> None:
    with pytest.raises(ValidationError, match="vendor_session_ref"):
        ObservedSession.model_validate(_session_row(vendor_session_ref="raw-session-id"))


def test_observed_session_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ObservedSession.model_validate(_session_row(transcript="text"))


def _duration_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "vendor_session_ref": "vsid-" + "0" * 32,
        "runtime": "claude",
        "kind": "command",
        "label": "git",
        "count": 1,
        "total_ms": 5,
        "max_ms": 5,
    }
    row.update(overrides)
    return row


def test_observed_duration_rejects_out_of_vocabulary_label() -> None:
    with pytest.raises(ValidationError, match="vocabulary"):
        ObservedDuration.model_validate(_duration_row(label="rm -rf private"))


def test_observed_duration_rejects_max_over_total() -> None:
    with pytest.raises(ValidationError, match="max_ms"):
        ObservedDuration.model_validate(_duration_row(max_ms=6))


def test_observed_duration_rejects_zero_count() -> None:
    with pytest.raises(ValidationError, match="count"):
        ObservedDuration.model_validate(_duration_row(count=0))
