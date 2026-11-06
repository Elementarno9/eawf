"""One reflect run's session step: index local history, analyse it, title and page it.

The step is a library call the command handler dispatches to; these tests drive it
directly over a native tree and a synthetic history redirected under ``tmp_path``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import eawf.observability.telemetry.projector as projector
from eawf.observability.reflect.analysis import HISTORY_INDEX_FILENAME
from eawf.observability.reflect.report import ReportScope, reflect_root
from eawf.observability.reflect.runs import tree_project_code
from eawf.observability.reflect.scopes import ForeignRowsRefusedError, OutputArea
from eawf.observability.reflect.sessions import SessionRun, reflect_sessions
from eawf.observability.reflect.titles import CallBudget, TitleCache
from eawf.observability.telemetry.projector import (
    IndexBusyError,
    LocalHistory,
    history_index_lock,
)
from tests.contract.surfaces.cli._reflect_tree import native_tree
from tests.integration.observability.reflect.test_reflect_totals import own_project

_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def _claude(cwd: Path, session: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "sessionId": session,
            "cwd": str(cwd),
            "timestamp": "2026-09-01T10:00:00Z",
            "message": {"content": "synthetic request"},
        },
        {
            "type": "assistant",
            "sessionId": session,
            "timestamp": "2026-09-01T10:00:01Z",
            "message": {
                "id": f"{session}-m1",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository holding a tree with no Runs, beside a redirected history."""
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    native_tree(repo, [], [])
    projects = tmp_path / "harness" / "projects"
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _lines(projects / "-own" / "own.jsonl", _claude(repo.resolve(), "own-session"))
    _lines(projects / "-other" / "other.jsonl", _claude(tmp_path / "elsewhere", "other-session"))
    return repo


def _run(repo: Path, area: OutputArea, kind: str = "project", **overrides: Any) -> SessionRun:
    tree_root = repo.resolve() / ".ea"
    arguments: dict[str, Any] = {
        "report_scope": ReportScope(kind=kind),  # type: ignore[arg-type]
        "area": area,
        "history": LocalHistory.discover(),
        "harness_store": repo / "no-harness-store",
        "cache": TitleCache(),
        "provider": None,
        "budget": CallBudget(),
        "local_only": True,
        "now": _NOW,
        "on_wait": lambda: None,
    }
    arguments.update(overrides)
    return reflect_sessions(tree_root, **arguments)


def _sessions(run: SessionRun) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(run.analysis.read_text(encoding="utf-8"))["sessions"]
    return rows


def test_reflect_sessions_labels_own_sessions_with_the_tree_project_code(
    repo: Path, tmp_path: Path
) -> None:
    own_project(repo.resolve() / ".ea", "RFL")
    area = OutputArea.of(tmp_path / "out", tree_root=repo.resolve() / ".ea")

    run = _run(repo, area, kind="user")

    labels = sorted(row["project_label"] for row in _sessions(run))
    assert labels == ["RFL", "unknown"]
    assert run.overview.parent == area.directory == run.analysis.parent
    assert (area.directory / HISTORY_INDEX_FILENAME).is_file()
    assert run.account.digests_sent == 0 and run.account.local_only


def test_reflect_sessions_falls_back_to_the_repository_name_without_a_project_row(
    repo: Path, tmp_path: Path
) -> None:
    area = OutputArea.of(tmp_path / "out", tree_root=repo.resolve() / ".ea")

    run = _run(repo, area)

    assert sorted(row["project_label"] for row in _sessions(run)) == ["synthetic-repo", "unknown"]


def test_reflect_sessions_over_no_history_writes_an_empty_analysis(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "absent"))
    area = OutputArea.of(reflect_root(repo.resolve() / ".ea"), tree_root=repo.resolve() / ".ea")

    run = _run(repo, area, kind="user")

    assert _sessions(run) == []
    assert run.cache.entries == {}


def test_reflect_sessions_refuses_foreign_rows_before_writing_a_committed_area(
    repo: Path, tmp_path: Path
) -> None:
    (repo / ".git").mkdir()
    area = OutputArea(directory=repo / "reports", committed=True)

    with pytest.raises(ForeignRowsRefusedError, match="1 session rows"):
        _run(repo, area, kind="user")

    assert not (repo / "reports").exists()
    assert (reflect_root(repo.resolve() / ".ea") / HISTORY_INDEX_FILENAME).is_file()


def test_reflect_sessions_waits_then_gives_up_on_a_held_index(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    area = OutputArea.of(tmp_path / "out", tree_root=repo.resolve() / ".ea")
    area.directory.mkdir(parents=True)
    monkeypatch.setattr(projector, "INDEX_LOCK_WAIT_SECONDS", 0.2)
    waited: list[bool] = []
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with history_index_lock(area.directory / HISTORY_INDEX_FILENAME, on_wait=lambda: None):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(10)
    try:
        with pytest.raises(IndexBusyError):
            _run(repo, area, on_wait=lambda: waited.append(True))
    finally:
        release.set()
        holder.join(10)
    assert waited == [True]


def test_tree_project_code_reads_the_project_row_or_nothing(repo: Path, tmp_path: Path) -> None:
    tree_root = repo.resolve() / ".ea"
    assert tree_project_code(tree_root) is None
    own_project(tree_root, "RFL", "ABC")
    assert tree_project_code(tree_root) == "ABC"
    assert tree_project_code(tmp_path / "no-tree" / ".ea") is None
