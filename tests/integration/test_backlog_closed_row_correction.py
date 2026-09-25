"""``eawf backlog correct``: the one bounded edit a closed backlog row accepts.

A closure can record the wrong resolution text (a row closed with its
namesake's words) or a commit only a per-wave branch keeps alive. The verb
may rewrite exactly those two fields, under a complete audit, with the prior
values kept in the append-only event log. It must never reopen the row,
change its id, touch an open row, or accept a commit off ``HEAD``'s history.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.evidence import backlog as backlog_evi

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
AUDIT_ID = "A-CLOSEOUT"
WRONG_TEXT = "Metrics dispatch-cost tests are hermetic against a global config."
RIGHT_TEXT = "Juror ballots persist per criterion and rebuild the per-item grid."
runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "--allow-empty", "--no-gpg-sign", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "@".join(("tester", "example.invalid")))
    _commit(root, "root")
    state = root / ".ea" / "state.json"
    shutil.copy(FIXTURE, state)
    monkeypatch.setenv("EA_STATE", str(state))
    return root


def _state_path(repo: Path) -> Path:
    return repo / ".ea" / "state.json"


def _invoke(*argv: str) -> int:
    from eawf.surfaces.cli.app import app

    return runner.invoke(app, list(argv)).exit_code


def _seed_complete_audit() -> None:
    artifact_id = f"ART-{AUDIT_ID}"
    uri = f"repo:.ea/artifacts/{artifact_id}.md"
    assert _invoke("artifact", "add", artifact_id, "--kind", "audit_report", "--uri", uri) == 0
    assert (
        _invoke(
            "audit", "add", AUDIT_ID, "--scope-id", "QR", "--kind", "evaluation",
            "--report", artifact_id, "--verdict", "pass",
        )
        == 0
    )  # fmt: skip


def _seed_closed_row(item_id: str = "B071", *, commit: str = "abc1234") -> None:
    assert _invoke("backlog", "add", item_id, "--title", "Persist the jury ballot grid") == 0
    assert (
        _invoke(
            "backlog", "close", item_id, "--resolution", WRONG_TEXT,
            "--commit", commit, "--audit", AUDIT_ID,
        )
        == 0
    )  # fmt: skip


def _row(repo: Path, item_id: str = "B071") -> dict[str, object]:
    return json.loads(_state_path(repo).read_text())["backlog"][item_id]


def _correct_events(repo: Path) -> list[dict[str, object]]:
    events = repo / ".ea" / "store" / "event.jsonl"
    return [
        json.loads(line) for line in events.read_text().splitlines() if '"backlog.correct"' in line
    ]


def test_backlog_correct_rewrites_resolution_and_commit(repo: Path) -> None:
    _seed_complete_audit()
    _seed_closed_row()
    before = _row(repo)
    landed = _commit(repo, "fix: persist juror ballots")

    code = _invoke(
        "backlog", "correct", "B071", "--resolution", RIGHT_TEXT, "--commit", landed[:8],
        "--audit", AUDIT_ID, "--reason", "closed with the text of B71",
    )  # fmt: skip

    assert code == 0
    after = _row(repo)
    assert after["resolution"] == RIGHT_TEXT
    assert after["commit"] == landed
    for frozen in ("id", "status", "title", "closed_at", "created_at", "priority"):
        assert after[frozen] == before[frozen]
    [event] = _correct_events(repo)
    trail = json.loads(event["payload"]["message"])
    assert trail["prior"] == {"commit": "abc1234", "resolution": WRONG_TEXT}
    assert trail["new"] == {"commit": landed, "resolution": RIGHT_TEXT}
    assert trail["audit_id"] == AUDIT_ID
    assert event["summary"] == f"backlog B071 corrected fields=commit,resolution audit={AUDIT_ID}"


def test_backlog_correct_commit_only_keeps_resolution(repo: Path) -> None:
    _seed_complete_audit()
    _seed_closed_row("B149")
    landed = _commit(repo, "feat: the squashed delivery")

    code = _invoke(
        "backlog", "correct", "B149", "--commit", landed,
        "--audit", AUDIT_ID, "--reason", "cited the branch-only wave commit",
    )  # fmt: skip

    assert code == 0
    row = _row(repo, "B149")
    assert row["commit"] == landed
    assert row["resolution"] == WRONG_TEXT
    assert json.loads(_correct_events(repo)[0]["payload"]["message"])["prior"] == {
        "commit": "abc1234"
    }


def test_backlog_correct_refuses_open_row(repo: Path) -> None:
    _seed_complete_audit()
    assert _invoke("backlog", "add", "B002", "--title", "Still open") == 0

    code = _invoke(
        "backlog", "correct", "B002", "--resolution", RIGHT_TEXT,
        "--audit", AUDIT_ID, "--reason", "would smuggle a close",
    )  # fmt: skip

    assert code != 0
    row = _row(repo, "B002")
    assert row["status"] == "open"
    assert row["resolution"] is None
    assert not (repo / ".ea" / "store" / "event.jsonl").read_text().count("backlog.correct")


def test_backlog_correct_refuses_unknown_row(repo: Path) -> None:
    _seed_complete_audit()

    code = _invoke(
        "backlog", "correct", "B999", "--resolution", RIGHT_TEXT,
        "--audit", AUDIT_ID, "--reason", "no such row",
    )  # fmt: skip

    assert code != 0
    assert not json.loads(_state_path(repo).read_text())["backlog"]


@pytest.mark.parametrize("flag", [["--status", "open"], ["--id", "B072"]])
def test_backlog_correct_has_no_reopen_or_rename_surface(repo: Path, flag: list[str]) -> None:
    _seed_complete_audit()
    _seed_closed_row()
    before = _row(repo)

    code = _invoke(
        "backlog", "correct", "B071", *flag, "--resolution", RIGHT_TEXT,
        "--audit", AUDIT_ID, "--reason", "try to reopen or rename",
    )  # fmt: skip

    assert code == 2
    assert _row(repo) == before


def test_backlog_correct_refuses_commit_off_head(repo: Path) -> None:
    _seed_complete_audit()
    _seed_closed_row()
    _git(repo, "checkout", "-q", "-b", "wave-branch")
    branch_only = _commit(repo, "fix: only on the wave branch")
    _git(repo, "checkout", "-q", "main")

    code = _invoke(
        "backlog", "correct", "B071", "--commit", branch_only,
        "--audit", AUDIT_ID, "--reason", "repin to the wave commit",
    )  # fmt: skip

    assert code != 0
    assert _row(repo)["commit"] == "abc1234"
    assert _correct_events(repo) == []


def test_backlog_correct_refuses_unresolvable_commit(repo: Path) -> None:
    _seed_complete_audit()
    _seed_closed_row()

    code = _invoke(
        "backlog", "correct", "B071", "--commit", "0" * 40,
        "--audit", AUDIT_ID, "--reason", "repin to a missing commit",
    )  # fmt: skip

    assert code != 0
    assert _row(repo)["commit"] == "abc1234"


def test_backlog_correct_refuses_unknown_audit(repo: Path) -> None:
    _seed_complete_audit()
    _seed_closed_row()

    code = _invoke(
        "backlog", "correct", "B071", "--resolution", RIGHT_TEXT,
        "--audit", "A-MISSING", "--reason", "closed with the text of B71",
    )  # fmt: skip

    assert code == 2
    assert _row(repo)["resolution"] == WRONG_TEXT


def test_resolve_landed_commit_returns_full_sha_for_head(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")

    assert backlog_evi.resolve_landed_commit(repo, head[:7]) == head


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({}, "nothing to correct"),
        ({"resolution": "  "}, "must not be blank"),
        ({"resolution": RIGHT_TEXT, "reason": " "}, "--reason"),
        ({"resolution": WRONG_TEXT}, "already records"),
    ],
)
def test_correct_backlog_refuses_empty_or_no_op_input(
    repo: Path, kwargs: dict[str, str], match: str
) -> None:
    from eawf.workflow.evidence import _io

    _seed_complete_audit()
    _seed_closed_row()
    state = _io.load_state(_state_path(repo))
    call: dict[str, str] = {"reason": "closed with the text of B71", **kwargs}

    with pytest.raises(cli_errors.UserError, match=match):
        backlog_evi.correct_backlog(state, item_id="B071", audit_id=AUDIT_ID, **call)
    assert state.backlog["B071"].resolution == WRONG_TEXT
