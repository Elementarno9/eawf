"""``eawf reflect run`` analyses local session history beside the report.

- Every Claude project's transcripts and every Codex rollout are indexed into
  the local reflection collection, this project's sessions are read in full, and
  the analysis lands as ``analysis-<date>.json`` with Codex shown as tokens only.
- With no history redirected, the suite's isolation leaves nothing to index.
- ``--local-only`` titles this project's sessions without calling out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.observability.reflect.analysis import ANALYSIS_PREFIX, HISTORY_INDEX_FILENAME
from eawf.observability.reflect.report import reflect_root
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import native_tree, refuse_egress, run_row

__all__ = ["refuse_egress"]

runner = CliRunner()


def _lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def _claude(cwd: Path, session: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": session,
            "cwd": str(cwd),
            "timestamp": "2026-09-01T10:00:00Z",
            "message": {"content": "synthetic request"},
        }
    ]
    for index in range(3):
        rows.append(
            {
                "type": "assistant",
                "sessionId": session,
                "timestamp": f"2026-09-01T10:00:0{index + 1}Z",
                "message": {
                    "id": f"{session}-m{index}",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
    return rows


def _codex(cwd: Path) -> list[dict[str, Any]]:
    usage = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}
    return [
        {
            "timestamp": "2026-09-02T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "synthetic-codex", "cwd": str(cwd)},
        },
        {
            "timestamp": "2026-09-02T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-6.1-sol"},
        },
        {
            "timestamp": "2026-09-02T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": usage, "last_token_usage": usage},
            },
        },
    ]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree beside a synthetic Claude and Codex history, both redirected."""
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    root = native_tree(repo, [run_row("RUN-00000001")], [])
    projects = tmp_path / "harness" / "projects"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    project_dir = projects / "-synthetic-repo"
    _lines(project_dir / "own.jsonl", _claude(repo.resolve(), "own-session"))
    _lines(
        projects / "-elsewhere" / "other.jsonl", _claude(tmp_path / "elsewhere", "other-session")
    )
    _lines(codex_home / "sessions" / "2026" / "rollout-1.jsonl", _codex(tmp_path / "elsewhere"))
    return root


def _run(tree: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app, ["-w", str(tree.parent), "--json", "reflect", "run", "--local-only", *args]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    payload: dict[str, Any] = envelope["result"]
    return payload


def test_reflect_run_indexes_no_real_history_under_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    root = native_tree(repo, [run_row("RUN-00000001")], [])

    payload = _run(root)

    analysis = json.loads(Path(payload["analysis"]).read_text(encoding="utf-8"))
    assert analysis["sessions"] == []
    assert analysis["usage"]["sessions"] == 0


def test_reflect_run_sessions_writes_the_history_analysis(tree: Path) -> None:
    payload = _run(tree)

    path = Path(payload["analysis"])
    assert path.parent == reflect_root(tree)
    assert path.name.startswith(ANALYSIS_PREFIX)
    analysis = json.loads(path.read_text(encoding="utf-8"))
    sessions = analysis["sessions"]
    assert len(sessions) == 3
    assert {row["runtime"] for row in sessions} == {"claude", "codex"}
    codex = next(row for row in analysis["rates"] if row["model"] == "gpt-6.1-sol")
    # The price file has a row for the Codex model, so its cost counts and ranks.
    assert codex["cost_usd"] is not None
    assert codex["quality"] == "verified"
    assert any(row["model"] == "gpt-6.1-sol" for row in analysis["model_cost_ranking"])
    own = [row for row in sessions if row["title"] is not None]
    assert len(own) == 1
    assert own[0]["title"]["source"] == "structural"
    assert own[0]["context"]
    assert (reflect_root(tree) / HISTORY_INDEX_FILENAME).exists()
    text = path.read_text(encoding="utf-8")
    assert "synthetic request" not in text
    assert str(tree.parent) not in text


def test_reflect_run_sessions_is_incremental(tree: Path) -> None:
    first = Path(_run(tree)["analysis"]).read_text(encoding="utf-8")
    second = Path(_run(tree)["analysis"]).read_text(encoding="utf-8")

    assert json.loads(first)["usage"] == json.loads(second)["usage"]


def test_reflect_run_names_a_legacy_collection_it_does_not_read(tree: Path) -> None:
    legacy = tree / "local" / "reflect"
    legacy.mkdir(parents=True)
    (legacy / HISTORY_INDEX_FILENAME).write_bytes(b"")

    result = runner.invoke(
        app, ["-w", str(tree.parent), "--json", "reflect", "run", "--local-only"]
    )

    assert result.exit_code == 0, result.output
    assert (
        "reflect: .ea/local/reflect holds reflect data of an earlier version that nothing reads;"
        " run eawf local tidy to move it to .ea/local/reports/reflect"
    ) in result.stderr.splitlines()
    assert (legacy / HISTORY_INDEX_FILENAME).exists()


def test_reflect_run_without_a_legacy_collection_says_nothing_of_it(tree: Path) -> None:
    result = runner.invoke(
        app, ["-w", str(tree.parent), "--json", "reflect", "run", "--local-only"]
    )

    assert result.exit_code == 0, result.output
    assert "eawf local tidy" not in result.stderr
