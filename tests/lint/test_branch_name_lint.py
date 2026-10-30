"""Milestone and Task branch names follow feature/<symbol>-v<X.Y>[-<task-key>]."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO_ROOT / "tools"

pytestmark = pytest.mark.unit


@pytest.fixture()
def lint() -> Any:
    spec = importlib.util.spec_from_file_location(
        "branch_name_lint", _TOOL_DIR / "branch_name_lint.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["branch_name_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "branch",
    [
        "feature/eawf-v0.7",
        "feature/eawf-v0.7-eawf-0628",
        "feature/eawf-v10.12",
        "feature/eawf-v0.7-eawf-0001",
        "feature/my-proj-v1.0-my-proj-0042",
        "feature/ab-v0.1",
    ],
)
def test_canonical_branches_admitted(lint: Any, branch: str) -> None:
    assert lint.branch_name_error(branch) is None


@pytest.mark.parametrize(
    "branch",
    [
        # Real branches cut on this repository's 0.7 Milestone.
        "feature/eawf-v0.7-rc4-final",
        "feature/eawf-v0.7-rc4-ready",
        "feature/eawf-v0.7-rc2",
        "feature/eawf-v0.7-final",
        "feature/eawf-v0.7-ready",
        "feature/eawf-v0.7-stable",
        "feature/eawf-v0.7-EAWF-0628",
        "feature/EAWF-v0.7",
        "feature/eawf-v0.7-eawf-062",
        "feature/eawf-v0.7-eawf-06280",
        "feature/eawf-v0.7-other-0628",
        "feature/eawf-v0.7-eawf-0628-fix",
        "feature/eawf-v0.7.1",
        "feature/eawf-v0",
        "feature/eawf-v0.7-",
    ],
)
def test_noncanonical_milestone_branches_rejected(lint: Any, branch: str) -> None:
    error = lint.branch_name_error(branch)

    assert error is not None
    assert repr(branch) in error


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "",
        "feature/console-redesign",
        "codex/rc3-final-squashed",
        "feature/v0.7",
        "fix/eawf-v0.7-final",
    ],
)
def test_other_branches_untouched(lint: Any, branch: str) -> None:
    assert lint.branch_name_error(branch) is None


def test_push_stage_reads_both_ref_names(lint: Any) -> None:
    env = {
        "PRE_COMMIT_LOCAL_BRANCH": "refs/heads/feature/eawf-v0.7-eawf-0628",
        "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/feature/eawf-v0.7-final",
    }

    assert lint.branches_to_lint(env) == ["feature/eawf-v0.7-eawf-0628", "feature/eawf-v0.7-final"]


def test_push_stage_names_one_branch_once(lint: Any) -> None:
    env = {
        "PRE_COMMIT_LOCAL_BRANCH": "refs/heads/feature/eawf-v0.7",
        "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/feature/eawf-v0.7",
    }

    assert lint.branches_to_lint(env) == ["feature/eawf-v0.7"]


def test_commit_stage_reads_checked_out_branch(lint: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lint, "_current_branch", lambda: "feature/eawf-v0.7-ready")

    assert lint.branches_to_lint({"PRE_COMMIT_LOCAL_BRANCH": ""}) == ["feature/eawf-v0.7-ready"]


def test_detached_head_names_no_branch(lint: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lint, "_current_branch", lambda: None)

    assert lint.branches_to_lint({}) == []


@pytest.mark.parametrize(
    ("remote", "code"),
    [("refs/heads/feature/eawf-v0.7-final", 1), ("refs/heads/feature/eawf-v0.7", 0)],
)
def test_main_exit_code(
    lint: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    remote: str,
    code: int,
) -> None:
    monkeypatch.delenv("PRE_COMMIT_LOCAL_BRANCH", raising=False)
    monkeypatch.setenv("PRE_COMMIT_REMOTE_BRANCH", remote)

    assert lint.main() == code
    assert bool(capsys.readouterr().err) is bool(code)


def test_hook_runs_at_commit_and_push_stages() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}

    hook = hooks["branch-name-lint"]
    assert hook["entry"] == "python3 tools/branch_name_lint.py"
    assert set(hook["stages"]) == {"pre-commit", "pre-push"}
