"""The host file-edit bracket: which Run an edit binds to, where its tree is taken, and
what a ``before`` whose ``after`` never arrives leaves behind.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_file_edit import (
    HOST_FILE_EDIT_AFTER_METHOD,
    HOST_FILE_EDIT_BEFORE_METHOD,
    PENDING_LOCATOR,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    tree_root,
)
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    SESSION,
    _running_run,
)

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A one-commit git canary holding one running Run on the host session."""
    provisioned = provision(tmp_path / "repo")
    seed(
        provisioned,
        {"run": {RUN_KEY: _running_run(SESSION)}, "repository": {"REP-EAWF": repository_row()}},
    )
    repo = provisioned.root
    (repo / ".gitignore").write_text(".ea/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    return method_context(tmp_path / "runtime")


def _edit(method: str, ctx: MethodContext, canary: CanaryProvision, path: Path, call: str) -> Any:
    params = {
        "repo_root": str(canary.root),
        "harness": "claude-code",
        "host_session_id": SESSION,
        "tool_use_id": call,
        "tool_name": "Edit",
        "file_path": str(path),
    }
    return asyncio.run(methods.dispatch(method, ctx, params))


def test_an_edit_in_a_managed_worktree_is_recorded(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _git(canary.root, "worktree", "add", "-q", ".ea/worktrees/w1", "-b", "w1")
    edited = canary.root / ".ea" / "worktrees" / "w1" / "src" / "app.py"

    _edit(HOST_FILE_EDIT_BEFORE_METHOD, ctx, canary, edited, "toolu_wt")
    edited.write_text("a = 2\n", encoding="utf-8")
    answer = _edit(HOST_FILE_EDIT_AFTER_METHOD, ctx, canary, edited, "toolu_wt")

    assert answer["event_ref"] is not None, answer["reason"]
    assert answer["reason"] == "src/app.py changed and is recorded"


def test_an_edit_in_the_main_checkout_is_still_recorded(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    edited = canary.root / "src" / "app.py"

    _edit(HOST_FILE_EDIT_BEFORE_METHOD, ctx, canary, edited, "toolu_main")
    edited.write_text("a = 3\n", encoding="utf-8")
    answer = _edit(HOST_FILE_EDIT_AFTER_METHOD, ctx, canary, edited, "toolu_main")

    assert answer["event_ref"] is not None, answer["reason"]


def test_a_queued_run_takes_no_host_edit(canary: CanaryProvision, ctx: MethodContext) -> None:
    queued = _running_run(SESSION) | {"status": "QUEUED"}
    seed(canary, {"run": {RUN_KEY: queued}})

    with pytest.raises(DaemonValidationError, match="identity_not_found: 0 live Runs"):
        _edit(HOST_FILE_EDIT_BEFORE_METHOD, ctx, canary, canary.root / "src" / "app.py", "q")


def test_a_pending_tree_whose_after_never_came_is_released(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    pending = tree_root(canary) / PENDING_LOCATOR
    pending.mkdir(parents=True, exist_ok=True)
    stale, fresh = pending / "stale.json", pending / "fresh.json"
    for orphan in (stale, fresh):
        orphan.write_text("{}", encoding="utf-8")
    two_days_ago = time.time() - 2 * 86_400
    os.utime(stale, (two_days_ago, two_days_ago))

    _edit(HOST_FILE_EDIT_BEFORE_METHOD, ctx, canary, canary.root / "src" / "app.py", "next")

    assert not stale.exists()
    assert fresh.exists()
