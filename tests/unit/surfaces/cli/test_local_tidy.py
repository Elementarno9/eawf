"""``eawf local tidy`` archives hand-made ``.ea/local`` entries and orphan ``.ea`` files.

Every test runs against a throwaway git repository under ``tmp_path``.
"""

from __future__ import annotations

import errno
import os
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the verb's output and exit

from eawf.kernel.store.commit_policy import EA_PATH_CLASSES
from eawf.observability.doctor import checks
from eawf.platform import local_tidy
from eawf.platform.install.gitignore_writer import GITIGNORE_PATTERNS
from eawf.platform.local_tidy import (
    PENDING_FILENAME,
    LocalTidyError,
    apply_local_tidy,
    manifest_line,
    parse_manifest_line,
    plan_local_tidy,
)
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.scope import NOT_MANAGED_MESSAGE
from tests._linked_worktree import git

_TODAY = date(2026, 11, 10)
_ARCHIVE = ".ea/local/archive/2026-11-10-tidy"
_REPO_ROOT = Path(__file__).resolve().parents[4]


class _FixedDate(date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(2026, 11, 10)


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from the root of a fixture repository eawf manages, with one tracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    (repo / ".ea" / "config.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    git(repo, "add", ".ea/config.yaml")
    git(repo, "commit", "-q", "-m", "init")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr("eawf.surfaces.cli.commands.local.date", _FixedDate)
    return repo.resolve()


def _tidy(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["local", "tidy", *args])
    return result.exit_code, result.stdout


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _write(path: Path, body: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_tidy_dry_run_then_move_then_idempotent(managed: Path) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md", "# notes\n")
    _write(managed / ".ea" / "local" / "research" / "brief.md")
    row = f".ea/local/dispatch\t{_ARCHIVE}/local/dispatch\t8"
    before = _tree(managed)

    code, out = _tidy("--dry-run")
    assert code == 0
    assert out.splitlines() == ["local tidy: would move 1 entry", f"  {row}"]
    assert _tree(managed) == before

    code, out = _tidy()
    assert code == 0
    assert out.splitlines() == ["local tidy: moved 1 entry", f"  {row}"]
    assert not (managed / ".ea" / "local" / "dispatch").exists()
    assert (managed / _ARCHIVE / "local" / "dispatch" / "x.md").read_text() == "# notes\n"
    assert (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8") == f"{row}\n"
    assert (managed / ".ea" / "local" / "research" / "brief.md").is_file()

    code, out = _tidy()
    assert (code, out) == (0, "local tidy: moved 0 entries\n")
    assert git(managed, "status", "--porcelain", "--untracked-files=no") == ""
    assert (managed / ".ea" / "config.yaml").read_text() == "schema_version: '1'\n"


def test_orphans_and_dropped_lines(managed: Path) -> None:
    ea = managed / ".ea"
    orphans = [
        "state.json.bak.v1.2",
        "specs/P32/P32-I01.md",
        "locks/worktrees.lock.lock",
        "store/commit_repin.jsonl",
        "store/gold_label.jsonl",
    ]
    for rel in orphans:
        _write(ea / rel)
    _write(ea / "state.json.bak.tracked")
    git(managed, "add", "-f", ".ea/state.json.bak.tracked")
    _write(ea / "store" / "audit.jsonl")

    code, out = _tidy()

    assert code == 0, out
    assert out.splitlines()[0] == "local tidy: moved 5 entries"
    assert "  kept .ea/state.json.bak.tracked: git tracks a file in it" in out.splitlines()
    for rel in ("state.json.bak.v1.2", "specs", "locks/worktrees.lock.lock"):
        assert not (ea / rel).exists()
        assert (managed / _ARCHIVE / rel).exists()
    assert not (ea / "store" / "commit_repin.jsonl").exists()
    assert not (ea / "store" / "gold_label.jsonl").exists()
    assert (ea / "store" / "audit.jsonl").is_file()
    assert (ea / "state.json.bak.tracked").is_file()
    manifest = (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[0] for line in manifest] == [
        ".ea/state.json.bak.v1.2",
        ".ea/specs",
        ".ea/locks/worktrees.lock.lock",
        ".ea/store/commit_repin.jsonl",
        ".ea/store/gold_label.jsonl",
    ]
    assert all(line.split("\t")[2] == "2" for line in manifest)

    shipped = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for lines in (GITIGNORE_PATTERNS, shipped):
        assert not [line for line in lines if "specs" in line or ".bak" in line]
    assert not [row for row in EA_PATH_CLASSES if "specs" in row.pattern or "bak" in row.pattern]
    assert not hasattr(checks, "STATE_BACKUP_WARN_COUNT")
    assert not hasattr(checks, "_state_backup_census")


def test_a_clean_tree_moves_nothing(managed: Path) -> None:
    assert _tidy("--dry-run") == (0, "local tidy: would move 0 entries\n")
    assert not (managed / ".ea" / "local").exists()


def test_refuses_outside_a_managed_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.chdir(loose)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    result = CliRunner().invoke(app, ["local", "tidy", "--dry-run"])
    assert (result.exit_code, result.stdout) == (3, "")
    assert NOT_MANAGED_MESSAGE in result.stderr


def test_an_existing_target_is_kept_in_place(managed: Path) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")
    _write(managed / _ARCHIVE / "local" / "dispatch" / "older.md")
    code, out = _tidy()
    assert code == 0
    assert out.splitlines() == [
        "local tidy: moved 0 entries",
        f"  kept .ea/local/dispatch: {_ARCHIVE}/local/dispatch already exists",
    ]
    assert (managed / ".ea" / "local" / "dispatch" / "x.md").is_file()


def test_a_glob_like_name_and_a_link_move_as_themselves(managed: Path) -> None:
    local = managed / ".ea" / "local"
    _write(local / "*" / "a.md")
    _write(local / "research" / "kept.md")
    (local / "to-research").symlink_to(local / "research")
    plan = plan_local_tidy(managed, today=_TODAY)
    assert [move.source.name for move in plan.moves] == ["*", "to-research"]
    apply_local_tidy(plan)
    assert (managed / _ARCHIVE / "local" / "to-research").is_symlink()
    assert (local / "research" / "kept.md").is_file()


def test_a_fifo_is_kept(managed: Path) -> None:
    local = managed / ".ea" / "local"
    local.mkdir(parents=True)
    os.mkfifo(local / "pipe")
    plan = plan_local_tidy(managed, today=_TODAY)
    assert plan.moves == ()
    assert [(kept.path.name, kept.reason) for kept in plan.kept] == [
        ("pipe", "it is not a file, a folder or a link")
    ]


def test_a_target_made_after_planning_is_skipped(managed: Path) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")
    plan = plan_local_tidy(managed, today=_TODAY)
    _write(managed / _ARCHIVE / "local" / "dispatch" / "late.md")
    result = apply_local_tidy(plan)
    assert result.made == ()
    assert [(kept.path.name, kept.reason) for kept in result.kept] == [
        ("dispatch", f"{_ARCHIVE}/local/dispatch already exists")
    ]
    assert (managed / ".ea" / "local" / "dispatch" / "x.md").is_file()
    assert not (managed / _ARCHIVE / "MANIFEST.tsv").exists()


# ---------- the legacy reflect collection moves under reports ----------

_LEGACY = ".ea/local/reflect"
_REPORTS = ".ea/local/reports/reflect"
_INDEX = "history-index.sqlite"


def _legacy_collection(repo: Path) -> None:
    legacy = repo / _LEGACY
    for rel in ("report-2026-11-01.md", "title-cache.json", "pages/overview.html", _INDEX):
        _write(legacy / rel, "ab\n")
    for suffix in ("-wal", "-shm"):
        _write(legacy / f"{_INDEX}{suffix}", "w\n")


def test_reflect_data_moves_to_reports_file_by_file(managed: Path) -> None:
    _legacy_collection(managed)
    _write(managed / _REPORTS / "title-cache.json", "newer\n")
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")

    code, out = _tidy()

    assert code == 0, out
    moved = {
        _INDEX: 3,
        f"{_INDEX}-wal": 2,
        f"{_INDEX}-shm": 2,
        "pages/overview.html": 3,
        "report-2026-11-01.md": 3,
    }
    assert out.splitlines() == [
        "local tidy: moved 6 entries",
        *(f"  {_LEGACY}/{rel}\t{_REPORTS}/{rel}\t{size}" for rel, size in moved.items()),
        f"  .ea/local/dispatch\t{_ARCHIVE}/local/dispatch\t2",
        f"  kept {_LEGACY}/title-cache.json: {_REPORTS}/title-cache.json already exists",
    ]
    for rel in moved:
        assert (managed / _REPORTS / rel).is_file()
        assert not (managed / _LEGACY / rel).exists()
    assert (managed / _REPORTS / "title-cache.json").read_text() == "newer\n"
    assert (managed / _LEGACY / "title-cache.json").read_text() == "ab\n"
    assert not (managed / _LEGACY / "pages").exists()
    manifest = (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8")
    assert manifest.splitlines() == [line.strip() for line in out.splitlines()[1:7]]

    code, out = _tidy()
    assert (code, out.splitlines()) == (
        0,
        [
            "local tidy: moved 0 entries",
            f"  kept {_LEGACY}/title-cache.json: {_REPORTS}/title-cache.json already exists",
        ],
    )


def test_an_emptied_legacy_folder_is_removed(managed: Path) -> None:
    _write(managed / _LEGACY / "sub" / "a.json")
    assert _tidy()[0] == 0
    assert not (managed / _LEGACY).exists()
    assert (managed / _REPORTS / "sub" / "a.json").is_file()
    assert _tidy() == (0, "local tidy: moved 0 entries\n")


def test_reflect_dry_run_moves_nothing(managed: Path) -> None:
    _legacy_collection(managed)
    before = _tree(managed)
    code, out = _tidy("--dry-run")
    assert code == 0
    assert out.splitlines()[0] == "local tidy: would move 6 entries"
    assert _tree(managed) == before


def test_a_held_history_index_moves_nothing_of_its_set(managed: Path) -> None:
    _legacy_collection(managed)
    index = managed / _LEGACY / _INDEX
    plan = plan_local_tidy(managed, today=_TODAY)
    with portalock.acquire(index, timeout=0):
        result = apply_local_tidy(plan)
    held = {kept.path.name: kept.reason for kept in result.kept}
    assert held == dict.fromkeys(
        (_INDEX, f"{_INDEX}-shm", f"{_INDEX}-wal"), f"another process holds {_INDEX} open"
    )
    for name in held:
        assert (managed / _LEGACY / name).is_file()
        assert not (managed / _REPORTS / name).exists()
    assert (managed / _REPORTS / "report-2026-11-01.md").is_file()


def test_a_held_index_exits_non_zero(managed: Path) -> None:
    _legacy_collection(managed)
    with portalock.acquire(managed / _LEGACY / _INDEX, timeout=0):
        code, out = _tidy()
    assert code == 3
    assert f"  kept {_LEGACY}/{_INDEX}: another process holds {_INDEX} open" in out.splitlines()


def test_a_free_lock_file_moves_after_its_set(managed: Path) -> None:
    _legacy_collection(managed)
    with portalock.acquire(managed / _LEGACY / _INDEX, timeout=0):
        pass
    assert (managed / _LEGACY / f"{_INDEX}.lock").exists()
    assert _tidy()[0] == 0
    assert (managed / _REPORTS / _INDEX).is_file()
    assert (managed / _REPORTS / f"{_INDEX}.lock").is_file()
    assert not (managed / _LEGACY).exists()


def test_an_existing_index_target_keeps_the_whole_set(managed: Path) -> None:
    _legacy_collection(managed)
    _write(managed / _REPORTS / f"{_INDEX}-wal")
    plan = plan_local_tidy(managed, today=_TODAY)
    reason = f"{_REPORTS}/{_INDEX}-wal already exists"
    assert {kept.path.name: kept.reason for kept in plan.kept} == dict.fromkeys(
        (_INDEX, f"{_INDEX}-shm", f"{_INDEX}-wal"), reason
    )
    assert _INDEX not in {move.source.name for move in plan.moves}


def test_a_set_whose_second_rename_fails_is_put_back(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_collection(managed)
    plan = plan_local_tidy(managed, today=_TODAY)
    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == f"{_INDEX}-shm" and self.parent.name == "reflect":
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    result = apply_local_tidy(plan)
    assert {kept.path.name for kept in result.kept} == {_INDEX, f"{_INDEX}-shm", f"{_INDEX}-wal"}
    assert (managed / _LEGACY / _INDEX).is_file()
    assert not (managed / _REPORTS / _INDEX).exists()
    lines = (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[:4] == [
        f"{_LEGACY}/{_INDEX}\t{_REPORTS}/{_INDEX}\t3",
        f"{_LEGACY}/{_INDEX}-wal\t{_REPORTS}/{_INDEX}-wal\t2",
        f"{_REPORTS}/{_INDEX}-wal\t{_LEGACY}/{_INDEX}-wal\t2",
        f"{_REPORTS}/{_INDEX}\t{_LEGACY}/{_INDEX}\t3",
    ]
    assert (managed / _LEGACY / f"{_INDEX}-wal").is_file()


# ---------- each move is recorded before it is made ----------


def test_a_stop_between_rename_and_record_is_reconciled(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")
    row = f".ea/local/dispatch\t{_ARCHIVE}/local/dispatch\t2"
    real_record = local_tidy._record

    def stop(archive_dir: Path, line: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(local_tidy, "_record", stop)
    with pytest.raises(KeyboardInterrupt):
        apply_local_tidy(plan_local_tidy(managed, today=_TODAY))
    assert (managed / _ARCHIVE / "local" / "dispatch" / "x.md").is_file()
    assert (managed / _ARCHIVE / PENDING_FILENAME).read_text(encoding="utf-8") == f"{row}\n"
    assert not (managed / _ARCHIVE / "MANIFEST.tsv").exists()

    monkeypatch.setattr(local_tidy, "_record", real_record)
    assert _tidy() == (0, "local tidy: moved 0 entries\n")
    assert (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8") == f"{row}\n"
    assert not (managed / _ARCHIVE / PENDING_FILENAME).exists()


def test_a_pending_record_of_a_move_never_made_is_dropped(managed: Path) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")
    row = f".ea/local/dispatch\t{_ARCHIVE}/local/dispatch\t2"
    _write(managed / ".ea" / "local" / "archive" / "2026-11-09-tidy" / PENDING_FILENAME, f"{row}\n")
    _write(managed / ".ea" / "local" / "archive" / "2026-11-08-tidy" / PENDING_FILENAME, "a\tb")
    assert _tidy()[0] == 0
    assert not list((managed / ".ea" / "local" / "archive").glob(f"*/{PENDING_FILENAME}"))
    assert (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8") == f"{row}\n"


def test_a_pending_record_already_in_the_manifest_is_not_doubled(managed: Path) -> None:
    _write(managed / ".ea" / "local" / "dispatch" / "x.md")
    plan = plan_local_tidy(managed, today=_TODAY)
    apply_local_tidy(plan)
    row = manifest_line(plan, plan.moves[0])
    _write(managed / _ARCHIVE / PENDING_FILENAME, f"{row}\n")
    apply_local_tidy(plan_local_tidy(managed, today=_TODAY))
    assert (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8") == f"{row}\n"
    assert not (managed / _ARCHIVE / PENDING_FILENAME).exists()


# ---------- a refused rename is reported, not raised ----------


def test_a_refused_rename_is_kept_and_exits_non_zero(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(managed / ".ea" / "local" / "alpha" / "a.md")
    _write(managed / ".ea" / "local" / "beta" / "b.md")
    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "beta":
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    code, out = _tidy()

    assert code == 3
    assert out.splitlines() == [
        "local tidy: moved 1 entry",
        f"  .ea/local/alpha\t{_ARCHIVE}/local/alpha\t2",
        f"  kept .ea/local/beta: it could not be moved: {os.strerror(errno.EXDEV)}",
    ]
    assert (managed / _ARCHIVE / "MANIFEST.tsv").read_text(encoding="utf-8") == (
        f".ea/local/alpha\t{_ARCHIVE}/local/alpha\t2\n"
    )
    assert (managed / ".ea" / "local" / "beta" / "b.md").is_file()
    assert not (managed / _ARCHIVE / PENDING_FILENAME).exists()


# ---------- manifest fields are escaped reversibly ----------


@pytest.mark.parametrize(
    "name",
    ["plain", "tab\there", "new\nline", "back\\slash", "cr\rname", "\\t literal", "\\", "x" * 255],
)
def test_manifest_line_round_trips(managed: Path, name: str) -> None:
    source = managed / ".ea" / "local" / name
    _write(source / "a.md")
    plan = plan_local_tidy(managed, today=_TODAY)
    (move,) = plan.moves
    line = manifest_line(plan, move)
    assert "\n" not in line and "\r" not in line and line.count("\t") == 2
    assert parse_manifest_line(line) == (
        f".ea/local/{name}",
        f"{_ARCHIVE}/local/{name}",
        2,
    )


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("", "holds 1 fields"),
        ("a\tb", "holds 2 fields"),
        ("a\tb\t1\t2", "holds 4 fields"),
        ("a\tb\t", "no byte count"),
        ("a\tb\t-1", "no byte count"),
        ("a\tb\t٣", "no byte count"),
        ("a\\q\tb\t1", "unknown escape"),
        ("a\tb\\\t1", "unknown escape"),
    ],
)
def test_a_malformed_manifest_line_is_refused(line: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_manifest_line(line)


def test_a_non_string_manifest_line_is_refused() -> None:
    with pytest.raises(AttributeError):
        parse_manifest_line(3)  # type: ignore[arg-type]


def test_a_tree_git_cannot_read_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _write(tmp_path / "plain" / ".ea" / "local" / "dispatch" / "x.md")
    with pytest.raises(LocalTidyError, match="git ls-files failed"):
        plan_local_tidy(tmp_path / "plain", today=_TODAY)
    assert (tmp_path / "plain" / ".ea" / "local" / "dispatch" / "x.md").is_file()


def test_a_string_repo_is_refused() -> None:
    with pytest.raises(TypeError):
        plan_local_tidy("repo", today=_TODAY)  # type: ignore[arg-type]
