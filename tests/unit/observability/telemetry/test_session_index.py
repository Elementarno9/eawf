"""Tests for the incremental session-history index.

Every transcript is a synthetic fixture under ``tmp_path``; no real history
is read.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import pytest
from pydantic import BaseModel, ValidationError

import eawf.observability.telemetry.projector as projector
from eawf.observability.telemetry.models import (
    DurationKind,
    HistoryIndexEntry,
    ObservedDuration,
    ObservedRequest,
    ObservedSession,
    PriceSourceKind,
    RequestDropReason,
    SweepDropReason,
)
from eawf.observability.telemetry.projector import (
    IndexBusyError,
    LocalHistory,
    SweepFunnel,
    SweepStage,
    history_index_lock,
    index_session_history,
    load_session_detail,
)
from eawf.observability.telemetry.sources.codex_session import CodexFold, fold_codex_record
from eawf.observability.telemetry.sources.session_history import (
    REFLECTION_MARKER,
    DurationTally,
    HistoryFile,
    ParsedTranscript,
    RequestLedger,
    TranscriptFold,
    claude_history_root,
    discover_claude_project,
    discover_history_files,
    fold_history_lines,
    history_lines,
    history_path_digest,
    project_history_lines,
    read_session_detail,
    working_dir_digest,
)
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.cli.commands.reflect import _progress

_PRICED = "claude-opus-4-7"
_UNPRICED = "vendor-unknown-1"


def _ts(second: int) -> str:
    return f"2026-09-01T10:{second // 60:02d}:{second % 60:02d}Z"


def _claude_user(
    second: int, cwd: Path, content: object = "do the thing", *, session: str = "claude-raw-id"
) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "cwd": str(cwd),
        "timestamp": _ts(second),
        "message": {"content": content},
    }


def _claude_assistant(
    second: int,
    message_id: str,
    *,
    output: int = 20,
    model: str = _PRICED,
    content: list[dict[str, Any]] | None = None,
    session: str = "claude-raw-id",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": _ts(second),
        "message": {
            "id": message_id,
            "model": model,
            "content": content or [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": output,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
                "cache_creation": {"ephemeral_1h_input_tokens": 15},
            },
        },
    }


def _tool_use(block_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def _tool_result(second: int, tool_use_id: str, cwd: Path) -> dict[str, Any]:
    return _claude_user(second, cwd, [{"type": "tool_result", "tool_use_id": tool_use_id}])


def _claude_lines(
    cwd: Path, *, model: str = _PRICED, session: str = "claude-raw-id"
) -> list[dict[str, Any]]:
    bash = _tool_use("t1", "Bash", {"command": "git log"})
    return [
        _claude_user(0, cwd, session=session),
        _claude_assistant(1, "m1", model=model, content=[bash], session=session),
        _tool_result(3, "t1", cwd),
        _claude_assistant(
            4, "m2", output=5, model=model, content=[_tool_use("t2", "Read", {})], session=session
        ),
    ]


def _codex_usage(gross: int, cached: int, output: int, reasoning: int) -> dict[str, int]:
    return {
        "input_tokens": gross,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": gross + output,
    }


def _codex_count(
    second: int, total: tuple[int, ...], last: tuple[int, ...] | None
) -> dict[str, Any]:
    info: dict[str, Any] = {"total_token_usage": _codex_usage(*total)}
    if last is not None:
        info["last_token_usage"] = _codex_usage(*last)
    return {
        "timestamp": _ts(second),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def _codex_lines(cwd: Path) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": _ts(0),
            "type": "session_meta",
            "payload": {"id": "codex-raw-id", "cwd": str(cwd), "base_instructions": "be terse"},
        },
        {
            "timestamp": _ts(1),
            "type": "turn_context",
            "payload": {"model": "gpt-5-codex", "cwd": str(cwd)},
        },
        {
            "timestamp": _ts(2),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": ["git", "status"]}),
                "call_id": "c1",
            },
        },
        {
            "timestamp": _ts(5),
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "clean"},
        },
        _codex_count(6, (100, 40, 10, 4), (100, 40, 10, 4)),
        _codex_count(7, (100, 40, 10, 4), (100, 40, 10, 4)),
        _codex_count(9, (250, 140, 30, 9), (150, 100, 20, 5)),
    ]


def _dump(records: list[dict[str, Any]]) -> str:
    return "".join(f"{json.dumps(record)}\n" for record in records)


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(records), encoding="utf-8")
    return path


def _append(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_dump(records))


def _store(path: Path) -> SqliteMetricsStore:
    store = SqliteMetricsStore(path)
    store.init_schema()
    return store


def _rows(store: SqliteMetricsStore) -> dict[str, list[dict[str, Any]]]:
    tables: tuple[tuple[str, type[BaseModel]], ...] = (
        ("telemetry_observed_sessions", ObservedSession),
        ("telemetry_observed_requests", ObservedRequest),
        ("telemetry_observed_durations", ObservedDuration),
    )
    return {
        table: sorted(
            (row.model_dump(mode="json") for row in store.fetch_all(table, model)),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for table, model in tables
    }


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    cwd = tmp_path / "work" / "repo"
    return {
        "cwd": cwd,
        "claude": tmp_path / "claude-projects",
        "codex": tmp_path / "codex-sessions",
    }


def _files(layout: dict[str, Path]) -> list[HistoryFile]:
    return discover_history_files(claude_projects=layout["claude"], codex_sessions=layout["codex"])


class _DecodeSpy:
    """Count the lines the index decodes per file, through the real fold."""

    def __init__(self) -> None:
        self.decoded: dict[str, int] = {}
        self._real = fold_history_lines

    def __call__(self, handle: IO[bytes], *args: Any, **kwargs: Any) -> tuple[int, int]:
        consumed, decoded = self._real(handle, *args, **kwargs)
        name = Path(str(getattr(handle, "name", ""))).name
        self.decoded[name] = self.decoded.get(name, 0) + decoded
        return consumed, decoded


# ---- CR-737: an append decodes only the appended lines -------------------------------


def test_append_only(
    tmp_path: Path, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = layout["cwd"]
    claude = _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(cwd))
    still = _write(layout["claude"] / "-work-other" / "b.jsonl", _claude_lines(cwd, session="b"))
    codex = _write(
        layout["codex"] / "2026" / "09" / "01" / "rollout-2026-09-01-x.jsonl", _codex_lines(cwd)
    )
    store = _store(tmp_path / "incremental.db")
    first = index_session_history(store, _files(layout), project_id="scope")
    assert first.lines_decoded == 4 + 4 + 7

    # The appended Claude lines finish a message begun before the append, close
    # a tool opened before it and bill a new one; the Codex lines bill one.
    _append(
        claude,
        [
            _claude_assistant(5, "m2", output=50),
            _tool_result(8, "t2", cwd),
            _claude_assistant(9, "m3", model=_UNPRICED),
        ],
    )
    _append(
        codex,
        [
            {"timestamp": _ts(10), "type": "turn_context", "payload": {"model": "gpt-5"}},
            _codex_count(11, (400, 200, 50, 12), (150, 60, 20, 3)),
        ],
    )
    spy = _DecodeSpy()
    monkeypatch.setattr(projector, "fold_history_lines", spy)
    second = index_session_history(store, _files(layout), project_id="scope")

    assert spy.decoded == {claude.name: 3, codex.name: 2}
    assert still.name not in spy.decoded
    assert (second.files_read, second.lines_decoded) == (2, 5)

    rebuilt = _store(tmp_path / "rebuilt.db")
    monkeypatch.setattr(projector, "fold_history_lines", spy._real)
    full = index_session_history(rebuilt, _files(layout), project_id="scope")
    assert full.lines_decoded == 4 + 3 + 4 + 7 + 2
    assert _rows(store) == _rows(rebuilt)
    assert second.stages() == full.stages()
    assert (second.requests, second.requests_priced) == (full.requests, full.requests_priced)

    requests = [
        ObservedRequest.model_validate(r) for r in _rows(store)["telemetry_observed_requests"]
    ]
    claude_requests = sorted(
        (r for r in requests if r.runtime == "claude"),
        key=lambda r: (r.vendor_session_ref, r.ordinal),
    )
    completed = [r for r in claude_requests if r.output_tokens == 50]
    assert len(completed) == 1 and completed[0].ordinal == 2
    codex_requests = sorted((r for r in requests if r.runtime == "codex"), key=lambda r: r.ordinal)
    assert [
        (r.input_tokens, r.cache_read_tokens, r.output_tokens, r.reasoning_tokens)
        for r in codex_requests
    ] == [
        (60, 40, 10, 4),
        (50, 100, 20, 5),
        (90, 60, 20, 3),
    ]
    assert [r.model for r in codex_requests] == ["gpt-5-codex", "gpt-5-codex", "gpt-5"]
    assert all(
        r.cost_usd is None and r.price_source is PriceSourceKind.UNPRICED for r in codex_requests
    )


def test_append_only_unchanged_sweep_opens_nothing(
    tmp_path: Path, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(layout["cwd"]))
    store = _store(tmp_path / "m.db")
    first = index_session_history(store, _files(layout), project_id="scope")
    spy = _DecodeSpy()
    monkeypatch.setattr(projector, "fold_history_lines", spy)

    again = index_session_history(store, _files(layout), project_id="scope")

    assert spy.decoded == {}
    assert (again.files_read, again.lines_decoded) == (0, 0)
    assert again.stages() == first.stages()
    assert [s.vendor_session_ref for s in again.sessions] == [
        s.vendor_session_ref for s in first.sessions
    ]


# ---- CR-738: a rewritten file is re-read and its rows replaced -----------------------


def _shrink(path: Path, cwd: Path) -> int:
    _write(path, _claude_lines(cwd)[:2])
    return 2


def _rewrite_head(path: Path, cwd: Path) -> int:
    lines = _claude_lines(cwd)
    lines[1] = _claude_assistant(1, "m9")
    _write(path, [*lines, _claude_assistant(6, "m4")])
    return 5


def _backdate(path: Path, cwd: Path) -> int:
    before = path.stat().st_mtime
    _append(path, [_claude_assistant(6, "m4")])
    os.utime(path, (before - 3600, before - 3600))
    return 5


@pytest.mark.parametrize(
    "change", [_shrink, _rewrite_head, _backdate], ids=["truncate", "head", "mtime"]
)
def test_truncation_rereads(tmp_path: Path, layout: dict[str, Path], change: Any) -> None:
    path = _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(layout["cwd"]))
    store = _store(tmp_path / "m.db")
    index_session_history(store, _files(layout), project_id="scope")

    lines = change(path, layout["cwd"])
    funnel = index_session_history(store, _files(layout), project_id="scope")

    assert funnel.lines_decoded == lines
    rebuilt = _store(tmp_path / "rebuilt.db")
    index_session_history(rebuilt, _files(layout), project_id="scope")
    assert _rows(store) == _rows(rebuilt)
    rows = _rows(store)
    assert len(rows["telemetry_observed_sessions"]) == 1
    assert len(rows["telemetry_observed_requests"]) == funnel.requests


def test_truncation_rereads_a_trailing_partial_line_only_once_complete(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    path = _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(layout["cwd"]))
    store = _store(tmp_path / "m.db")
    index_session_history(store, _files(layout), project_id="scope")
    partial = json.dumps(_claude_assistant(6, "m4"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[:40])

    waiting = index_session_history(store, _files(layout), project_id="scope")
    assert (waiting.lines_decoded, waiting.requests) == (0, 2)
    unchanged = index_session_history(store, _files(layout), project_id="scope")
    assert unchanged.files_read == 0

    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[40:] + "\n")
    complete = index_session_history(store, _files(layout), project_id="scope")
    assert (complete.lines_decoded, complete.requests) == (1, 3)
    assert len(_rows(store)["telemetry_observed_requests"]) == 3


# ---- CR-739: the funnel reconciles -----------------------------------------------------


def _funnel_fixture(layout: dict[str, Path]) -> None:
    cwd = layout["cwd"]
    project = layout["claude"] / "-work-repo"
    _write(project / "good.jsonl", _claude_lines(cwd))
    (project / "corrupt.jsonl").write_bytes(b"{not json\n\xff\xfe\n")
    _write(project / "idle.jsonl", [{"type": "summary", "summary": "x"}])
    unpriced = [
        _claude_user(0, cwd, session="u"),
        _claude_assistant(1, "u1", model=_UNPRICED, session="u"),
    ]
    _write(project / "unpriced.jsonl", unpriced)
    _write(layout["codex"] / "2026" / "09" / "01" / "rollout-1.jsonl", _codex_lines(cwd))


def _assert_reconciles(funnel: SweepFunnel) -> None:
    stage_drops = {stage: [d for d in funnel.drops if d.stage is stage] for stage in SweepStage}
    assert funnel.seen - funnel.parsed == len(stage_drops[SweepStage.PARSED])
    assert funnel.parsed - funnel.attributed == len(stage_drops[SweepStage.ATTRIBUTED])
    assert funnel.attributed - funnel.projected == len(stage_drops[SweepStage.PROJECTED])
    names = [drop.file_name for drop in funnel.drops]
    assert len(names) == len(set(names))
    assert funnel.requests_priced + sum(funnel.request_drops.values()) == funnel.requests


def test_funnel_reconciles(
    tmp_path: Path, layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _funnel_fixture(layout)
    store = _store(tmp_path / "m.db")

    funnel = index_session_history(store, _files(layout), project_id="scope", progress=_progress)

    assert funnel.stages() == (5, 4, 3, 3)
    assert {(d.file_name, d.stage, d.reason) for d in funnel.drops} == {
        ("corrupt.jsonl", SweepStage.PARSED, SweepDropReason.CORRUPT_RECORD),
        ("idle.jsonl", SweepStage.ATTRIBUTED, SweepDropReason.NO_OPERATOR_WORK),
    }
    assert (funnel.requests, funnel.requests_priced) == (5, 2)
    assert funnel.request_drops == {
        RequestDropReason.UNPRICED_MODEL: 1,
        RequestDropReason.UNPRICED_RUNTIME: 2,
    }
    _assert_reconciles(funnel)
    progress = capsys.readouterr().err.splitlines()
    assert progress and all(re.fullmatch(r"reflect: index \d+/5", line) for line in progress)
    assert progress[0] == "reflect: index 0/5" and progress[-1] == "reflect: index 5/5"

    again = index_session_history(store, _files(layout), project_id="scope")
    assert again.files_read == 0
    assert (again.stages(), again.requests, again.request_drops) == (
        funnel.stages(),
        funnel.requests,
        funnel.request_drops,
    )
    _assert_reconciles(again)


def test_funnel_reconciles_over_no_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    funnel = index_session_history(
        _store(tmp_path / "m.db"), [], project_id="scope", progress=_progress
    )

    assert funnel.stages() == (0, 0, 0, 0)
    assert (funnel.requests, funnel.drops, funnel.sessions) == (0, [], [])
    assert capsys.readouterr().err.splitlines() == ["reflect: index 0/0"]


def test_funnel_counts_a_vanished_file_as_corrupt(tmp_path: Path, layout: dict[str, Path]) -> None:
    gone = HistoryFile("claude", tmp_path / "gone.jsonl")

    funnel = index_session_history(_store(tmp_path / "m.db"), [gone], project_id="scope")

    assert funnel.stages() == (1, 0, 0, 0)
    assert funnel.drops[0].reason is SweepDropReason.CORRUPT_RECORD


# ---- rows, digests and detail ----------------------------------------------------------


def test_session_rows_carry_the_working_directory_digest(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    _funnel_fixture(layout)
    funnel = index_session_history(_store(tmp_path / "m.db"), _files(layout), project_id="scope")

    assert {s.cwd_digest for s in funnel.sessions} == {working_dir_digest(layout["cwd"])}
    by_runtime = {s.runtime: s for s in funnel.sessions if s.model != _UNPRICED}
    assert by_runtime["codex"].price_source is PriceSourceKind.UNPRICED
    assert by_runtime["codex"].reasoning_tokens == 9
    assert by_runtime["claude"].price_source is PriceSourceKind.LIST_RECONSTRUCTED
    assert by_runtime["claude"].turn_count == 2


def test_working_dir_digest_normalises_and_refuses_empty() -> None:
    assert working_dir_digest("/a/b/") == working_dir_digest(Path("/a/b"))
    assert working_dir_digest("/a/b") != working_dir_digest("/a/c")
    assert re.fullmatch(r"cwd-[0-9a-f]{32}", working_dir_digest("/"))
    with pytest.raises(ValueError, match="empty"):
        working_dir_digest("")


def test_history_path_digest_is_stable_per_path(tmp_path: Path) -> None:
    assert history_path_digest(tmp_path / "a") == history_path_digest(tmp_path / "a")
    assert history_path_digest(tmp_path / "a") != history_path_digest(tmp_path / "b")
    assert str(tmp_path) not in history_path_digest(tmp_path / "a")


def test_discover_history_files_spans_projects_and_skips_missing(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    assert _files(layout) == []
    _funnel_fixture(layout)
    (layout["claude"] / "-work-repo" / "nested").mkdir()
    _write(layout["claude"] / "-work-repo" / "nested" / "child.jsonl", [])
    _write(layout["claude"] / "-work-two" / "z.jsonl", [])
    _write(layout["codex"] / "2026" / "notes.jsonl", [])

    files = _files(layout)

    assert [(f.runtime, f.path.name) for f in files] == [
        ("claude", "corrupt.jsonl"),
        ("claude", "good.jsonl"),
        ("claude", "idle.jsonl"),
        ("claude", "unpriced.jsonl"),
        ("claude", "z.jsonl"),
        ("codex", "rollout-1.jsonl"),
    ]


def test_history_lines_skips_partial_and_undecodable_lines(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    path.write_bytes(b"one\n\xff\xfe\ntwo\nthree")

    assert list(history_lines(path)) == ["one", "two"]
    assert list(history_lines(tmp_path / "absent.jsonl")) == []


def test_project_history_lines_reads_one_project_since_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    repo = tmp_path / "repo"
    assert list(project_history_lines(repo, modified_since=None)) == []
    directory = claude_history_root(repo)
    old = _write(directory / "old.jsonl", [{"n": 1}])
    _write(directory / "new.jsonl", [{"n": 2}])
    _write(projects / "-elsewhere" / "x.jsonl", [{"n": 3}])
    os.utime(old, (1_000, 1_000))

    assert list(project_history_lines(repo, modified_since=None)) == ['{"n": 2}', '{"n": 1}']
    since = datetime(2000, 1, 1, tzinfo=UTC)
    assert list(project_history_lines(repo, modified_since=since)) == ['{"n": 2}']


def test_fold_line_counts_blank_malformed_and_records() -> None:
    fold = TranscriptFold(runtime="claude")
    ledger = RequestLedger()

    assert fold.fold_line(b"\n", ledger, fallback_session_id="stem") is None
    assert fold.fold_line(b"[1]\n", ledger, fallback_session_id="stem") is None
    assert fold.fold_line(b'{"type": "user"}\n', ledger, fallback_session_id="stem") == {
        "type": "user"
    }
    assert (fold.lines, fold.records, fold.skipped_lines) == (2, 1, 1)
    assert fold.vendor_session_ref == hash_vendor_session_id("stem")


def test_codex_fold_skips_repeats_and_inherited_counters() -> None:
    acc = CodexFold()
    count = _codex_count(0, (3_000_000, 0, 10, 0), None)
    assert fold_codex_record(acc, count) is None
    assert fold_codex_record(acc, count) is None
    moved = fold_codex_record(acc, _codex_count(1, (3_000_100, 50, 30, 0), None))
    assert moved is not None
    assert (moved.input_tokens, moved.cache_read_tokens, moved.output_tokens) == (50, 50, 20)
    over = fold_codex_record(acc, _codex_count(2, (3_000_200, 50, 40, 0), (2_500_000, 0, 10, 0)))
    assert over is not None and (over.input_tokens, over.output_tokens) == (100, 10)
    assert fold_codex_record(acc, {"type": "event_msg", "payload": "x"}) is None


def test_read_session_detail_reports_steps_spans_blocks_and_compactions(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    cwd = layout["cwd"]
    compact = {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": _ts(5),
        "compactMetadata": {"trigger": "auto", "preTokens": 1234},
    }
    sidechain = {**_claude_user(6, cwd, "child prompt"), "isSidechain": True}
    claude = _write(
        layout["claude"] / "-work-repo" / "a.jsonl", [*_claude_lines(cwd), compact, sidechain]
    )
    codex = _write(
        layout["codex"] / "rollout-1.jsonl",
        [
            *_codex_lines(cwd),
            {"timestamp": _ts(12), "type": "compacted", "payload": {"message": "sum"}},
        ],
    )

    detail = read_session_detail(HistoryFile("claude", claude))
    assert detail is not None
    assert [s.ordinal for s in detail.steps] == [1, 2]
    (shell,) = detail.tool_spans[DurationKind.COMMAND]
    assert (shell.label, shell.duration_ms) == ("git", 2000)
    assert [c.trigger for c in detail.compactions] == ["auto"]
    assert detail.compactions[0].pre_tokens == 1234
    assert detail.blocks[-1].lane == "sidechain"
    assert {b.kind for b in detail.blocks} >= {"user", "tool_call", "tool_result"}
    assert "child prompt" not in detail.model_dump_json()

    codex_detail = read_session_detail(HistoryFile("codex", codex))
    assert codex_detail is not None
    assert len(codex_detail.steps) == 2
    (codex_shell,) = codex_detail.tool_spans[DurationKind.COMMAND]
    assert (codex_shell.label, codex_shell.duration_ms) == ("git", 3000)
    assert [c.request_ordinal for c in codex_detail.compactions] == [2]
    assert codex_detail.blocks[0].kind == "system"

    assert read_session_detail(HistoryFile("claude", tmp_path / "absent.jsonl")) is None
    empty = _write(tmp_path / "empty.jsonl", [])
    assert read_session_detail(HistoryFile("claude", empty)) is None


@pytest.mark.parametrize(
    ("record", "version"),
    [
        ({"type": "user", "version": "2.1.235"}, "2.1.235"),
        ({"type": "session_meta", "payload": {"cli_version": "0.50.1"}}, "0.50.1"),
        ({"type": "user", "version": "2.1.235; rm -rf ~"}, None),
        ({"type": "user", "version": "2.1"}, None),
        ({"type": "user", "version": 2}, None),
        ({"type": "user"}, None),
    ],
)
def test_read_session_detail_keeps_only_a_plain_runtime_version(
    tmp_path: Path, record: dict[str, Any], version: str | None
) -> None:
    line = {**record, "sessionId": "claude-raw-id", "timestamp": _ts(0)}
    path = _write(tmp_path / "v.jsonl", [line])

    detail = read_session_detail(HistoryFile("claude", path))

    assert detail is not None
    assert detail.runtime_version == version


def _fork_rollout(dense: int, own: int) -> list[dict[str, Any]]:
    """Return a Codex rollout opening on ``dense`` copied readings, then ``own`` real ones."""
    rows: list[dict[str, Any]] = [
        {"timestamp": _ts(0), "type": "session_meta", "payload": {"id": "fork-raw-id"}},
        {"timestamp": _ts(0), "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
    ]
    total = 0
    for index in range(dense + own):
        total += 10
        stamp = (
            f"2026-09-01T10:00:00.{index * 10:03d}Z"
            if index < dense
            else _ts(10 + 5 * (index - dense))
        )
        count = _codex_count(0, (total, 0, total // 10, 0), (10, 0, 1, 0))
        rows.append({**count, "timestamp": stamp})
    return rows


@pytest.mark.parametrize(
    ("dense", "own", "billed"),
    [(25, 2, 2), (20, 1, 1), (19, 1, 20), (3, 0, 3), (25, 0, 0)],
)
def test_codex_fold_drops_a_forked_threads_copied_history(
    tmp_path: Path, dense: int, own: int, billed: int
) -> None:
    path = _write(tmp_path / "rollout-fork.jsonl", _fork_rollout(dense, own))
    fold = TranscriptFold(runtime="codex")
    ledger = RequestLedger()

    with path.open("rb") as handle:
        fold_history_lines(handle, fold, ledger, fallback_session_id="fork")

    assert len(ledger.rows) == billed
    assert fold.replayed_requests == (dense if dense >= 20 else 0)
    assert [row.ordinal for row in sorted(ledger.rows.values(), key=lambda r: r.ordinal)] == list(
        range(1, billed + 1)
    )
    assert fold.replay_open is False


def test_codex_fold_settles_a_resumed_fold_from_before_the_guard() -> None:
    fold = TranscriptFold(runtime="codex")
    ledger = RequestLedger()
    for record in _fork_rollout(0, 1):
        fold.fold_line(json.dumps(record).encode(), ledger, fallback_session_id="fork")
    fold.settle(ledger)
    restored = TranscriptFold.model_validate_json(
        fold.model_dump_json(exclude={"replay_open", "replay_pending", "replayed_requests"})
    )
    later = {**_codex_count(30, (500, 0, 50, 0), (10, 0, 1, 0))}

    restored.fold_line(json.dumps(later).encode(), ledger, fallback_session_id="fork")

    assert len(ledger.rows) == 2


def test_load_session_detail_finds_the_indexed_file(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    _funnel_fixture(layout)
    store = _store(tmp_path / "m.db")
    funnel = index_session_history(store, _files(layout), project_id="scope")
    codex_ref = next(s.vendor_session_ref for s in funnel.sessions if s.runtime == "codex")

    detail = load_session_detail(store, _files(layout), codex_ref)

    assert detail is not None and detail.vendor_session_ref == codex_ref
    assert load_session_detail(store, [], codex_ref) is None
    assert load_session_detail(store, _files(layout), "vsid-" + "0" * 32) is None


# ---- row and store contracts -----------------------------------------------------------


def _index_entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path_digest": "src-" + "a" * 32,
        "runtime": "claude",
        "size": 10,
        "mtime": 1.0,
        "byte_offset": 10,
        "head_bytes": 10,
        "head_digest": "b" * 64,
        "requests": 2,
        "requests_priced": 1,
        "requests_unpriced_model": 1,
        "fold": "{}",
        "last_scan_ts": datetime(2026, 9, 1, tzinfo=UTC),
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    "overrides",
    [
        {"byte_offset": 11},
        {"head_bytes": 11},
        {"head_bytes": 4097, "size": 5000, "byte_offset": 5000},
        {"requests_priced": 2},
        {"path_digest": "/some/path"},
        {"unknown": 1},
    ],
    ids=[
        "offset-past-size",
        "head-past-size",
        "head-over-cap",
        "drops-miss-total",
        "raw-path",
        "extra",
    ],
)
def test_history_index_entry_refuses_inconsistent_rows(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HistoryIndexEntry.model_validate(_index_entry(**overrides))


def test_history_index_entry_accepts_an_empty_file() -> None:
    row = HistoryIndexEntry.model_validate(
        _index_entry(
            size=0,
            byte_offset=0,
            head_bytes=0,
            requests=0,
            requests_priced=0,
            requests_unpriced_model=0,
        )
    )
    assert row.drop_reason is None


def _request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "vendor_session_ref": "vsid-" + "c" * 32,
        "runtime": "claude",
        "request_ref": "req-" + "d" * 32,
        "ordinal": 1,
        "input_tokens": 1,
        "cache_write_tokens": 2,
        "cache_write_1h_tokens": 2,
        "total_tokens": 3,
        "price_source": "unpriced",
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    "overrides",
    [
        {"cache_write_1h_tokens": 3},
        {"total_tokens": 4},
        {"ordinal": 0},
        {"cost_usd": "0.1"},
        {"request_ref": "m1"},
        {"text": "hello"},
    ],
    ids=["1h-over-writes", "total-mismatch", "ordinal-zero", "unpriced-cost", "raw-ref", "extra"],
)
def test_observed_request_refuses_inconsistent_rows(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ObservedRequest.model_validate(_request(**overrides))


def test_store_keyed_statements_refuse_no_or_unknown_columns(tmp_path: Path) -> None:
    store = _store(tmp_path / "m.db")
    with pytest.raises(ValueError, match="names no column"):
        store.delete_where("telemetry_observed_requests")
    with pytest.raises(ValueError, match="no column"):
        store.fetch_where("telemetry_observed_requests", ObservedRequest, nope=1)


def test_store_keyed_statements_select_and_delete_only_matches(tmp_path: Path) -> None:
    store = _store(tmp_path / "m.db")
    for ordinal, ref in enumerate(("c", "e"), start=1):
        store.upsert(
            "telemetry_observed_requests",
            ObservedRequest.model_validate(
                _request(vendor_session_ref="vsid-" + ref * 32, ordinal=ordinal)
            ),
        )

    kept = "vsid-" + "e" * 32
    store.delete_where("telemetry_observed_requests", vendor_session_ref="vsid-" + "c" * 32)
    rows = store.fetch_where("telemetry_observed_requests", ObservedRequest, runtime="claude")

    assert [r.vendor_session_ref for r in rows if isinstance(r, ObservedRequest)] == [kept]


# ---- subagent transcripts bill their parent session ----------------------------------


def _subagent_lines(
    cwd: Path, agent: str, *, first: int, shared: str | None = None
) -> list[dict[str, Any]]:
    """A subagent transcript: its records name the parent session and are sidechain."""
    task = _tool_use(f"{agent}-t", "Read", {})
    records = [
        _claude_user(first, cwd, f"work for {agent}", session="parent-raw-id"),
        _claude_assistant(
            first + 1, f"{agent}-m1", output=7, content=[task], session="parent-raw-id"
        ),
        _tool_result(first + 4, f"{agent}-t", cwd),
        _claude_assistant(first + 5, f"{agent}-m2", output=3, session="parent-raw-id"),
    ]
    if shared is not None:
        records.append(_claude_assistant(first + 6, shared, output=9, session="parent-raw-id"))
    for record in records:
        record["isSidechain"] = True
    return records


def _subagent_session(layout: dict[str, Path]) -> tuple[Path, Path, Path]:
    cwd = layout["cwd"]
    project = layout["claude"] / "-work-repo"
    parent = _write(project / "parent-raw-id.jsonl", _claude_lines(cwd, session="parent-raw-id"))
    subagents = project / "parent-raw-id" / "subagents"
    first = _write(subagents / "agent-a1.jsonl", _subagent_lines(cwd, "a1", first=10))
    # The second subagent repeats one of the first one's requests, as a
    # resumed subagent's copied history does, and one of the parent's.
    second = _write(
        subagents / "agent-a2.jsonl",
        [*_subagent_lines(cwd, "a2", first=20, shared="a1-m2"), _claude_assistant(30, "m1")],
    )
    (subagents / "agent-a1.meta.json").write_text("{}", encoding="utf-8")
    return parent, first, second


def test_subagent_transcripts_bill_the_parent_session(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    parent, first, second = _subagent_session(layout)
    files = _files(layout)
    store = _store(tmp_path / "m.db")

    funnel = index_session_history(store, files, project_id="scope")

    assert [(f.path.name, f.subagent) for f in files] == [
        (parent.name, False),
        (first.name, True),
        (second.name, True),
    ]
    assert (funnel.seen, funnel.subagent_files, funnel.projected) == (3, 2, 3)
    assert (funnel.requests, funnel.requests_shared) == (2 + 2 + 2, 2)
    assert funnel.requests == funnel.requests_priced
    rows = _rows(store)
    requests = [ObservedRequest.model_validate(r) for r in rows["telemetry_observed_requests"]]
    (session,) = [ObservedSession.model_validate(r) for r in rows["telemetry_observed_sessions"]]
    assert session.vendor_session_ref == hash_vendor_session_id("parent-raw-id")
    assert [s.vendor_session_ref for s in funnel.sessions] == [session.vendor_session_ref]
    assert len(requests) == 6 and session.turn_count == 6
    assert {r.vendor_session_ref for r in requests} == {session.vendor_session_ref}
    lanes = {r.lane for r in requests}
    assert lanes == {"main", files[1].lane, files[2].lane}
    assert all(re.fullmatch(r"agent-[0-9a-f]{32}", lane) for lane in lanes - {"main"})
    assert session.output_tokens == sum(r.output_tokens for r in requests) == 20 + 5 + 7 + 3 + 7 + 3
    assert session.cost_usd == sum(r.cost_usd or 0 for r in requests)
    assert (session.started_at, session.ended_at) == (
        datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 10, 0, 30, tzinfo=UTC),
    )
    durations = {(d["kind"], d["label"]): d["count"] for d in rows["telemetry_observed_durations"]}
    assert durations[("tool_call", "Read")] == 2
    assert durations[("tool_call", "Bash")] == 1
    detail = load_session_detail(store, files, session.vendor_session_ref)
    assert detail is not None and [s.lane for s in detail.steps] == ["main", "main"]


def test_subagent_append_equals_a_full_rebuild(
    tmp_path: Path, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent, first, _second = _subagent_session(layout)
    store = _store(tmp_path / "m.db")
    index_session_history(store, _files(layout), project_id="scope")

    extra = _claude_assistant(40, "a1-m3", output=11, session="parent-raw-id")
    extra["isSidechain"] = True
    _append(first, [extra])
    spy = _DecodeSpy()
    monkeypatch.setattr(projector, "fold_history_lines", spy)
    again = index_session_history(store, _files(layout), project_id="scope")

    assert spy.decoded == {first.name: 1}
    monkeypatch.setattr(projector, "fold_history_lines", spy._real)
    rebuilt = _store(tmp_path / "rebuilt.db")
    full = index_session_history(rebuilt, _files(layout), project_id="scope")
    assert _rows(store) == _rows(rebuilt)
    assert (
        (again.requests, again.requests_shared)
        == (full.requests, full.requests_shared)
        == (
            7,
            2,
        )
    )


def test_subagent_rewrite_replaces_only_its_own_lane(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    _parent, _first, second = _subagent_session(layout)
    store = _store(tmp_path / "m.db")
    index_session_history(store, _files(layout), project_id="scope")

    _write(second, _subagent_lines(layout["cwd"], "a2", first=20)[:2])
    funnel = index_session_history(store, _files(layout), project_id="scope")

    rebuilt = _store(tmp_path / "rebuilt.db")
    index_session_history(rebuilt, _files(layout), project_id="scope")
    assert _rows(store) == _rows(rebuilt)
    requests = [
        ObservedRequest.model_validate(r) for r in _rows(store)["telemetry_observed_requests"]
    ]
    assert len(requests) == funnel.requests == 2 + 2 + 1
    assert sum(1 for r in requests if r.lane == "main") == 2
    assert funnel.requests_shared == 0


def test_subagent_without_billed_work_drops_at_attribution(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    _subagent_session(layout)
    idle = layout["claude"] / "-work-repo" / "parent-raw-id" / "subagents" / "agent-a3.jsonl"
    _write(idle, _subagent_lines(layout["cwd"], "a3", first=50)[:1])
    store = _store(tmp_path / "m.db")

    funnel = index_session_history(store, _files(layout), project_id="scope")

    _assert_reconciles(funnel)
    assert [(d.file_name, d.stage) for d in funnel.drops] == [
        ("agent-a3.jsonl", SweepStage.ATTRIBUTED)
    ]
    assert funnel.subagent_files == 3


def test_discover_claude_project_lists_own_files_before_subagents(tmp_path: Path) -> None:
    project = tmp_path / "-work-repo"
    _write(project / "s" / "subagents" / "agent-x.jsonl", [])
    (project / "s" / "subagents" / "agent-x.meta.json").write_text("{}", encoding="utf-8")
    _write(project / "s" / "subagents" / "other.jsonl", [])
    _write(project / "s.jsonl", [])

    files = discover_claude_project(project)

    assert [(f.path.name, f.subagent) for f in files] == [
        ("s.jsonl", False),
        ("agent-x.jsonl", True),
    ]
    assert [f.fallback_session_id for f in files] == ["s", "s"]
    assert files[0].lane == "main" and files[1].lane.startswith("agent-")
    assert "agent-x" not in files[1].lane.removeprefix("agent-")
    assert discover_claude_project(tmp_path / "missing") == []


def test_parsed_transcript_merged_refuses_no_folds() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ParsedTranscript.merged([], [])


def test_parsed_transcript_merged_sums_durations_across_lanes() -> None:
    main = TranscriptFold(runtime="claude")
    sub = TranscriptFold(runtime="claude", lane="agent-" + "a" * 32)
    for fold, ms in ((main, 100), (sub, 400)):
        fold.tallies["tool_call/Read"] = DurationTally(
            kind=DurationKind.TOOL_CALL, label="Read", count=1, total_ms=ms, max_ms=ms
        )
    sub.cwd_digest = "cwd-" + "b" * 32

    merged = ParsedTranscript.merged([sub, main], [])

    tally = merged.durations[(DurationKind.TOOL_CALL, "Read")]
    assert (tally.count, tally.total_ms, tally.max_ms) == (2, 500, 400)
    assert main.tallies["tool_call/Read"].count == 1
    assert merged.cwd_digest == "cwd-" + "b" * 32


# ---- concurrent runs on one index ------------------------------------------------------


def test_history_index_lock_waits_for_the_run_holding_it(tmp_path: Path) -> None:
    index = tmp_path / "history-index.sqlite"
    waited: list[str] = []
    order: list[str] = []
    holding = threading.Event()
    release = threading.Event()

    def first() -> None:
        with history_index_lock(index, on_wait=lambda: waited.append("first")):
            order.append("first")
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=first)
    holder.start()
    assert holding.wait(10)
    threading.Timer(0.3, release.set).start()
    with history_index_lock(index, on_wait=lambda: waited.append("second")):
        order.append("second")
    holder.join(10)

    assert waited == ["second"]
    assert order == ["first", "second"]


def test_history_index_lock_gives_up_with_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "history-index.sqlite"
    monkeypatch.setattr(projector, "INDEX_LOCK_WAIT_SECONDS", 0.2)
    holding = threading.Event()
    release = threading.Event()

    def first() -> None:
        with history_index_lock(index, on_wait=lambda: None):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=first)
    holder.start()
    assert holding.wait(10)
    try:
        with (
            pytest.raises(IndexBusyError, match="another reflect run"),
            history_index_lock(index, on_wait=lambda: None),
        ):
            pass
    finally:
        release.set()
        holder.join(10)


def test_concurrent_index_runs_on_one_index_both_complete(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    _subagent_session(layout)
    _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(layout["cwd"]))
    index = tmp_path / "history-index.sqlite"
    results: dict[str, tuple[int, ...]] = {}
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def run(name: str) -> None:
        try:
            start.wait(10)
            with history_index_lock(index, on_wait=lambda: None):
                store = SqliteMetricsStore(index)
                try:
                    store.init_schema()
                    funnel = index_session_history(store, _files(layout), project_id="scope")
                finally:
                    store.close()
            results[name] = (*funnel.stages(), funnel.requests)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert results["a"] == results["b"] == (4, 4, 4, 4, 8)


# ---- title helper sessions stay out of usage ------------------------------------------


@pytest.mark.parametrize("as_blocks", [False, True], ids=["string", "text-block"])
def test_reflection_helper_sessions_are_dropped_not_billed(
    tmp_path: Path, layout: dict[str, Path], as_blocks: bool
) -> None:
    cwd = layout["cwd"]
    _write(layout["claude"] / "-work-repo" / "a.jsonl", _claude_lines(cwd))
    prompt = f"{REFLECTION_MARKER} Write one imperative noun phrase\nfacts"
    content: object = [{"type": "text", "text": prompt}] if as_blocks else prompt
    helper = _write(
        layout["claude"] / "-tmp-eawf-reflect-title-x" / "h.jsonl",
        [
            _claude_user(0, tmp_path, content, session="helper"),
            _claude_assistant(1, "h1", session="helper"),
        ],
    )
    store = _store(tmp_path / "m.db")

    funnel = index_session_history(store, _files(layout), project_id="scope")

    _assert_reconciles(funnel)
    assert [(d.file_name, d.stage, d.reason) for d in funnel.drops] == [
        (helper.name, SweepStage.ATTRIBUTED, SweepDropReason.REFLECTION_HELPER)
    ]
    rows = _rows(store)
    helper_ref = hash_vendor_session_id("helper")
    assert all(row["vendor_session_ref"] != helper_ref for table in rows.values() for row in table)
    assert (funnel.requests, len(funnel.sessions)) == (2, 1)


def test_a_marker_after_the_opening_prompt_does_not_drop_a_session(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    cwd = layout["cwd"]
    lines = _claude_lines(cwd)
    lines.insert(2, _claude_user(2, cwd, f"what does {REFLECTION_MARKER} mean?"))
    _write(layout["claude"] / "-work-repo" / "a.jsonl", lines)

    funnel = index_session_history(_store(tmp_path / "m.db"), _files(layout), project_id="s")

    assert (funnel.drops, funnel.projected, funnel.requests) == ([], 1, 2)


def test_local_history_discovers_indexes_and_reads_detail(
    tmp_path: Path, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _subagent_session(layout)
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(layout["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    history = LocalHistory.discover()
    store = _store(tmp_path / "m.db")

    funnel = history.index(store, project_id="scope")

    assert history.file_count == len(history.files) == funnel.seen == 3
    ref = hash_vendor_session_id("parent-raw-id")
    detail = history.detail(store, ref)
    assert detail is not None and detail.vendor_session_ref == ref
    assert history.detail(store, "vsid-" + "0" * 32) is None


def test_local_history_over_missing_directories_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent-codex"))

    history = LocalHistory.discover()

    assert (history.files, history.file_count) == ((), 0)
    assert history.index(_store(tmp_path / "m.db"), project_id="scope").stages() == (0, 0, 0, 0)
