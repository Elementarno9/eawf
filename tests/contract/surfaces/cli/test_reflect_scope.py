"""``reflect run`` selects its Runs by --window and --scope, and prints JSON on --format json.

A Run is placed by its scope: a Task's Batch ships under a Milestone, and the Milestone
serves its primary Track and any contributing ones. Local sessions are placed by the
directory they ran in: the session pages cover the scope's sessions, and an output
directory git would commit refuses other projects' rows before a byte lands in it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.observability.reflect import report as rp
from eawf.observability.reflect.analysis import ANALYSIS_PREFIX
from eawf.observability.reflect.html.shared import page_name
from eawf.observability.reflect.report import ReflectReport
from eawf.observability.telemetry.sources.session_history import working_dir_digest
from eawf.platform.registry import Registry, RegistryRepoEntry
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import native_tree, run_row

runner = CliRunner()

BASE = "eawf://WSP-RFL/PRJ-RFL/REP-RFL"


def on_task(row: dict[str, Any], task: str) -> dict[str, Any]:
    """Return ``row`` scoped to the Task keyed ``task``."""
    return row | {"scope": row["scope"] | {"task_ref": f"{BASE}/task/{task}"}}


def recent(row: dict[str, Any]) -> dict[str, Any]:
    """Return ``row`` started a day ago and ended an hour later."""
    started = datetime.now(UTC) - timedelta(days=1)
    return row | {
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(hours=1)).isoformat(),
    }


def place(tree_root: Path) -> None:
    """File the Tasks, Batches and Milestones the fixture Runs are placed by."""
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    document["task"] = {
        f"RFL-000{n}": {"key": f"RFL-000{n}", "batch_ref": f"{BASE}/batch/BAT-000{n}"}
        for n in (1, 2, 4)
    }
    document["batch"] = {
        "BAT-0001": {"key": "BAT-0001", "milestone_ref": f"{BASE}/milestone/MLS-0104"},
        "BAT-0002": {"key": "BAT-0002", "milestone_ref": f"{BASE}/milestone/MLS-0105"},
        "BAT-0004": {"key": "BAT-0004", "milestone_ref": f"{BASE}/milestone/MLS-0106"},
    }
    document["milestone"] = {
        "MLS-0104": {"key": "MLS-0104", "primary_track_ref": f"{BASE}/track/TRK-EAWF-CORE"},
        "MLS-0105": {"key": "MLS-0105", "primary_track_ref": f"{BASE}/track/TRK-RFL-SIDE"},
        "MLS-0106": {
            "key": "MLS-0106",
            "primary_track_ref": f"{BASE}/track/TRK-RFL-SIDE",
            "contributing_track_refs": [f"{BASE}/track/TRK-EAWF-CORE"],
        },
    }
    write_document(path, document)


@pytest.fixture
def placed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree with Runs under two Milestones, one recent unplaced Run and a queued one."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [
            on_task(run_row("RUN-00000001"), "RFL-0001"),
            on_task(run_row("RUN-00000002"), "RFL-0002"),
            on_task(recent(run_row("RUN-00000003")), "RFL-0003"),
            on_task(run_row("RUN-00000004"), "RFL-0004"),
            on_task(run_row("RUN-00000005", status="QUEUED"), "RFL-0001"),
        ],
        [],
    )
    place(tree_root)
    return tree_root


def _json(tree_root: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "-w",
            str(tree_root.parent),
            "reflect",
            "run",
            "--local-only",
            "--format",
            "json",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _keys(document: dict[str, Any]) -> list[str]:
    return [line["run_key"] for line in document["sessions"]]


def test_window_scope_and_format_select_runs(placed_tree: Path) -> None:
    """Window and scope narrow sessions and totals; workspace and user keep every Run."""
    everything = _json(placed_tree)
    assert ReflectReport.model_validate(everything).schema_version == 2
    assert _keys(everything) == [f"RUN-0000000{n}" for n in range(1, 6)]
    assert everything["scope"] == {"kind": "project", "key": None}

    week = _json(placed_tree, "--window", "7d")
    assert _keys(week) == ["RUN-00000003"]
    assert week["window_days"] == 7

    milestone = _json(placed_tree, "--scope", "milestone:MLS-0104")
    assert _keys(milestone) == ["RUN-00000001", "RUN-00000005"]
    # no Run here reported usage, so none counts as unpriced spend
    assert [row["unpriced_runs"] for row in milestone["totals"]] == [0]

    track = _json(placed_tree, "--scope", "track:TRK-EAWF-CORE")
    assert _keys(track) == ["RUN-00000001", "RUN-00000004", "RUN-00000005"]
    assert [row["unpriced_runs"] for row in track["totals"]] == [0]

    for wide, kind in (("workspace", "workspace"), ("user", "user"), ("all_local", "user")):
        document = _json(placed_tree, "--scope", wide)
        assert _keys(document) == _keys(everything)
        assert document["scope"] == {"kind": kind, "key": None}


@pytest.mark.parametrize(
    "value",
    ["", "0d", "7", "7w", "-1d", "10000d", "d"],
    ids=["empty", "zero", "no-unit", "weeks", "negative", "over-max", "no-number"],
)
def test_a_window_that_is_not_whole_days_is_refused(value: str) -> None:
    """Error path: --window takes 1d to 9999d."""
    with pytest.raises(ValueError, match="<N>d"):
        rp.parse_window(value)


def test_window_bounds_are_one_and_9999_days() -> None:
    """Boundary: the smallest and largest windows parse."""
    assert (rp.parse_window("1d"), rp.parse_window("9999d")) == (1, 9999)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "project:",
        "project:EAWF",
        "milestone:",
        "milestone:M-1",
        "track:EAWF",
        "cohort:x",
    ],
    ids=[
        "empty",
        "bare-colon",
        "keyed-project",
        "no-key",
        "bad-milestone",
        "bad-track",
        "cohort",
    ],
)
def test_a_scope_outside_the_named_ones_is_refused(value: str) -> None:
    """Error path: only project, workspace, user, milestone:<KEY> and track:<KEY> parse."""
    with pytest.raises(ValueError, match="milestone"):
        rp.parse_scope(value)


def test_an_unknown_format_exits_2(placed_tree: Path) -> None:
    """Error path: --format takes text or json."""
    result = runner.invoke(
        app, ["-w", str(placed_tree.parent), "reflect", "run", "--format", "yaml"]
    )
    assert result.exit_code == 2, result.output


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _claude(cwd: Path, session: str) -> str:
    rows: list[dict[str, Any]] = [
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
                "id": f"{session}-m0",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    return "".join(f"{json.dumps(row)}\n" for row in rows)


_SESSION_DIRS = ("repo", "repo-wt", "sibling", "elsewhere")


@pytest.fixture
def git_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a git repository's tree with a live worktree and one session per directory.

    The repository and a sibling are in the registry; ``elsewhere`` is not.
    """
    monkeypatch.delenv("EA_STATE", raising=False)
    base = tmp_path.resolve()
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "init")
    _git(repo, "worktree", "add", "-q", "--detach", str(base / "repo-wt"))
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    tree = native_tree(repo, [run_row("RUN-00000001")], [])
    (base / "sibling").mkdir()
    registry = base / "registry.json"
    registry.write_text(
        Registry(
            repos={
                code: RegistryRepoEntry(code=code, path=str(base / name))
                for code, name in (("RFL", "repo"), ("SIB", "sibling"))
            }
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(registry))
    projects = base / "harness" / "projects"
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_HOME", str(base / "codex-home"))
    for name in _SESSION_DIRS:
        folder = projects / f"-{name}"
        folder.mkdir(parents=True)
        (folder / "session.jsonl").write_text(_claude(base / name, name), encoding="utf-8")
    return tree


def _wide(tree: Path, *args: str) -> Any:
    return runner.invoke(app, ["-w", str(tree.parent), "reflect", "run", "--local-only", *args])


@pytest.mark.parametrize(
    ("scope", "covered"),
    [
        ("project", _SESSION_DIRS[:2]),
        ("workspace", _SESSION_DIRS[:3]),
        ("user", _SESSION_DIRS),
        ("all_local", _SESSION_DIRS),
    ],
)
def test_reflect_run_pages_the_sessions_each_scope_covers(
    git_world: Path, tmp_path: Path, scope: str, covered: tuple[str, ...]
) -> None:
    """Every scope exits 0 and writes one session page per covered session, no other."""
    out = tmp_path / "outside" / scope
    result = _wide(git_world, "--scope", scope, "--out", str(out / "r.txt"))

    assert result.exit_code == 0, result.output
    (analysis,) = out.glob(f"{ANALYSIS_PREFIX}*.json")
    sessions = json.loads(analysis.read_text(encoding="utf-8"))["sessions"]
    paged = {
        row["project_key"] for row in sessions if (out / page_name(row["session_ref"])).exists()
    }
    base = tmp_path.resolve()
    assert paged == {working_dir_digest(base / name) for name in covered}


def test_reflect_run_refuses_foreign_rows_on_a_committed_output(
    git_world: Path, tmp_path: Path
) -> None:
    """A committed --out is a user error naming the two foreign rows, and stays unwritten."""
    repo = git_world.parent
    for scope in ("workspace", "user"):
        refused = _wide(git_world, "--scope", scope, "--out", str(repo / "reports" / "r.txt"))
        assert refused.exit_code == exit_codes.USER_ERROR, refused.output
        assert "refusing to write 2 session rows of other projects" in refused.output
        assert not (repo / "reports").exists()

    before = {path for path in repo.rglob("*") if ".git" not in path.parts}
    assert _wide(git_world, "--scope", "user").exit_code == 0
    after = {path for path in repo.rglob("*") if ".git" not in path.parts}
    assert after - before
    assert all(path.is_relative_to(git_world / "local") for path in after - before)

    ignored = repo / "ignored" / "deep"
    assert _wide(git_world, "--scope", "user", "--out", str(ignored / "r.txt")).exit_code == 0
    assert (ignored / "r.txt").is_file()
