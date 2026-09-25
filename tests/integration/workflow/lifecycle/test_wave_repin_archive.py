"""Wave branch heads are archived before a repin can orphan them.

Every test builds a scratch repository whose waves ran on ``-pNN-wMM``
branches and were cherry-picked onto ``main``, so the state pins point at
branch-only commits. ``eawf wave archive-refs`` must preserve each branch
head under ``refs/eawf/archive/<phase>/<wave>``, and ``verify-commits
--repair`` must refuse while a pin it would drop is held only by an
unarchived branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.workflow.lifecycle.wave_archive import (
    WaveArchiveError,
    archive_ref_for_branch,
    archive_wave_branches,
    branches_orphaning,
)
from tests.integration.test_wave_verify_commits_cmd import (
    _commit_wave,
    _git,
    _read_commit,
    _seed_state,
)

W01 = "P28-I01-W01"
W06 = "P28-I01-W06"
REF_W01 = "refs/eawf/archive/P28/W01"
REF_W06 = "refs/eawf/archive/P28/W06"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    """Return a repo on ``main`` whose W01/W06 branch commits were cherry-picked home.

    The long-running ``feature/zz-v1.0`` branch carries no wave number and
    must never be archived. State pins the pre-cherry-pick branch SHAs.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "root.txt").write_text("root\n", encoding="utf-8")
    _git(root, "add", "root.txt")
    _git(root, "commit", "-q", "-m", "chore: root")
    _git(root, "branch", "feature/zz-v1.0")
    pins: dict[str, str] = {}
    for wave in ("W01", "W06"):
        _git(root, "switch", "-q", "-c", f"feature/zz-v1.0-p28-{wave.lower()}", "main")
        pins[wave] = _commit_wave(root, name=f"{wave}.txt", wave_id=f"P28-I01-{wave}")
    _git(root, "switch", "-q", "main")
    _commit_wave(root, name="W04.txt", wave_id="P28-I01-W04")
    _git(root, "cherry-pick", pins["W01"], pins["W06"])
    state_path = _seed_state(root / ".ea", waves={W01: pins["W01"], W06: pins["W06"]})
    monkeypatch.setenv("EA_STATE", str(state_path))
    return root, pins


def _invoke(*args: str) -> tuple[int, str]:
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, list(args))
    return res.exit_code, res.output


def _archive_refs(root: Path) -> dict[str, str]:
    out = _git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/eawf/archive")
    return dict(line.split(" ", 1) for line in out.splitlines())


def test_archive_ref_for_branch_maps_only_wave_branches() -> None:
    assert archive_ref_for_branch("feature/zz-v1.0-p28-w01") == REF_W01
    assert archive_ref_for_branch("feature/zz-v1.0-p100-w123") == "refs/eawf/archive/P100/W123"
    assert archive_ref_for_branch("feature/zz-v1.0") is None
    assert archive_ref_for_branch("feature/zz-v1.0-p2-w01") is None


def test_archive_refs_writes_one_ref_per_wave_branch(repo: tuple[Path, dict[str, str]]) -> None:
    root, pins = repo
    code, output = _invoke("--json", "wave", "archive-refs")
    assert code == 0, output
    payload = json.loads(output)
    assert payload["written_count"] == 2
    assert {row["outcome"] for row in payload["refs"]} == {"created"}
    assert _archive_refs(root) == {REF_W01: pins["W01"], REF_W06: pins["W06"]}


def test_archive_refs_is_idempotent(repo: tuple[Path, dict[str, str]]) -> None:
    root, pins = repo
    assert _invoke("wave", "archive-refs")[0] == 0
    code, output = _invoke("wave", "archive-refs")
    assert code == 0, output
    assert "0 written, 2 unchanged" in output
    assert _archive_refs(root) == {REF_W01: pins["W01"], REF_W06: pins["W06"]}


def test_archive_refs_fast_forwards_a_moved_branch(repo: tuple[Path, dict[str, str]]) -> None:
    root, _pins = repo
    assert archive_wave_branches(repo_root=root)
    _git(root, "switch", "-q", "feature/zz-v1.0-p28-w01")
    new_head = _commit_wave(root, name="W01-more.txt", wave_id=W01)
    _git(root, "switch", "-q", "main")
    entries = {e.wave_branch.archive_ref: e for e in archive_wave_branches(repo_root=root)}
    assert entries[REF_W01].outcome == "advanced"
    assert entries[REF_W06].outcome == "unchanged"
    assert _archive_refs(root)[REF_W01] == new_head


def test_archive_refs_refuses_to_move_a_ref_sideways(repo: tuple[Path, dict[str, str]]) -> None:
    root, pins = repo
    _git(root, "update-ref", REF_W01, pins["W06"])
    code, output = _invoke("wave", "archive-refs")
    assert code == exit_codes.USER_ERROR, output
    assert REF_W01 in output
    # All or nothing: the unrelated W06 ref was not written either.
    assert _archive_refs(root) == {REF_W01: pins["W06"]}


def test_archive_refs_outside_a_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(WaveArchiveError):
        archive_wave_branches(repo_root=tmp_path)


def test_branches_orphaning_is_empty_without_commits(tmp_path: Path) -> None:
    assert branches_orphaning([], repo_root=tmp_path) == []


def test_repair_refuses_while_the_archive_is_missing(repo: tuple[Path, dict[str, str]]) -> None:
    root, pins = repo
    state_path = root / ".ea" / "state.json"
    code, output = _invoke("wave", "verify-commits", "--repair")
    assert code == exit_codes.USER_ERROR, output
    assert "archive-refs" in output
    assert "feature/zz-v1.0-p28-w01" in output
    assert _read_commit(state_path, W01) == pins["W01"]
    assert _archive_refs(root) == {}


def test_repair_runs_once_the_archive_is_written(repo: tuple[Path, dict[str, str]]) -> None:
    root, pins = repo
    state_path = root / ".ea" / "state.json"
    assert _invoke("wave", "archive-refs")[0] == 0
    code, output = _invoke("--json", "wave", "verify-commits", "--repair")
    assert code == 0, output
    assert json.loads(output)["repaired_count"] == 2
    for wave_id, wave in ((W01, "W01"), (W06, "W06")):
        repinned = _read_commit(state_path, wave_id)
        assert repinned != pins[wave]
        assert _git(root, "merge-base", "--is-ancestor", str(repinned), "main") == ""
    # The old pins survive a branch prune through their archive refs.
    _git(root, "branch", "-D", "feature/zz-v1.0-p28-w01", "feature/zz-v1.0-p28-w06")
    assert _git(root, "rev-parse", REF_W01) == pins["W01"]
    assert _invoke("wave", "verify-commits")[0] == 0


def test_repair_refuses_again_when_a_branch_moves_past_its_archive(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, _pins = repo
    assert _invoke("wave", "archive-refs")[0] == 0
    _git(root, "switch", "-q", "feature/zz-v1.0-p28-w06")
    _commit_wave(root, name="W06-more.txt", wave_id=W06)
    _git(root, "switch", "-q", "main")
    # Pin W06 to the unarchived new head: only its branch holds that commit.
    state_path = root / ".ea" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["waves"][W06]["commit"] = _git(root, "rev-parse", "feature/zz-v1.0-p28-w06")
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    code, output = _invoke("wave", "verify-commits", "--repair")
    assert code == exit_codes.USER_ERROR, output
    assert "feature/zz-v1.0-p28-w06" in output
    assert "feature/zz-v1.0-p28-w01" not in output
