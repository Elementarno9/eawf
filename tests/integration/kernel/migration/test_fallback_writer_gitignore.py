"""A v1 fallback write leaves an initialised repository's git status clean."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import orjson

from eawf.kernel.migration.v1_state import State
from eawf.kernel.migrations import current_target_version
from eawf.kernel.state.enums import ProjectStatus, StoreKind
from eawf.kernel.state.io import commit_mutation, fallback_wal_dir, state_version
from eawf.kernel.state.models import Project
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input
from eawf.runtime.lock import portalock, sibling


def _legacy_state() -> dict[str, object]:
    """Return the minimal epoch-1 document an older ``eawf init`` wrote."""
    return {
        "schema_version": current_target_version(),
        "scope_kind": "repo",
        "urn": build_urn("state", owner="DEMO"),
        "updated_at": "2026-01-01T00:00:00+00:00",
        "project": Project(
            code="DEMO",
            slug="demo",
            title="DEMO",
            description=None,
            domains=["general"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn=build_urn("repo", owner="DEMO"),
        ).model_dump(mode="json"),
        "current": {
            "project_code": "DEMO",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


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


def test_custom_root_state_layout_ignores_only_owned_runtime_artifacts(
    tmp_path: Path,
) -> None:
    """A root state path stays clean without hiding unrelated lock files."""
    target = tmp_path / "custom-root"
    target.mkdir()
    _git(target, "init", "--quiet")
    _configure_git(target)
    run_wizard_no_input(_answers("state.json"), target, force=False)
    state_path = target / "state.json"
    # Init writes no v1 document; the fallback writer under test needs one.
    state_path.write_bytes(orjson.dumps(_legacy_state()))
    (target / "uv.lock").write_text("", encoding="utf-8")
    _commit_all(target, "test: seed custom state repository")

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
