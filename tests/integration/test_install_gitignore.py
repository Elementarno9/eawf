"""Integration coverage for EAWF-managed runtime lock ignores."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input


def _answers() -> WizardAnswers:
    return WizardAnswers(
        state_path=".ea/state.json",
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


def test_initialized_repo_stays_clean_after_runtime_store_append(tmp_path: Path) -> None:
    """Persistent sibling locks stay ignored without hiding dependency locks."""
    target = tmp_path / "proj"
    target.mkdir()
    _git(target, "init", "--quiet")
    run_wizard_no_input(_answers(), target, force=False)
    (target / "uv.lock").write_text("", encoding="utf-8")

    top_level_lock = target / ".ea" / "state.json.lock"
    assert top_level_lock.exists()
    assert (
        _git(
            target,
            "check-ignore",
            "--quiet",
            str(top_level_lock.relative_to(target)),
            check=False,
        ).returncode
        == 0
    )
    assert _git(target, "check-ignore", "--quiet", "uv.lock", check=False).returncode == 1

    _git(target, "config", "user.name", "EAWF Test")
    _git(target, "config", "user.email", "test@example.invalid")
    _git(target, "config", "commit.gpgSign", "false")
    _git(target, "config", "core.hooksPath", ".git/hooks")
    _git(target, "add", "--all")
    _git(target, "commit", "--quiet", "--message", "test: seed initialized repository")
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
    assert (
        _git(
            target,
            "check-ignore",
            "--quiet",
            str(nested_lock.relative_to(target)),
            check=False,
        ).returncode
        == 0
    )
    assert _git(target, "status", "--short", "--untracked-files=all").stdout == ""
