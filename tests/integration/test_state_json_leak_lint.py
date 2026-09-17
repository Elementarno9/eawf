"""Gate-fire proof: the leak lints scan the lines a commit adds to state.json.

The state file used to be dropped from both leak gates, so a machine path or
token written into one of its free-text fields reached a commit unscanned.
These tests stage a real ``.ea/state.json`` delta in a scratch repository and
pin three outcomes: an added leak reds the gate, an added placeholder passes,
and a leak already committed on an unchanged line is not re-flagged.

Every leak literal is assembled at runtime so this file carries none.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

STATE_REL = ".ea/state.json"
MACHINE_PATH = "/" + "Users" + "/" + "alice" + "/work/repo"
WINDOWS_PATH = "C:" + "\\" + "Users" + "\\" + "alice" + "\\work"
LEAKED_EMAIL = "alice" + "@" + "acmecorp.io"
GITHUB_TOKEN = "gh" + "p_" + "A1" * 20


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write_state(repo: Path, payload: dict[str, Any]) -> None:
    target = repo / STATE_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _repo_with_committed_state(tmp_path: Path, payload: dict[str, Any]) -> Path:
    """Init a repo whose first commit carries ``payload`` as the state file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    # Detach any global hooks so a seed carrying a deliberate leak commits.
    _git(["config", "core.hooksPath", str(repo / ".disabled-hooks")], repo)
    _write_state(repo, payload)
    _git(["add", STATE_REL], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _stage_state(repo: Path, payload: dict[str, Any]) -> None:
    _write_state(repo, payload)
    _git(["add", STATE_REL], repo)


def _lint(repo: Path, hook: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "-w", str(repo), "hook", hook])
    return result.exit_code, json.loads(result.stdout)


def _state_line_of(repo: Path, needle: str) -> int:
    lines = (repo / STATE_REL).read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, start=1) if needle in line)


def test_path_leak_lint_flags_machine_path_added_to_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    _stage_state(repo, {"outcome": f"built in {MACHINE_PATH}", "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 1, payload
    assert payload["scanned"] == 1
    assert payload["findings"] == [
        {
            "path": STATE_REL,
            "lineno": _state_line_of(repo, "outcome"),
            "snippet": "/" + "Users" + "/" + "alice",
        }
    ]


def test_path_leak_lint_passes_placeholder_added_to_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    _stage_state(repo, {"rule": "never commit /Users/<name> paths", "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 0, payload
    assert payload["scanned"] == 1
    assert payload["clean"] is True


def test_path_leak_lint_skips_preexisting_machine_path_in_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"outcome": MACHINE_PATH, "title": "seed"})
    _stage_state(repo, {"outcome": MACHINE_PATH, "note": "clean", "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 0, payload
    assert payload["scanned"] == 1
    assert payload["findings"] == []


def test_path_leak_lint_reports_only_the_added_machine_path(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"outcome": MACHINE_PATH, "title": "seed"})
    fresh = MACHINE_PATH.replace("alice", "bob")
    _stage_state(repo, {"outcome": MACHINE_PATH, "note": fresh, "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 1, payload
    assert [finding["lineno"] for finding in payload["findings"]] == [_state_line_of(repo, "note")]


def test_path_leak_lint_flags_windows_path_added_to_state_json(tmp_path: Path) -> None:
    # json.dumps doubles each backslash; the gate must still see the anchor.
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    _stage_state(repo, {"outcome": WINDOWS_PATH, "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 1, payload
    assert payload["findings"][0]["lineno"] == _state_line_of(repo, "outcome")


def test_path_leak_lint_flags_token_added_to_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    _stage_state(repo, {"note": f"pasted {GITHUB_TOKEN}", "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 1, payload
    assert [finding["snippet"] for finding in payload["findings"]] == [GITHUB_TOKEN]


def test_path_leak_lint_leaves_tokens_outside_state_json_to_detect_secrets(
    tmp_path: Path,
) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    (repo / "notes.txt").write_text(f"pasted {GITHUB_TOKEN}\n", encoding="utf-8")
    _git(["add", "notes.txt"], repo)

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 0, payload
    assert payload["scanned"] == 1


def test_path_leak_lint_scans_whole_state_json_given_explicitly(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"outcome": MACHINE_PATH, "title": "seed"})

    result = runner.invoke(app, ["--json", "-w", str(repo), "hook", "path-leak-lint", STATE_REL])

    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["findings"][0]["path"] == STATE_REL


def test_email_leak_lint_flags_email_added_to_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    _stage_state(repo, {"owner": LEAKED_EMAIL, "title": "seed"})

    exit_code, payload = _lint(repo, "email-leak-lint")

    assert exit_code == 1, payload
    assert payload["findings"] == [
        {"path": STATE_REL, "lineno": _state_line_of(repo, "owner"), "snippet": LEAKED_EMAIL}
    ]


def test_email_leak_lint_skips_preexisting_email_in_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"owner": LEAKED_EMAIL, "title": "seed"})
    _stage_state(repo, {"owner": LEAKED_EMAIL, "note": "noreply@anthropic.com", "title": "seed"})

    exit_code, payload = _lint(repo, "email-leak-lint")

    assert exit_code == 0, payload
    assert payload["scanned"] == 1


def test_path_leak_lint_honours_allowlist_marker_in_state_json(tmp_path: Path) -> None:
    repo = _repo_with_committed_state(tmp_path, {"title": "seed"})
    marker = "pragma: allowlist " + "secret"
    _stage_state(repo, {"sample": f"{MACHINE_PATH} {marker}", "title": "seed"})

    exit_code, payload = _lint(repo, "path-leak-lint")

    assert exit_code == 0, payload
