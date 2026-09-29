"""LINT-010: history written under the epoch-1 grammar stays lintable, read from git.

``commit_prefix_lint.py --check-history`` lints every commit of a range
without reading state. A scratch repository carries epoch-1 history, the
commit that switched it to epoch 2 and commits after it, and this
repository's own history is read whole: the grammar change must accept
every subject shape already written.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[4]
_TOOL_DIR = _REPO / "tools"


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def history() -> Any:
    _load("commit_prefix_lint")
    return _load("commit_grammar_history")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str, path: str) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(message, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def switched_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with epoch-1 history, the switch commit, and later commits."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "lint@example.invalid")
    _git(root, "config", "user.name", "lint")
    first = _commit(root, "[P12-CORE] polish: an epoch-1 commit", "a.txt")
    _commit(root, "feat: cut this repository over to epoch 2", ".ea/epoch2-opt-in.json")
    _commit(root, "feat: land it\n\nTask: EAWF-0001", "b.txt")
    return root, first


def test_lint_010_check_history_reads_epoch1_history_without_a_warning(
    history: Any, switched_repo: tuple[Path, str]
) -> None:
    root, first = switched_repo

    assert first in history.pre_switch_commits(root)
    assert history.check_history("HEAD", repo_root=root) == (0, "")


def test_lint_010_check_history_rejects_a_retired_form_after_the_switch(
    history: Any, switched_repo: tuple[Path, str]
) -> None:
    root, _ = switched_repo
    bad = _commit(root, "[P12-CORE] polish: written after the switch", "c.txt")

    code, report = history.check_history("HEAD", repo_root=root)

    assert code == 1
    assert report.startswith(f"rejected: {bad[:12]} ")


def test_lint_010_check_history_reports_an_unreadable_range(
    history: Any, switched_repo: tuple[Path, str]
) -> None:
    root, _ = switched_repo

    code, report = history.check_history("no-such-ref..HEAD", repo_root=root)

    assert code == 1
    assert "commit history unreadable" in report


def test_lint_010_every_commit_of_this_repository_stays_lintable(history: Any) -> None:
    """The grammar change accepts every subject shape history already holds."""
    code, report = history.check_history("HEAD", repo_root=_REPO)

    assert code == 0, report
