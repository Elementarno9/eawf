"""Tests for the ledger history gates over a real repository and through the hook.

The append-only gate reds on the in-place SHA remap a history rewrite once applied
to a committed batch ledger line; the reachability gate reds on a commit an amend
left dangling, passes over this repository's stray SHAs (they name no commit in a
fresh repository) and exempts a commit a row declares in ``unreachable_shas``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.store.ledger import LedgerRecord, render_ledger_line, split_ledger_lines
from eawf.platform.lint.ledger_history import (
    LedgerHistoryError,
    append_only_finding,
    scan_ledger_history,
)
from eawf.surfaces.cli.app import app

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "ledger_history"
_LEDGER = ".ea/generations/gen-ca819f3245bf71a8/ledger/batch.jsonl"
_TASK_LEDGER = ".ea/generations/gen-x/ledger/task.jsonl"

runner = CliRunner()


def _content(lines: list[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


def _stray_lines() -> list[str]:
    return list(split_ledger_lines((_FIXTURES / "task_stray.jsonl").read_bytes()))


def _declaration(sha: str) -> list[dict[str, str]]:
    return [{"sha": sha, "archive_ref": "refs/tags/archive/x", "reason": "squashed away"}]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_commit(repo: Path, rel: str, content: bytes, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository on ``main`` with one commit, isolated from any git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "canary@example.com")
    _git(root, "config", "user.name", "canary")
    _write_commit(root, "README", b"r\n", "chore: initialize")
    return root


def _row(key: str, **payload: object) -> str:
    return render_ledger_line(
        LedgerRecord.model_validate(
            {
                "collection": "task",
                "record_key": key,
                "status": "COMPLETED",
                "recorded_at": "2026-10-01T00:00:00Z",
                "payload": payload,
            }
        )
    )


def _invoke(repo: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(
        app, ["--json", "--workspace", str(repo), "hook", "ledger-history", *args]
    )
    return result.exit_code, orjson.loads(result.stdout)


def test_reachability_reds_on_a_commit_an_amend_left_dangling(repo: Path) -> None:
    live = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    dangling = _write_commit(repo, "b.txt", b"b\n", "feat: b")
    _git(repo, "commit", "-q", "--amend", "-m", "feat: b, amended")
    _git(repo, "branch", "base", live)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    declared = _write_commit(repo, "c.txt", b"c\n", "feat: c")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    strays = _stray_lines()
    rows = [
        *strays,
        _row("T-1", commit=dangling),
        _row("T-2", commit=live, tree_sha=tree),
        _row("T-3", commit=declared, unreachable_shas=_declaration(declared)),
    ]
    _write_commit(repo, _TASK_LEDGER, _content(rows), "state: record")

    report = scan_ledger_history(repo, "base..HEAD")
    # The real strays name no commit here, the tree is no commit, and the
    # declared commit is exempt: only the dangling commit is reported.
    assert [(f.line, f.record_key, f.sha) for f in report.unreachable] == [
        (len(strays) + 1, "T-1", dangling)
    ]
    assert report.append_only == ()

    code, payload = _invoke(repo, "--range", "base..HEAD")
    assert code == 1
    assert payload["clean"] is False
    assert [f["sha"] for f in payload["unreachable"]] == [dangling]


def test_append_only_reds_through_the_hook_on_a_rewritten_line(repo: Path) -> None:
    before = (_FIXTURES / "batch_before.jsonl").read_bytes()
    after = (_FIXTURES / "batch_remapped.jsonl").read_bytes()
    _write_commit(repo, _LEDGER, before, "state: record")
    _git(repo, "branch", "base")
    _write_commit(repo, _LEDGER, after, "state: remap")

    code, payload = _invoke(repo, "--range", "base..HEAD")
    assert code == 1
    expected = append_only_finding(_LEDGER, before, after)
    assert expected is not None
    assert payload["append_only"] == [expected.model_dump(mode="json")]


def test_append_only_passes_an_appended_row_in_the_working_tree(repo: Path) -> None:
    content = _content([_row("T-1")])
    _write_commit(repo, _TASK_LEDGER, content, "state: record")
    (repo / _TASK_LEDGER).write_bytes(content + _content([_row("T-2")]))
    code, payload = _invoke(repo, "--range", "HEAD")
    assert (code, payload["clean"], payload["tip"]) == (0, True, "WORKTREE")
    (repo / _TASK_LEDGER).write_bytes(_content([_row("T-0")]))
    code, payload = _invoke(repo, "--range", "HEAD")
    assert code == 1
    assert len(payload["append_only"]) == 1


def test_reachability_hook_refuses_an_unknown_rev_and_a_malformed_range(repo: Path) -> None:
    with pytest.raises(LedgerHistoryError, match="no-such-rev"):
        scan_ledger_history(repo, "no-such-rev..HEAD")
    result = runner.invoke(
        app, ["--workspace", str(repo), "hook", "ledger-history", "--range", "a...b"]
    )
    assert result.exit_code == 2
