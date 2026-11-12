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

from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    render_ledger_line,
    snapshot_name,
    split_ledger_lines,
)
from eawf.platform.lint.ledger_history import (
    LedgerHistoryError,
    append_only_finding,
    scan_ledger_history,
)
from eawf.surfaces.cli.app import app

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "ledger_history"
_LEDGER = ".ea/generations/gen-ca819f3245bf71a8/ledger/batch.jsonl"
_TASK_LEDGER = ".ea/generations/gen-x/ledger/task.jsonl"
_HISTORY = ".ea/generations/gen-x/history/change.jsonl"

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


# --- a history mention a ledger rebind covers ----------------------------------


def _superseding(line: str, **payload: object) -> str:
    """Return a row superseding *line* with its payload replaced by *payload*."""
    record = LedgerRecord.model_validate_json(line)
    return render_ledger_line(
        record.model_copy(update={"supersedes": line_digest(line), "payload": payload})
    )


def _history_row(change_id: str, sha: str) -> str:
    """Return a change record line naming *sha* as the completed Task's head."""
    change = {"field": "integrated_binding", "before": None, "after": {"value": {"head_sha": sha}}}
    return orjson.dumps({"change_id": change_id, "changes": [change]}).decode()


def _dangling(repo: Path, message: str) -> str:
    """Write a commit on top of HEAD that no branch reaches, as a squash leaves one."""
    return _git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", message)


def _scan(repo: Path, ledger: list[str], history: list[str]) -> list[tuple[str, int, str]]:
    """Commit both files on a new tip and return the unreachable findings as tuples."""
    _git(repo, "branch", "base")
    target = repo / _HISTORY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_content(history))
    _git(repo, "add", "--", _HISTORY)
    _write_commit(repo, _TASK_LEDGER, _content(ledger), "state: rebind")
    report = scan_ledger_history(repo, "base..HEAD")
    assert report.append_only == ()
    return [(f.path, f.line, f.sha) for f in report.unreachable]


def test_a_history_mention_of_a_commit_rebound_to_a_reachable_one_passes(repo: Path) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    landed = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    bound = _row("T-1", head_sha=source, reason=f"read at {source}")

    findings = _scan(
        repo,
        [bound, _superseding(bound, head_sha=landed, reason=f"read at {landed}")],
        [_history_row("c-1", source)],
    )

    assert findings == []


@pytest.mark.parametrize("case", ["never-rebound", "rebound-to-unreachable", "dropped"])
def test_an_uncovered_history_mention_is_still_unreachable(repo: Path, case: str) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    elsewhere = _dangling(repo, "feat: a, squashed elsewhere")
    bound = _row("T-1", head_sha=source, reason=f"read at {source}")
    ledger = {
        "never-rebound": [],
        "rebound-to-unreachable": [
            bound,
            _superseding(bound, head_sha=elsewhere, reason=f"read at {elsewhere}"),
        ],
        # A dropped commit leaves text in its place, so the row is no rebind.
        "dropped": [bound, _superseding(bound, head_sha=None, reason="read at a dropped commit")],
    }[case]

    findings = _scan(repo, ledger, [_history_row("c-1", source)])

    assert (_HISTORY, 1, source) in findings
    if case == "rebound-to-unreachable":
        assert findings == [(_HISTORY, 1, source), (_TASK_LEDGER, 2, elsewhere)]
    else:
        assert findings == [(_HISTORY, 1, source)]


def test_a_ledger_row_naming_a_rebound_commit_keeps_the_strict_rule(repo: Path) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    landed = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    bound = _row("T-1", head_sha=source)

    findings = _scan(
        repo,
        [bound, _superseding(bound, head_sha=landed), _row("T-2", head_sha=source)],
        [_history_row("c-1", source)],
    )

    # The history mention is covered; the effective ledger row is not.
    assert findings == [(_TASK_LEDGER, 3, source)]


def test_a_rewritten_history_line_still_fails_the_append_only_check(repo: Path) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    landed = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    bound = _row("T-1", head_sha=source)
    ledger = _content([bound, _superseding(bound, head_sha=landed)])
    _write_commit(repo, _TASK_LEDGER, ledger, "state: rebind")
    before = _content([_history_row("c-1", source)])
    _write_commit(repo, _HISTORY, before, "state: close")
    _git(repo, "branch", "base")
    after = _content([_history_row("c-1", landed)])
    _write_commit(repo, _HISTORY, after, "state: rewrite the history")

    report = scan_ledger_history(repo, "base..HEAD")

    assert [(f.path, f.detail) for f in report.append_only] == [
        (_HISTORY, "rewrites a committed line")
    ]


def test_a_rebind_of_a_line_sealed_in_the_snapshot_covers_the_history(repo: Path) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    landed = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    bound = _row("T-1", head_sha=source)
    sealed = _content([bound])
    snapshot = f"{Path(_TASK_LEDGER).parent}/snapshots/{snapshot_name('task', sealed)}"
    (repo / snapshot).parent.mkdir(parents=True, exist_ok=True)
    (repo / snapshot).write_bytes(sealed)
    _git(repo, "add", "--", snapshot)

    # The superseded line lives only in the snapshot; the live file holds its rebind.
    findings = _scan(repo, [_superseding(bound, head_sha=landed)], [_history_row("c-1", source)])

    assert findings == []


def test_a_commit_rebound_to_one_reachable_and_one_unreachable_commit_stays_reported(
    repo: Path,
) -> None:
    source = _dangling(repo, "feat: a, before the squash")
    elsewhere = _dangling(repo, "feat: a, squashed elsewhere")
    landed = _write_commit(repo, "a.txt", b"a\n", "feat: a")
    # The payload names the old commit twice; the rebind puts a different commit in each place.
    bound = _row("T-1", head_sha=source, reason=f"read at {source}")
    rebind = _superseding(bound, head_sha=landed, reason=f"read at {elsewhere}")

    findings = _scan(repo, [bound, rebind], [_history_row("c-1", source)])

    assert findings == [(_HISTORY, 1, source), (_TASK_LEDGER, 2, elsewhere)]
