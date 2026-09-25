"""``eawf wave verify-commits`` repairs a pin whose landed commit has a blank-line trailer.

A squash or history rewrite leaves a closed wave pinned to a commit that no
ref reaches any more. The repair re-pins it to the commit that landed, found
by its ``Eawf-Wave`` line. When that landed commit put a blank line between
``Eawf-Wave:`` and ``Co-Authored-By:``, the old trailer-block parse found no
candidate and the wave stayed ``pinned_but_missing``.

These cases drive the real CLI against a throwaway repository under
``tmp_path``: no git probe is stubbed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_WAVE_ID = "P28-I01-W01"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(root: Path, *, message: str) -> str:
    """Commit the staged tree with *message* verbatim and return the SHA."""
    message_path = root.parent / "message.txt"
    message_path.write_text(message, encoding="utf-8")
    _git(root, "commit", "-q", "--cleanup=verbatim", "-F", str(message_path))
    return _git(root, "rev-parse", "HEAD")


def _seed_state(repo: Path, *, commit: str) -> Path:
    """Write ``<repo>/.ea/state.json`` holding one closed wave pinned to *commit*."""
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    wave = {
        "id": _WAVE_ID,
        "iter_id": "P28-I01",
        "title": "land the payload module",
        "status": "closed",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": "done",
        "commit": commit,
        "opened_at": "2026-05-27T00:00:00Z",
        "closed_at": "2026-05-27T00:01:00Z",
    }
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": None,
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": "P28",
            "iter_id": "P28-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P28": {
                "id": "P28",
                "scope_id": "ZZ",
                "track_id": None,
                "title": "bootstrap",
                "status": "active",
                "iter_ids": ["P28-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-27T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P28-I01": {
                "id": "P28-I01",
                "phase_id": "P28",
                "title": "first",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-27T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {_WAVE_ID: wave},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


def _squashed_repo(tmp_path: Path, *, landed_body: str) -> tuple[Path, str, str]:
    """Build a repo whose wave commit was squashed onto ``main``.

    The pre-squash commit lives on a topic branch that is then deleted, so the
    object still exists but no ref reaches it. ``main`` carries the landed
    commit with the same tree change and *landed_body* as its message.

    Returns:
        ``(repo, pre_squash_sha, landed_sha)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _commit(repo, message="chore: seed the fixture\n")

    _git(repo, "switch", "-q", "-c", "topic")
    (repo / "payload.txt").write_text("payload\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    pre_squash = _commit(
        repo,
        message="feat: add the payload\n\nEawf-Wave: P28-I01-W01\n",
    )

    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "topic")
    landed = _commit(repo, message=landed_body)
    _git(repo, "branch", "-q", "-D", "topic")
    assert _git(repo, "cat-file", "-t", pre_squash) == "commit"
    assert _git(repo, "branch", "-a", "--contains", pre_squash) == ""
    return repo, pre_squash, landed


def _read_pin(state_path: Path) -> str | None:
    return orjson.loads(state_path.read_bytes())["waves"][_WAVE_ID]["commit"]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep git config, the home directory and the registry inside ``tmp_path``."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EAWF_HOME", str(home / ".eawf"))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(home / ".eawf" / "registry.json"))


def test_verify_commits_blank_line_trailer_pin_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classify as repairable, re-pin to the landed commit, then re-check clean."""
    from eawf.surfaces.cli import exit_codes
    from eawf.surfaces.cli.app import app

    repo, pre_squash, landed = _squashed_repo(
        tmp_path,
        landed_body=(
            "feat: add the payload (squashed)\n"
            "\n"
            "Squashed from the topic branch.\n"
            "\n"
            "Eawf-Wave: P28-I01-W01\n"
            "\n"
            "Co-Authored-By: Test Agent <agent@example.invalid>\n"
        ),
    )
    state_path = _seed_state(repo, commit=pre_squash)
    monkeypatch.setenv("EA_STATE", str(state_path))
    runner = CliRunner()

    scan = runner.invoke(app, ["--json", "wave", "verify-commits"])
    assert scan.exit_code == exit_codes.VALIDATION_ERROR, scan.output
    assert json.loads(scan.stdout)["drifts"] == [
        {
            "wave": _WAVE_ID,
            "kind": "pinned_mismatch",
            "state_commit": pre_squash,
            "git_commit": landed,
            "repairable": True,
            "resolution": "repairable",
        }
    ]
    assert _read_pin(state_path) == pre_squash

    repair = runner.invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert repair.exit_code == 0, repair.output
    repaired = json.loads(repair.stdout)
    assert repaired["repaired_count"] == 1
    assert repaired["skipped_count"] == 0
    assert repaired["repaired"] == [
        {
            "wave": _WAVE_ID,
            "kind": "pinned_mismatch",
            "old_commit": pre_squash,
            "new_commit": landed,
        }
    ]
    assert _read_pin(state_path) == landed

    recheck = runner.invoke(app, ["wave", "verify-commits"])
    assert recheck.exit_code == 0, recheck.output
    assert "0 drift" in recheck.output


def test_verify_commits_mid_sentence_mention_stays_unrepairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landed commit that only mentions the wave in prose is no repair target."""
    from eawf.surfaces.cli.app import app

    repo, pre_squash, _landed = _squashed_repo(
        tmp_path,
        landed_body=(
            "feat: add the payload (squashed)\n"
            "\n"
            "This squash replaces Eawf-Wave: P28-I01-W01 from the topic branch.\n"
        ),
    )
    state_path = _seed_state(repo, commit=pre_squash)
    monkeypatch.setenv("EA_STATE", str(state_path))

    repair = CliRunner().invoke(app, ["--json", "wave", "verify-commits", "--repair"])

    assert repair.exit_code == 0, repair.output
    payload = json.loads(repair.stdout)
    assert payload["repaired_count"] == 0
    assert [(row["wave"], row["kind"]) for row in payload["skipped"]] == [
        (_WAVE_ID, "pinned_but_missing")
    ]
    assert _read_pin(state_path) == pre_squash
