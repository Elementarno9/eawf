"""Integration coverage for EAWF-managed runtime lock ignores."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.io import commit_mutation, fallback_wal_dir, state_version
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.platform.install.gitignore_writer import GITIGNORE_PATTERNS, write_gitignore
from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input
from eawf.runtime.lock import portalock, sibling


def _answers(state_path: str = ".ea/state.json") -> WizardAnswers:
    return WizardAnswers(
        state_path=state_path,
        project_code="DEMO",
        project_title="Demo",
        lifecycle_depth="phase",
        profiles=("core",),
        runtime="claude-code",
        plugins=(),
        mcp=(),
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _configure_git(repo: Path) -> None:
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "core.hooksPath", ".git/hooks")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)


def _is_ignored(repo: Path, path: Path) -> bool:
    relative = path.relative_to(repo)
    return (
        _git(
            repo,
            "check-ignore",
            "--quiet",
            "--no-index",
            str(relative),
            check=False,
        ).returncode
        == 0
    )


def _append_store_row(path: Path, kind: StoreKind, index: int) -> None:
    append_envelope(
        path,
        Envelope(
            id=f"ROW-{index}",
            kind=kind,
            scope_id=None,
            created_at=datetime(2026, 7, 23, tzinfo=UTC),
            summary=f"exercise {kind.value} store lock",
            payload={},
        ),
    )


def test_initialized_repo_stays_clean_after_runtime_store_append(tmp_path: Path) -> None:
    """Persistent sibling locks stay ignored without hiding dependency locks."""
    target = tmp_path / "proj"
    target.mkdir()
    _git(target, "init", "--quiet")
    run_wizard_no_input(_answers(), target, force=False)
    (target / "uv.lock").write_text("", encoding="utf-8")

    top_level_lock = target / ".ea" / "state.json.lock"
    assert top_level_lock.exists()
    assert _is_ignored(target, top_level_lock)
    assert not _is_ignored(target, target / "uv.lock")

    _configure_git(target)
    _commit_all(target, "test: seed initialized repository")
    assert _git(target, "status", "--short", "--untracked-files=all").stdout == ""

    event_path = target / ".ea" / "store" / "event.jsonl"
    append_envelope(
        event_path,
        Envelope(
            id="EV-lock-ignore",
            kind=StoreKind.EVENT,
            scope_id=None,
            created_at=datetime(2026, 7, 23, tzinfo=UTC),
            summary="exercise persistent event-store lock",
            payload={},
        ),
    )

    nested_lock = event_path.with_name(f"{event_path.name}.lock")
    assert nested_lock.exists()
    assert _is_ignored(target, nested_lock)
    assert _git(target, "status", "--short", "--untracked-files=all").stdout == ""


def test_custom_root_state_layout_ignores_only_owned_runtime_artifacts(
    tmp_path: Path,
) -> None:
    """A root state path stays clean without hiding unrelated lock files."""
    target = tmp_path / "custom-root"
    target.mkdir()
    _git(target, "init", "--quiet")
    _configure_git(target)
    run_wizard_no_input(_answers("state.json"), target, force=False)
    (target / "uv.lock").write_text("", encoding="utf-8")
    _commit_all(target, "test: seed custom state repository")

    state_path = target / "state.json"
    for index, kind in enumerate(StoreKind):
        _append_store_row(store_path(state_path, kind), kind, index)

    payload = orjson.loads(state_path.read_bytes())
    candidate = State.model_validate(payload)
    with portalock.acquire(state_path):
        commit_mutation(
            state_path,
            candidate=candidate,
            before_version=state_version(payload),
            command="fixture.noop",
            args={},
            scope_id="DEMO",
            summary="exercise fallback WAL layout",
        )

    _commit_all(target, "test: track canonical ledger stores")

    backup_path = state_path.with_name(f"{state_path.name}.bak.v1.18.v1.19")
    backup_path.write_text("{}\n", encoding="utf-8")
    actual_lock = fallback_wal_dir(state_path).parent / "actual-P01-I01-W01.lock"
    actual_lock.parent.mkdir(parents=True, exist_ok=True)
    actual_lock.write_text("{}\n", encoding="utf-8")

    intended = [
        sibling.lock_path(state_path),
        backup_path,
        store_path(state_path, StoreKind.EVENT),
        fallback_wal_dir(state_path),
        actual_lock,
        *[sibling.lock_path(store_path(state_path, kind)) for kind in StoreKind],
    ]
    assert all(_is_ignored(target, path) for path in intended)
    assert _git(target, "status", "--short", "--untracked-files=all").stdout == ""

    vendor_lock = state_path.parent / "store" / "vendor.jsonl.lock"
    user_lock = state_path.parent / "locks" / "user.lock"
    vendor_lock.write_text("", encoding="utf-8")
    user_lock.write_text("", encoding="utf-8")
    assert not _is_ignored(target, vendor_lock)
    assert not _is_ignored(target, user_lock)
    assert not _is_ignored(target, target / "uv.lock")
    assert not _is_ignored(target, store_path(state_path, StoreKind.AUDIT))

    managed_lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.lock" not in managed_lines
    assert "/store/*.jsonl.lock" not in managed_lines
    assert "/locks/" not in managed_lines


def test_custom_state_patterns_escape_every_gitignore_metacharacter(
    tmp_path: Path,
) -> None:
    """Literal metacharacters cannot widen a custom state ignore."""
    target = tmp_path / "escape-repo"
    target.mkdir()
    _git(target, "init", "--quiet")
    state_dir = target / "meta \\ space !#*?[]"
    state_path = state_dir / "state \\ !#*?[].json"
    write_gitignore(target, state_path=state_path)

    intended_lock = sibling.lock_path(state_path)
    intended_lock.parent.mkdir(parents=True)
    intended_lock.write_text("", encoding="utf-8")
    near_lock = state_dir / "state wildcard.json.lock"
    near_lock.write_text("", encoding="utf-8")
    actual_lock = fallback_wal_dir(state_path).parent / "actual-scope.lock"
    actual_lock.parent.mkdir(parents=True)
    actual_lock.write_text("", encoding="utf-8")
    user_lock = actual_lock.parent / "user.lock"
    user_lock.write_text("", encoding="utf-8")

    assert _is_ignored(target, intended_lock)
    assert not _is_ignored(target, near_lock)
    assert _is_ignored(target, actual_lock)
    assert not _is_ignored(target, user_lock)


def test_absolute_inside_state_path_serializes_only_repo_relative_patterns(
    tmp_path: Path,
) -> None:
    """An absolute in-repo state path never writes its host prefix."""
    target = tmp_path / "inside-repo"
    target.mkdir()
    state_path = (target / "custom" / "state.json").resolve()

    result = write_gitignore(target, state_path=state_path)
    text = result.path.read_text(encoding="utf-8")

    assert "/custom/state.json.lock" in result.patterns
    assert str(target.resolve()) not in text


def test_outside_state_paths_add_no_dynamic_or_host_patterns(tmp_path: Path) -> None:
    """Absolute and relative escapes outside the repo use the static block."""
    target = tmp_path / "outside-repo"
    target.mkdir()
    outside = tmp_path / "external" / "state.json"

    absolute_result = write_gitignore(target, state_path=outside)
    relative_result = write_gitignore(target, state_path=Path("../external/state.json"))
    text = relative_result.path.read_text(encoding="utf-8")

    assert absolute_result.patterns == GITIGNORE_PATTERNS
    assert relative_result.patterns == GITIGNORE_PATTERNS
    assert str(tmp_path.resolve()) not in text
    assert str(outside) not in text


def test_missing_state_path_keeps_static_patterns_only(tmp_path: Path) -> None:
    """The legacy writer call remains byte-compatible and duplicate-free."""
    target = tmp_path / "static-repo"
    target.mkdir()

    result = write_gitignore(target)

    assert result.patterns == GITIGNORE_PATTERNS
    assert len(result.patterns) == len(set(result.patterns))


@pytest.mark.parametrize("separator", ["\r", "\n"])
def test_state_path_rejects_gitignore_line_injection(
    tmp_path: Path,
    separator: str,
) -> None:
    """CR/LF in a state path rejects before any managed block is written."""
    target = tmp_path / "injection-repo"
    target.mkdir()

    with pytest.raises(ValueError, match="cannot contain CR or LF"):
        write_gitignore(target, state_path=Path(f"bad{separator}pattern/state.json"))

    assert not (target / ".gitignore").exists()


def test_refresh_preserves_user_space_patterns_and_git_behavior(
    tmp_path: Path,
) -> None:
    """Replacement keeps boundary-space patterns byte-exact and effective."""
    target = tmp_path / "space-pattern-repo"
    target.mkdir()
    _git(target, "init", "--quiet")
    gitignore_path = target / ".gitignore"
    prefix = b"trailing\\ \n"
    suffix = b" leading.txt\n"
    gitignore_path.write_bytes(
        prefix
        + b"# BEGIN EAWF:gitignore\n"
        + b"stale-managed-pattern\n"
        + b"# END EAWF:gitignore\n"
        + suffix
    )
    trailing_space = target / "trailing "
    leading_space = target / " leading.txt"
    near_trailing = target / "trailing"
    near_leading = target / "leading.txt"
    for path in (trailing_space, leading_space, near_trailing, near_leading):
        path.write_text("", encoding="utf-8")

    write_gitignore(target, state_path=Path("state.json"))
    first = gitignore_path.read_bytes()

    assert first.startswith(prefix)
    assert first.endswith(suffix)
    assert b"stale-managed-pattern" not in first
    assert _is_ignored(target, trailing_space)
    assert _is_ignored(target, leading_space)
    assert not _is_ignored(target, near_trailing)
    assert not _is_ignored(target, near_leading)

    write_gitignore(target, state_path=Path("state.json"))

    assert gitignore_path.read_bytes() == first
    assert _is_ignored(target, trailing_space)
    assert _is_ignored(target, leading_space)
