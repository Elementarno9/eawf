"""Reflect reads session history only through the one session-history reader.

The AST scan proves no reflect module can reach a history file by itself: it
imports neither runtime-specific source, nor the discovery helpers that hand
out history paths, nor names a history location. The row scan proves the
rows the reader writes keep no path, command or message text.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel

import eawf.observability.reflect as reflect_package
from eawf.observability.telemetry.models import ObservedRequest, ObservedSession
from eawf.observability.telemetry.projector import index_session_history
from eawf.observability.telemetry.sources.session_history import (
    HistoryFile,
    working_dir_digest,
)
from eawf.observability.telemetry.store import SqliteMetricsStore

_REFLECT_ROOT: Final = Path(reflect_package.__file__).parent
_SOURCES: Final = "eawf.observability.telemetry.sources"
_READER: Final = f"{_SOURCES}.session_history"

#: Modules that open history files or hand out the paths to them.
_FORBIDDEN_MODULES: Final = frozenset(
    {
        f"{_SOURCES}.claude_session",
        f"{_SOURCES}.codex_session",
        "eawf.runtime.runtimes.claude.transcript_counters",
        "eawf.runtime.session.transcript_index",
    }
)

#: Reader names that return a history path rather than what the file holds.
_PATH_NAMES: Final = frozenset(
    {
        "claude_history_root",
        "claude_projects_root",
        "codex_sessions_root",
        "discover_history",
        "discover_history_files",
        "HistoryFile",
    }
)

#: Strings that name where a runtime keeps its history.
_LOCATION_MARKERS: Final = (
    ".claude",
    ".codex",
    "rollout-",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "EAWF_CLAUDE_PROJECTS_DIR",
)


def _violations(source: str) -> list[str]:
    """Return each way *source* could reach a history file without the reader."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name in _FORBIDDEN_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _FORBIDDEN_MODULES:
                found.append(f"from {node.module}")
            elif node.module == _SOURCES:
                found += [
                    f"from {_SOURCES} import {a.name}"
                    for a in node.names
                    if f"{_SOURCES}.{a.name}" in _FORBIDDEN_MODULES
                ]
            elif node.module == _READER:
                found += [f"{_READER}.{a.name}" for a in node.names if a.name in _PATH_NAMES]
        elif isinstance(node, ast.Attribute) and node.attr in _PATH_NAMES:
            found.append(f"attribute {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [f"literal {m!r}" for m in _LOCATION_MARKERS if m in node.value]
    return found


def _reflect_modules() -> list[Path]:
    return sorted(_REFLECT_ROOT.rglob("*.py"))


def test_single_history_reader_reflect_modules_reach_history_only_through_the_reader() -> None:
    modules = _reflect_modules()
    assert modules, "the reflect package holds no module to scan"

    offenders = {
        str(path.relative_to(_REFLECT_ROOT)): found
        for path in modules
        if (found := _violations(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "from eawf.observability.telemetry.sources.codex_session import CodexSessionSource",
        "import eawf.observability.telemetry.sources.claude_session",
        "from eawf.observability.telemetry.sources import claude_session",
        "from eawf.observability.telemetry.sources.session_history import discover_history",
        "from eawf.observability.telemetry.sources.session_history import HistoryFile",
        "from eawf.runtime.runtimes.claude.transcript_counters import projects_root",
        "import x\nx.claude_history_root(root)",
        "root = home / '.codex' / 'sessions'",
        "os.environ.get('CLAUDE_CONFIG_DIR')",
    ],
)
def test_single_history_reader_scan_catches_each_route(source: str) -> None:
    assert _violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "from eawf.observability.telemetry.sources.session_history import history_lines",
        "from eawf.observability.telemetry.sources import session_history",
        "path.read_text(encoding='utf-8')",
    ],
)
def test_single_history_reader_scan_admits_the_reader_api(source: str) -> None:
    assert _violations(source) == []


def test_single_history_reader_scan_refuses_unparsable_source() -> None:
    with pytest.raises(SyntaxError):
        _violations("def broken(:")


_SECRET_COMMAND: Final = "tee notes.txt <<'EOF'\nprivate operator words\nEOF"
_SECRET_PROMPT: Final = "please refactor the billing module"
_SECRET_BRANCH: Final = "feature/private-branch-name"


def _history(tmp_path: Path, cwd: Path) -> list[HistoryFile]:
    claude = tmp_path / "claude-projects"
    codex = tmp_path / "codex-sessions"
    claude_lines: list[dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": "claude-raw-id",
            "cwd": str(cwd),
            "timestamp": "2026-09-01T10:00:00Z",
            "message": {"content": _SECRET_PROMPT},
        },
        {
            "type": "assistant",
            "sessionId": "claude-raw-id",
            "cwd": str(cwd),
            "timestamp": "2026-09-01T10:00:01Z",
            "message": {
                "id": "msg-raw-id",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "text", "text": _SECRET_PROMPT},
                    {
                        "type": "tool_use",
                        "id": "tool-raw-id",
                        "name": "Bash",
                        "input": {"command": _SECRET_COMMAND},
                    },
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        },
    ]
    codex_lines: list[dict[str, Any]] = [
        {
            "timestamp": "2026-09-01T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "codex-raw-id",
                "cwd": str(cwd),
                "git": {"branch": _SECRET_BRANCH},
            },
        },
        {
            "timestamp": "2026-09-01T10:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": _SECRET_PROMPT},
        },
        {
            "timestamp": "2026-09-01T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 9, "output_tokens": 2}},
            },
        },
    ]
    subagent_lines = [
        {**line, "isSidechain": True, "agentId": "agent-raw-id"} for line in claude_lines
    ]
    subagent_lines[1] = {
        **subagent_lines[1],
        "message": {**claude_lines[1]["message"], "id": "sub-msg-raw-id"},
    }
    files = [
        HistoryFile("claude", claude / "-work-repo" / "s.jsonl"),
        HistoryFile(
            "claude",
            claude / "-work-repo" / "s" / "subagents" / "agent-raw-id.jsonl",
            subagent=True,
        ),
        HistoryFile("codex", codex / "rollout-1.jsonl"),
    ]
    for file, lines in zip(files, (claude_lines, subagent_lines, codex_lines), strict=True):
        file.path.parent.mkdir(parents=True)
        file.path.write_text("".join(f"{json.dumps(line)}\n" for line in lines), encoding="utf-8")
    return files


def _index_cells(db: Path) -> dict[str, list[str]]:
    """Return every cell of every table of the index database, as text, by table."""
    connection = sqlite3.connect(db)
    try:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            name: [str(cell) for row in connection.execute(f"SELECT * FROM {name}") for cell in row]
            for name in names
        }
    finally:
        connection.close()


def test_single_history_reader_rows_hold_no_path_command_or_text(tmp_path: Path) -> None:
    cwd = tmp_path / "work" / "repo"
    db = tmp_path / "m.db"
    store = SqliteMetricsStore(db)
    store.init_schema()

    funnel = index_session_history(store, _history(tmp_path, cwd), project_id="scope")
    store.close()

    cells = _index_cells(db)
    assert funnel.projected == 3
    assert {"telemetry_observed_sessions", "telemetry_history_index"} <= set(cells)
    assert cells["telemetry_history_index"], "the index stored no file row to scan"
    leaks = {
        (table, secret)
        for table, values in cells.items()
        for secret in (
            str(tmp_path),
            str(cwd),
            _SECRET_PROMPT,
            _SECRET_BRANCH,
            "private operator words",
            "claude-raw-id",
            "codex-raw-id",
            "msg-raw-id",
            "sub-msg-raw-id",
            "tool-raw-id",
            "agent-raw-id",
            "rollout-1",
        )
        if any(secret in value for value in values)
    }
    assert leaks == set()
    store = SqliteMetricsStore(db)
    tables: tuple[tuple[str, type[BaseModel]], ...] = (
        ("telemetry_observed_sessions", ObservedSession),
        ("telemetry_observed_requests", ObservedRequest),
    )
    rows = [row for table, model in tables for row in store.fetch_all(table, model)]
    store.close()
    assert {type(row) for row in rows} == {ObservedSession, ObservedRequest}
    sessions = [row for row in rows if isinstance(row, ObservedSession)]
    assert {row.cwd_digest for row in sessions} == {working_dir_digest(cwd)}


@pytest.mark.parametrize("model", [ObservedSession, ObservedRequest])
def test_single_history_reader_row_fields_name_no_path_command_or_text(
    model: type[BaseModel],
) -> None:
    location_fields = {
        name
        for name in model.model_fields
        if any(part in name for part in ("path", "command", "text", "cwd", "content", "prompt"))
    }

    assert location_fields <= {"cwd_digest"}
